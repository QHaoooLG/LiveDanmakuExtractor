"""托盘应用：把 `PanelController` + 启动面板 + 托盘图标组装起来。

这是 `bili-danmaku tray`（以及打包后的 exe 双击）的入口。

## 线程模型

**全部在 tkinter 主线程**：

* `PanelController` 把 uvicorn 跑在自己的后台线程里；
* tkinter 的窗口、托盘的消息窗口、菜单都归主线程；
* 托盘消息由 `root.after()` 周期调用 `TrayIcon.pump()` 排空 ——
  不需要额外的消息循环线程，也就没有跨线程 UI 调用的问题。

## 退出路径（**不能丢数据**）

托盘「退出」→ 二次确认 → `PanelController.stop()`（`should_exit` →
lifespan 的 `panel.stop()` → 每个房间 `bus.stop()` **flush 落盘**）→
移除托盘图标 → 销毁窗口。**绝不在未 flush 的情况下静默退出**（DEC-3 的核心承诺）。
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import messagebox

from ..launcher import LauncherOptions, PanelController, PanelStatus
from . import _win32
from .panel_window import LauncherWindow, describe_status, format_uptime

log = logging.getLogger("bili_danmaku.gui.tray")

#: 托盘消息泵的间隔（毫秒）。50ms 足够让菜单/单击及时响应，又不占 CPU。
PUMP_MS = 50

#: 菜单命令 id 起始值
MENU_BASE_ID = 1000


def build_menu_items(
    status: PanelStatus, *, on_open, on_copy, on_toggle, on_restart, on_show, on_logs, on_folder, on_quit
) -> list[_win32.MenuItem]:
    """按当前状态构建托盘右键菜单。

    抽成**纯函数**（只需要一个 `PanelStatus` 与若干回调）是为了能离线单测 ——
    GUI 本身在 CI/沙箱里点不了，但"菜单项与置灰逻辑"是可以断言的。
    """
    running = status.running
    state_text, _color = describe_status(status)
    if running:
        header = f"B站弹幕提取姬 · 运行中 {format_uptime(status.uptime_s)}"
        address = status.url or "（地址未知）"
    else:
        header = f"B站弹幕提取姬 · {state_text}"
        address = ""

    items = [
        _win32.MenuItem(header, None, enabled=False),  # 状态行：只展示
    ]
    if address:
        items.append(_win32.MenuItem(address, None, enabled=False))
    items += [
        _win32.MenuItem.sep(),
        _win32.MenuItem("打开面板", on_open, enabled=running, default=True),
        _win32.MenuItem("复制链接", on_copy, enabled=running),
        _win32.MenuItem.sep(),
        _win32.MenuItem("停止采集" if running else "启动采集", on_toggle),
        _win32.MenuItem("重启面板", on_restart, enabled=running),
        _win32.MenuItem.sep(),
        _win32.MenuItem("显示启动面板", on_show),
        _win32.MenuItem("查看日志", on_logs),
        _win32.MenuItem("打开数据目录", on_folder),
        _win32.MenuItem.sep(),
        _win32.MenuItem("退出（停止采集）", on_quit),
    ]
    return items


class TrayApp:
    """托盘应用主体。"""

    def __init__(self, controller: PanelController) -> None:
        self.controller = controller
        self.root = tk.Tk()
        self.window = LauncherWindow(
            self.root,
            controller,
            on_quit=self.quit,
            on_hide=self._on_window_hidden,
        )
        self.tray = _win32.TrayIcon(tooltip="B站弹幕提取姬")
        self._quitting = False
        self._last_status: PanelStatus = controller.status()
        # 立刻同步一次托盘提示，否则在首次 refresh 之前会一直显示默认文字
        # （用户右键/悬浮看到的状态就是错的）。
        self._on_refresh(self._last_status)

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #

    def run(self, *, auto_start: bool = False, show_window: bool = True) -> int:
        """进入事件循环；返回进程退出码。"""
        if not self.tray.show():
            log.warning("托盘图标挂载失败（资源管理器未就绪？）；窗口功能仍可用")
            # 不致命：仍然把窗口显示出来，用户至少能操作
        self._wire_tray()
        self.window.set_refresh_hook(self._on_refresh)

        if auto_start and not self.controller.is_running():
            status = self.controller.start(wait=True, timeout=25)
            if not status.running:
                log.warning("自动启动失败：%s", status.error)

        if show_window:
            self.window.show()
        else:
            self.root.withdraw()

        self.window.schedule_refresh()
        self.root.after(PUMP_MS, self._pump)
        try:
            self.root.mainloop()
        finally:
            self._cleanup()
        return 0

    def _wire_tray(self) -> None:
        """绑定托盘鼠标行为。

        * 左键单击 = 显示启动面板（避免误触弹浏览器）
        * 左键双击 = 直接打开 Web 面板
        * 右键     = 菜单
        """
        self.tray.on_click = self.window.show
        self.tray.on_double_click = self.window.open_panel
        self.tray.on_right_click = self._popup_menu

    def _pump(self) -> None:
        """把 Windows 消息泵进 tkinter 主循环（托盘事件靠它送达）。"""
        if self._quitting:
            return
        self.tray.pump()
        self.root.after(PUMP_MS, self._pump)

    # ------------------------------------------------------------------ #
    # 托盘动作
    # ------------------------------------------------------------------ #

    def _popup_menu(self) -> None:
        items = build_menu_items(
            self.controller.status(),
            on_open=self.window.open_panel,
            on_copy=self.window.copy_link,
            on_toggle=self.window.toggle_collect,
            on_restart=self.window.restart,
            on_show=self.window.show,
            on_logs=self.open_logs,
            on_folder=self.window.open_data_dir,
            on_quit=self.window.quit_app,
        )
        chosen = self.tray.popup_menu(items, base_id=MENU_BASE_ID)
        if chosen is not None and callable(chosen.action):
            chosen.action()

    def open_logs(self) -> None:
        """打开日志文件（不存在就用记事本建一个空的，避免报错）。"""
        import os
        import subprocess

        from ..runtime import LOG_FILE

        path = os.path.abspath(str(LOG_FILE))
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.isfile(path):
                with open(path, "a", encoding="utf-8"):
                    pass
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - Windows 专用
        except (OSError, AttributeError):
            subprocess.Popen(["notepad", path])

    def _on_refresh(self, status: PanelStatus) -> None:
        """每次刷新后同步托盘提示文字。"""
        self._last_status = status
        if status.running:
            self.tray.update_tooltip(
                f"B站弹幕提取姬 · 运行中 {format_uptime(status.uptime_s)}"
            )
        else:
            self.tray.update_tooltip("B站弹幕提取姬 · 已停止")

    def _on_window_hidden(self) -> None:
        """窗口隐藏时给一次托盘气泡提示（仅首次，避免打扰）。"""
        if getattr(self, "_bubble_shown", False):
            return
        self._bubble_shown = True
        try:
            self.tray.update_tooltip("B站弹幕提取姬 · 仍在托盘运行（右键可退出）")
        except Exception:  # pragma: no cover - 提示失败无所谓
            pass

    # ------------------------------------------------------------------ #
    # 退出
    # ------------------------------------------------------------------ #

    def quit(self) -> None:
        """优雅退出：停面板（flush）→ 移除托盘 → 退出事件循环。"""
        if self._quitting:
            return
        self._quitting = True
        self.window.stop_refresh()
        try:
            if self.controller.is_running():
                status = self.controller.stop()
                if status.running:
                    # 停不下来就不要假装退出（否则会丢未落盘数据）
                    self._quitting = False
                    messagebox.showerror(
                        "停止失败",
                        status.error or "面板仍在运行，请稍后重试或查看日志。",
                        parent=self.root,
                    )
                    return
        finally:
            if self._quitting:
                self.tray.hide()
                self.root.quit()

    def _cleanup(self) -> None:
        """主循环退出后的收尾（幂等）。"""
        try:
            # 二维码弹窗是独立顶层窗口，先自己关掉（并结束登录会话），
            # 免得它带着一个还在轮询的 daemon 线程活过主循环
            self.window.shutdown()
        except Exception:  # pragma: no cover - 退出路径不该再抛
            log.debug("退出时关闭二维码窗口失败", exc_info=True)
        try:
            self.tray.hide()
        except Exception:  # pragma: no cover
            pass
        try:
            if self.controller.is_running():
                self.controller.stop()
        except Exception:  # pragma: no cover
            log.exception("退出时停止面板失败")


def run_tray(
    *,
    rooms: tuple[str, ...] = (),
    port: int | None = None,
    out_dir: str | None = None,
    open_browser: bool = False,
    auto_start: bool = True,
) -> int:
    """托盘应用入口（`bili-danmaku tray` 与 exe 双击都走这里）。

    **`open_browser` 与 `auto_start` 是两件事，不要合并**（曾经合并过，是 bug）：
      * `auto_start`：起来就把面板跑起来（`tray --rooms X` 显然要起面板）；
      * `open_browser`：起完自动打开浏览器。

    合并的后果：`tray --rooms 804456 --no-open` 会因为"不打开浏览器"而**连面板都不启动**，
    用户看到进程活着、窗口在、但 `127.0.0.1:8787` 根本没人监听。

    `auto_start=False`（exe 双击的默认）表示**只起托盘 + 显示启动面板，不自动开始采集** ——
    房间号未定时不该猜（DEC-6 / D4）。
    """
    if sys.platform != "win32":
        print("托盘应用目前只支持 Windows（依赖 Shell_NotifyIcon）。", file=sys.stderr)
        return 2

    from ..runtime import LOG_FILE

    options = LauncherOptions(
        rooms=rooms,
        open_browser=open_browser,
        # 托盘进程常常没有 stderr（`--windowed` 打包后 stdout/stderr 都是 None），
        # 不显式落文件就什么都看不到 —— 见 `LauncherOptions.log_file` 的说明。
        log_file=str(LOG_FILE),
    )
    if port is not None:
        options.port = port
    if out_dir:
        options.out_dir = out_dir

    controller = PanelController(options)
    app = TrayApp(controller)
    return app.run(auto_start=auto_start, show_window=True)
