"""托盘应用冒烟测试：**不进入 mainloop**，只把生命周期走一遍。

能验到的：
  * TrayApp 能否构建（tkinter 窗口 + 启动面板 + 托盘消息窗口）；
  * 托盘图标能否真的挂上（本环境有真实桌面，应成功）；
  * 状态刷新、托盘提示更新、消息泵能否正常跑；
  * 退出路径能否**优雅停面板**并清理托盘。

验不到的：真人点击菜单、滚动、视觉排版 —— 那些只能手测。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tkinter as tk  # noqa: E402

from bili_danmaku.gui.tray import PUMP_MS, TrayApp, build_menu_items  # noqa: E402
from bili_danmaku.launcher import LauncherOptions, PanelController  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print("=" * 78)
print("1. 构建 TrayApp（真实 Tk 窗口 + 托盘消息窗口）")
print("=" * 78)

data_dir = ROOT / "build" / "tray_smoke_data"
options = LauncherOptions(host="127.0.0.1", port=0, out_dir=str(data_dir), rooms=(), open_browser=False)
controller = PanelController(options)
app = None
try:
    app = TrayApp(controller)
    check("TrayApp 构建", True)

    # 挂载托盘图标（`show()` 才会创建消息窗口 —— 先挂载再断言 hwnd）
    shown = app.tray.show()
    check("Shell_NotifyIconW 挂载托盘图标", shown, "" if shown else "（可能无系统托盘区）")
    check("托盘消息窗口已创建", app.tray.hwnd != 0, f"hwnd={app.tray.hwnd}")
    print(f"     提示文字 = {app.tray.tooltip!r}")

    app._wire_tray()

    print()
    print("=" * 78)
    print("2. 状态刷新 + 托盘提示（未运行 → 运行中 → 停止）")
    print("=" * 78)
    status = app.window.refresh()
    check("未运行时 refresh", status.running is False)
    check("托盘提示=已停止", "已停止" in app.tray.tooltip, repr(app.tray.tooltip))

    started = controller.start(wait=True, timeout=25)
    check("面板在托盘进程内启动", started.running, started.error)
    if started.running:
        print(f"     URL = {started.url}")

    app.window.refresh()
    app._on_refresh(controller.status())
    check("运行中 refresh", controller.status().running is True)
    check("托盘提示含运行时长", "运行中" in app.tray.tooltip, repr(app.tray.tooltip))

    print()
    print("=" * 78)
    print("3. HTTP 可达（面板确实在本进程里服务）")
    print("=" * 78)
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{started.url}api/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        check("GET /api/health", response.status == 200)
        check("frontend_built=True", payload.get("frontend_built") is True)
        check("content_only=True（默认只采文字弹幕）", payload.get("content_only") is True)
    except Exception as exc:
        check("GET /api/health", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=" * 78)
    print("4. 菜单构建（真实菜单项 + 真实 HMENU）")
    print("=" * 78)
    st = controller.status()
    items = build_menu_items(
        st, on_open=lambda: None, on_copy=lambda: None, on_toggle=lambda: None,
        on_restart=lambda: None, on_show=lambda: None, on_logs=lambda: None,
        on_folder=lambda: None, on_quit=lambda: None,
    )
    labels = [i.label for i in items if not i.separator]
    print(f"     菜单项={labels}")
    check("菜单含状态行", any("运行中" in x for x in labels))
    check("菜单含地址行", any("http" in x for x in labels))
    check("菜单含停止采集", "停止采集" in labels)

    print()
    print("=" * 78)
    print("5. 消息泵 + 退出路径")
    print("=" * 78)
    pumped = 0
    for _ in range(5):
        app.root.update_idletasks()
        app.root.update()
        pumped += app.tray.pump()
    check("消息泵可运行", pumped >= 0, f"处理 {pumped} 条消息")

    app.quit()
    check("退出后控制器已停止", controller.is_running() is False)
    check("退出后托盘已移除", app.tray.hwnd == 0, f"hwnd={app.tray.hwnd}")
    check("退出后消息泵停止（_quitting）", app._quitting is True)
finally:
    try:
        if controller.is_running():
            controller.stop()
    except Exception as exc:  # pragma: no cover
        print(f"  清理时停止面板失败：{exc}")
    try:
        if app is not None:
            app.root.destroy()
    except Exception:
        pass
    import shutil

    shutil.rmtree(data_dir, ignore_errors=True)

print()
print("=" * 78)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 托盘应用冒烟测试全部通过")
print("=" * 78)
