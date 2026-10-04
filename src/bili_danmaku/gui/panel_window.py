"""启动面板（tkinter）：展示状态、提供跳转 Web 面板的链接入口与基础操作。

对应 HANDOVER §5.13.4 的功能清单。设计取向是**简易**：
扩展功能（多房间编辑、格式选择等）留在"高级设置"折叠区或未来实现。

关键行为：
* 关窗口 = **最小化到托盘**（不是退出）；首次关闭弹一次气泡提示。
* 每 2 秒刷新状态，**直接读同进程的 `PanelController`**（不发 HTTP）。
* 「启动采集」前会用 `rooms.py` 校验房间号 —— 识别不出就报错，不猜。
"""

from __future__ import annotations

import base64
import logging
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Any, Callable

from .. import __version__
from ..launcher import PanelController, PanelStatus
from . import _win32

log = logging.getLogger("bili_danmaku.gui.panel")

#: 状态刷新间隔（毫秒）
REFRESH_MS = 2000

#: 登录态**联网**复查的最小间隔（秒）。凭证失效不紧急，但也不能一直不查 ——
#: 5 分钟一次既够及时，又不会把 `nav` 接口打得太频繁。
LOGIN_RECHECK_S = 300.0

#: 配色（与 Web 面板的深色风格大致对齐）
BG = "#1e1f22"
FG = "#e6e6e6"
MUTED = "#9aa0a6"
OK_GREEN = "#3fb950"
BAD_RED = "#f85149"
ACCENT = "#00aeec"  # B 站蓝
ENTRY_BG = "#2b2d31"

#: 登录提示条的三种状态配色（背景 / 前景）
OK_BG, OK_FG = "#12261a", "#3fb950"       # 已登录
WARN_BG, WARN_FG = "#2a2411", "#e3b341"   # 未登录 / 未校验
ALERT_BG, ALERT_FG = "#2d1416", "#f85149"  # 凭证失效（要重新扫码）


def format_uptime(seconds: float) -> str:
    """把秒数格式化成 `2h05m` / `3m20s` / `45s`。"""
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def describe_status(status: PanelStatus) -> tuple[str, str]:
    """把状态映射成 `(文本, 颜色)`，供状态行显示。"""
    if status.running:
        return "运行中", OK_GREEN
    if status.error:
        return f"未运行（{status.error[:40]}）", BAD_RED
    return "已停止", MUTED


def describe_login(status: Any, *, online: bool = False) -> tuple[str, str, str]:
    """把登录态映射成 `(配色, 文案, 级别)`。

    抽成**纯函数**是因为这里踩过一次真坑：最初把"有凭据且报了错"一律当红色，
    结果**网络不通**也显示红色"凭据失效，请重新扫码" —— 把用户赶去白折腾一趟。
    现在只有"联网**确认**过失效"才是红色（`level="alert"`）。

    级别语义：`ok`（已登录）/ `pending`（本地有凭据、还没校验）/
    `alert`（确认失效）/ `warn`（没凭据、或没能校验）。
    """
    logged = bool(getattr(status, "logged_in", False))
    checked = bool(getattr(status, "checked", False))
    message = str(getattr(status, "message", "") or "")
    uname = str(getattr(status, "uname", "") or "")
    had_credentials = bool(getattr(status, "has_sessdata", False))

    if logged and checked:
        text = f"已登录为「{uname}」" if uname else "已登录"
        if not online:
            text += "（未联网校验）"
        return OK_BG, text, "ok"
    if logged and not checked:
        # 本地有凭据但还没校验：中性提示，别吓人
        return OK_BG, "已找到凭据，正在校验…", "pending"
    if had_credentials and checked and message:
        # 有凭据 + **联网确认**失败 ⇒ 凭证失效，红色要求重新登录
        return ALERT_BG, message, "alert"
    if message:
        # 有凭据但**没能校验**（网络问题），或压根没凭据 —— 黄色
        return WARN_BG, message, "warn"
    return WARN_BG, "未登录：弹幕里的部分昵称会被平台打码（如 宅***）", "warn"


class LauncherWindow:
    """启动面板窗口。

    刻意不继承 `tk.Tk`：窗口由调用方（`tray.run_tray`）创建并持有 root，
    便于托盘统一管理生命周期。
    """

    def __init__(
        self,
        root: tk.Tk,
        controller: PanelController,
        *,
        on_quit: Callable[[], None] | None = None,
        on_hide: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.controller = controller
        self.on_quit = on_quit
        self.on_hide = on_hide
        self._warned_about_tray = False
        self._refresh_job: str | None = None
        #: 每次刷新后的回调（tray 用它同步托盘提示文字）
        self._on_refresh: Callable[[PanelStatus], None] | None = None
        #: 当前登录会话（扫码登录用；None = 没在登录）
        self._login_session: Any = None
        self._logged_once = False
        self._login_checked_at = 0.0
        #: tkinter 的 PhotoImage 必须留引用，否则会被 GC 掉、图片变空白
        self._qr_photo: Any = None

        self._build()

    # ------------------------------------------------------------------ #
    # 构建界面
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        root = self.root
        root.title("B站弹幕提取姬 · 启动面板")
        root.configure(bg=BG)
        root.resizable(False, False)
        self._set_icon(root)

        outer = tk.Frame(root, bg=BG, padx=16, pady=14)
        outer.pack(fill="both", expand=True)

        self._build_header(outer)
        self._build_login_banner(outer)
        self._build_status(outer)
        self._build_actions(outer)
        self._build_advanced(outer)
        self._build_footer(outer)

        # 关窗口 → 最小化到托盘（不是退出）
        root.protocol("WM_DELETE_WINDOW", self.hide)

    def _set_icon(self, root: tk.Tk) -> None:
        """给窗口/任务栏设置图标。

        用 tkinter 的 `iconbitmap` 读 `.ico` 是 Windows 上最省事的做法；
        失败就静默跳过（没有图标不该影响功能）。
        """
        path = _win32.find_icon()
        if path is None:
            return
        try:
            root.iconbitmap(default=str(path))
        except tk.TclError as exc:  # pragma: no cover - 取决于平台/文件
            log.debug("设置窗口图标失败：%s", exc)

    def _build_header(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")

        tk.Label(row, text="🎬", bg=BG, fg=ACCENT, font=("Segoe UI Emoji", 20)).pack(side="left")
        title_box = tk.Frame(row, bg=BG)
        title_box.pack(side="left", padx=(10, 0))
        tk.Label(
            title_box, text="B站弹幕提取姬", bg=BG, fg=FG,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box, text=f"v{__version__}", bg=BG, fg=MUTED, font=("Segoe UI", 8),
        ).pack(anchor="w")

    def _build_login_banner(self, parent: tk.Frame) -> None:
        """登录状态栏：**凭证失效时用醒目配色提示，并就地提供扫码登录**。

        为什么要放在**最上面**（而不是塞进高级设置）：未登录会让弹幕里的昵称被
        平台打码成 `宅***`，用户会以为"程序坏了"。这个提示必须显眼、且要能一步修好。

        四种状态各有一套配色与按钮组合（见 `_apply_login_status`）：
        * `unknown` 灰：还没校验（或本地就没凭据）；
        * `ok` 绿：已登录，显示昵称；
        * `warn` 黄：联网校验失败（网络问题）—— **不要**赶用户去重新登录；
        * `alert` 红：凭据确实失效/缺失 —— 这才需要重新扫码。
        """
        box = tk.Frame(parent, bg=WARN_BG, padx=10, pady=8)
        self.login_box = box

        self.login_label = tk.Label(
            box, text="正在检查登录状态…", bg=WARN_BG, fg=WARN_FG,
            font=("Microsoft YaHei UI", 9, "bold"), anchor="w", justify="left",
            wraplength=430,
        )
        self.login_label.pack(fill="x")

        row = tk.Frame(box, bg=WARN_BG)
        row.pack(fill="x", pady=(6, 0))
        self.login_row = row
        self.login_btn = self._button(row, "扫码登录", self.start_qr_login)
        self.login_btn.pack(side="left")
        self.recheck_btn = self._button(row, "重新检查", self.recheck_login)
        self.recheck_btn.pack(side="left", padx=(6, 0))
        self.login_hint = tk.Label(
            row, text="", bg=WARN_BG, fg=MUTED, font=("Segoe UI", 8), anchor="w",
        )
        self.login_hint.pack(side="left", padx=(8, 0))

        # 二维码画布（登录时才 pack）
        self.qr_canvas = tk.Label(box, bg="#ffffff", bd=0)
        self.login_box.pack(fill="x", pady=(10, 0))

    def _apply_login_status(self, status: Any, *, online: bool) -> None:
        """按登录态刷新提示条的配色/文案/按钮可用性。

        配色语义见 `describe_login()` 的说明（只有"联网确认失效"才是红色）。
        """
        bg, text, level = describe_login(status, online=online)
        fg = {"ok": OK_FG, "alert": ALERT_FG}.get(level, WARN_FG)

        for widget in (self.login_box, self.login_row, self.login_label,
                       self.login_btn, self.recheck_btn, self.login_hint):
            try:
                widget.configure(bg=bg)
            except tk.TclError:  # pragma: no cover
                pass
        self.login_label.configure(text=text, bg=bg, fg=fg)
        if self.recheck_btn.cget("bg") != bg:
            self.recheck_btn.configure(bg=bg, activebackground=bg)

    def _toggle_banner_color(self, bg: str, fg: str) -> None:
        """（未使用，保留给未来的状态色扩展。）"""

    # ------------------------------------------------------------------ #
    # 登录检查
    # ------------------------------------------------------------------ #

    def schedule_login_check(self, *, delay_ms: int = 300) -> None:
        """启动后**异步**做一次联网校验（不阻塞窗口显示）。"""
        self.root.after(delay_ms, lambda: self.check_login(online=True))

    def check_login(self, *, online: bool = True) -> None:
        """检查登录态。`online=True` 会打一次 `nav` 接口（在后台线程里跑）。"""
        if online:
            self.login_label.configure(text="正在校验登录状态…")

        def worker() -> None:
            try:
                status = self.controller.check_login() if online else self.controller.local_credential_status()
            except Exception as exc:  # noqa: BLE001 - 检查失败不该崩窗口
                from ..login import LoginStatus

                status = LoginStatus(message=f"登录检查失败：{type(exc).__name__}: {exc}")
            self.root.after(0, lambda: self._apply_login_status(status, online=online))

        threading.Thread(target=worker, name="login-check", daemon=True).start()

    def recheck_login(self) -> None:
        self._login_checked_at = 0.0
        self.check_login(online=True)

    def maybe_recheck_login(self) -> None:
        """周期性复查（由 `refresh()` 调用）。默认 5 分钟一次，避免频繁打网络。"""
        if time.time() - self._login_checked_at < LOGIN_RECHECK_S:
            return
        self._login_checked_at = time.time()
        self.check_login(online=True)

    # ------------------------------------------------------------------ #
    # 扫码登录
    # ------------------------------------------------------------------ #

    def start_qr_login(self) -> None:
        """开始扫码登录：后台申请二维码 + 轮询，主线程每 200ms 消费事件。

        会话由 `controller.new_login_session()` 建（凭据文件跟随配置），
        这样"用哪个 cookies.json"只有一处决定 —— 否则 GUI 与面板会各算一遍。
        """
        if self._login_session is not None:
            return
        session = self.controller.new_login_session()
        if not session.start():
            return
        self._login_session = session
        self.login_btn.configure(state="disabled", text="登录中…")
        self.login_hint.configure(text="正在申请二维码…")
        self.qr_canvas.pack_forget()
        self.root.after(200, self._poll_qr_login)

    def cancel_qr_login(self) -> None:
        session = self._login_session
        if session is not None:
            session.cancel()
        self._login_session = None
        self._qr_photo = None
        self.qr_canvas.pack_forget()
        self.login_btn.configure(state="normal", text="扫码登录")
        self.login_hint.configure(text="")

    def _poll_qr_login(self) -> None:
        """消费扫码事件（**主线程**里跑，所以能安全碰 tkinter）。"""
        session = self._login_session
        if session is None:
            return

        event = session.poll()
        if event is not None:
            state = event.get("state")
            if state == "qr" and session.qr_png:
                self._show_qr(session.qr_png)
                self.login_hint.configure(text="请用 B 站手机客户端扫码")
            elif state == "waiting":
                self.login_hint.configure(text=str(event.get("text") or "等待扫码…"))
            elif state == "scanned":
                self.login_hint.configure(text="已扫码，请在手机上确认")
            elif state == "expired":
                self.login_hint.configure(text="二维码已失效，正在换一张…")
                self.qr_canvas.pack_forget()
            elif state == "ok":
                self.login_hint.configure(text=f"登录成功，凭据已保存")
                self.cancel_qr_login()
                self.check_login(online=True)
                return
            elif state in ("failed", "cancelled"):
                detail = str(event.get("message") or "已取消")
                self.login_hint.configure(text=detail)
                self.cancel_qr_login()
                return

        if self._login_session is not None:
            self.root.after(200, self._poll_qr_login)

    def _show_qr(self, png_bytes: bytes) -> None:
        """把二维码 PNG 显示在面板上（tkinter 原生只吃 GIF/PNG/BMP，正好）。"""
        try:
            import io

            photo = tk.PhotoImage(data=base64.b64encode(png_bytes).decode("ascii"))
        except Exception:  # noqa: BLE001 - 显示不了就退回"用链接打开"
            self.login_hint.configure(text="二维码渲染失败，请点「重新检查」后重试")
            return
        self._qr_photo = photo  # 必须留引用
        self.qr_canvas.configure(image=photo)
        self.qr_canvas.pack(pady=(8, 0))

    def _build_status(self, parent: tk.Frame) -> None:
        box = tk.Frame(parent, bg=ENTRY_BG, padx=12, pady=10)
        box.pack(fill="x", pady=(12, 10))

        line = tk.Frame(box, bg=ENTRY_BG)
        line.pack(fill="x")
        self.status_dot = tk.Label(line, text="●", bg=ENTRY_BG, fg=MUTED, font=("Segoe UI", 11))
        self.status_dot.pack(side="left")
        self.status_text = tk.Label(
            line, text="已停止", bg=ENTRY_BG, fg=FG, font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.status_text.pack(side="left", padx=(6, 0))
        self.uptime_text = tk.Label(line, text="", bg=ENTRY_BG, fg=MUTED, font=("Segoe UI", 9))
        self.uptime_text.pack(side="right")

        # 地址 = 跳转 Web 端的链接入口（本阶段的主要用途）
        self.url_label = tk.Label(
            box, text="未启动", bg=ENTRY_BG, fg=ACCENT,
            font=("Consolas", 10, "underline"), cursor="hand2", anchor="w",
        )
        self.url_label.pack(fill="x", pady=(6, 0))
        self.url_label.bind("<Button-1>", lambda _event: self.open_panel())

        self.meta_text = tk.Label(
            box, text="", bg=ENTRY_BG, fg=MUTED, font=("Segoe UI", 9), anchor="w",
        )
        self.meta_text.pack(fill="x", pady=(4, 0))

    def _build_actions(self, parent: tk.Frame) -> None:
        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.open_btn = self._button(grid, "打开面板", self.open_panel, primary=True)
        self.open_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 6))
        self.copy_btn = self._button(grid, "复制链接", self.copy_link)
        self.copy_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 6))

        self.toggle_btn = self._button(grid, "启动采集", self.toggle_collect, primary=True)
        self.toggle_btn.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(0, 6))
        self.restart_btn = self._button(grid, "重启", self.restart)
        self.restart_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(0, 6))

        self.folder_btn = self._button(grid, "打开数据目录", self.open_data_dir)
        self.folder_btn.grid(row=2, column=0, sticky="ew", padx=(0, 4))

    def _button(
        self,
        parent: tk.Frame,
        text: str,
        command: Callable[[], None],
        *,
        primary: bool = False,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=ACCENT if primary else ENTRY_BG,
            fg="#ffffff" if primary else FG,
            activebackground=ACCENT if primary else "#3a3d42",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=10,
            pady=7,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
            disabledforeground="#6b6f75",
        )

    def _build_advanced(self, parent: tk.Frame) -> None:
        """「高级设置」默认折叠 —— 只放房间号，其他留待未来。"""
        self.advanced_open = tk.BooleanVar(value=False)
        self.adv_toggle = tk.Button(
            parent, text="▸ 高级设置", command=self._toggle_advanced,
            bg=BG, fg=MUTED, activebackground=BG, activeforeground=FG,
            relief="flat", bd=0, anchor="w", font=("Microsoft YaHei UI", 9),
            cursor="hand2", padx=0,
        )
        self.adv_toggle.pack(fill="x", pady=(10, 0))

        self.adv_body = tk.Frame(parent, bg=BG)
        row = tk.Frame(self.adv_body, bg=BG)
        row.pack(fill="x", pady=(6, 2))
        tk.Label(row, text="房间号", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.rooms_var = tk.StringVar(value=",".join(self.controller.options.rooms))
        tk.Entry(
            row, textvariable=self.rooms_var, bg=ENTRY_BG, fg=FG, insertbackground=FG,
            relief="flat", font=("Consolas", 9), width=22,
        ).pack(side="left", padx=(8, 0), ipady=3)
        tk.Label(
            self.adv_body, text="多个房间用逗号分隔；留空则启动空面板（可在网页里添加）",
            bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w", wraplength=400,
        ).pack(fill="x")

    def _build_footer(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(14, 0))
        tk.Label(
            row, text="关闭本窗口不会退出，程序仍在托盘运行",
            bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        tk.Button(
            row, text="退出", command=self.quit_app, bg=BG, fg=MUTED,
            activebackground=BG, activeforeground=BAD_RED, relief="flat", bd=0,
            font=("Microsoft YaHei UI", 9), cursor="hand2",
        ).pack(side="right")

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #

    def _toggle_advanced(self) -> None:
        if self.advanced_open.get():
            self.adv_body.pack_forget()
            self.adv_toggle.configure(text="▸ 高级设置")
            self.advanced_open.set(False)
        else:
            self.adv_body.pack(fill="x")
            self.adv_toggle.configure(text="▾ 高级设置")
            self.advanced_open.set(True)

    def show(self) -> None:
        """显示并置顶窗口。"""
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:  # pragma: no cover
            pass

    def hide(self) -> None:
        """隐藏到托盘（首次隐藏时提示一次）。"""
        self.root.withdraw()
        if callable(self.on_hide):
            self.on_hide()
        if not self._warned_about_tray:
            self._warned_about_tray = True
            # 不强弹 messagebox（会打断用户）；改在状态行提示
            self.status_text.configure(text="仍在托盘运行")

    def open_panel(self) -> None:
        """在浏览器打开 Web 面板（未运行时先启动）。"""
        status = self.controller.status()
        if not status.running:
            self.toggle_collect()
            status = self.controller.status()
        if status.running and status.url:
            webbrowser.open(status.url)
        else:
            messagebox.showwarning("面板未运行", "面板没有成功启动，请查看日志。", parent=self.root)

    def copy_link(self) -> None:
        url = self.controller.status().url
        if not url:
            messagebox.showinfo("还没有链接", "面板未运行，先点「启动采集」。", parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.status_text.configure(text="链接已复制")

    def _parse_rooms(self) -> tuple[str, ...] | None:
        """解析房间号输入；**校验失败返回 None 并提示**（识别不出就不猜）。"""
        raw = (self.rooms_var.get() or "").strip()
        if not raw:
            return ()
        from ..rooms import parse_room_input

        rooms: list[str] = []
        for piece in raw.replace("，", ",").split(","):
            item = piece.strip()
            if not item:
                continue
            try:
                rooms.append(str(parse_room_input(item)))
            except ValueError as exc:
                messagebox.showerror(
                    "房间号无法识别", f"「{item}」不是有效的房间号或直播间链接。\n\n{exc}",
                    parent=self.root,
                )
                return None
        return tuple(rooms)

    def toggle_collect(self) -> None:
        """启动/停止采集。"""
        if self.controller.is_running():
            self.controller.stop()
            self.refresh()
            return

        rooms = self._parse_rooms()
        if rooms is None:
            return
        self.controller.options.rooms = rooms
        status = self.controller.start(wait=True, timeout=25)
        if not status.running:
            messagebox.showerror(
                "启动失败", status.error or "未知错误，请查看日志。", parent=self.root
            )
        self.refresh()

    def restart(self) -> None:
        if self.controller.is_running():
            self.controller.stop()
        self.toggle_collect()

    def open_data_dir(self) -> None:
        """用资源管理器打开落盘目录。"""
        import os
        import subprocess

        target = os.path.abspath(self.controller.options.out_dir)
        if not os.path.isdir(target):
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("打不开目录", f"{target}\n\n{exc}", parent=self.root)
                return
        try:
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606 - Windows 专用
        except (OSError, AttributeError):
            subprocess.Popen(["explorer", target])

    def quit_app(self) -> None:
        """退出：二次确认 → 停采集（优雅 flush）→ 交给 tray 收尾。"""
        if not messagebox.askyesno(
            "退出", "停止采集并退出程序？\n\n采集数据会先落盘再退出。", parent=self.root
        ):
            return
        if callable(self.on_quit):
            self.on_quit()

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #

    def refresh(self) -> PanelStatus:
        """刷新界面状态；返回本次快照。"""
        status = self.controller.status()
        text, color = describe_status(status)
        self.status_dot.configure(fg=color)
        self.status_text.configure(text=text, fg=FG if status.running else MUTED)
        self.uptime_text.configure(
            text=f"已运行 {format_uptime(status.uptime_s)}" if status.running else ""
        )

        if status.running and status.url:
            self.url_label.configure(text=status.url, fg=ACCENT, cursor="hand2")
        else:
            self.url_label.configure(text="未启动", fg=MUTED, cursor="arrow")

        if status.running:
            rooms = "、".join(status.rooms) if status.rooms else "无（可在网页添加）"
            self.meta_text.configure(
                text=f"房间 {rooms}　收包 {status.published}　丢弃 {status.dropped}"
            )
        elif status.error:
            self.meta_text.configure(text=status.error[:80])
        else:
            self.meta_text.configure(text="")

        self.open_btn.configure(state="normal")
        self.copy_btn.configure(state="normal" if status.running else "disabled")
        self.toggle_btn.configure(text="停止采集" if status.running else "启动采集")
        self.restart_btn.configure(state="normal" if status.running else "disabled")
        if not status.frontend_built:
            self.meta_text.configure(text="⚠ 前端产物缺失，网页会是降级页")

        # 首屏先做一次**离线**判断（立刻有说法），随后异步补一次联网校验；
        # 之后每 `LOGIN_RECHECK_S` 复查一次（凭证会过期/被踢）。
        if not self._logged_once:
            self._logged_once = True
            self._apply_login_status(
                self.controller.local_credential_status(), online=False
            )
            self.check_login(online=True)
        else:
            self.maybe_recheck_login()
        return status

    def schedule_refresh(self) -> None:
        """启动周期性刷新（每 `REFRESH_MS`）。"""
        status = self.refresh()
        if callable(self._on_refresh):
            self._on_refresh(status)
        self._refresh_job = self.root.after(REFRESH_MS, self.schedule_refresh)

    def stop_refresh(self) -> None:
        if self._refresh_job is not None:
            try:
                self.root.after_cancel(self._refresh_job)
            except tk.TclError:  # pragma: no cover
                pass
            self._refresh_job = None

    def set_refresh_hook(self, hook: Callable[[PanelStatus], None]) -> None:
        self._on_refresh = hook
