"""验证 exe 里的**登录提示条**：四种状态都要显示对，且扫码登录可用。

需求："在启动面板添加扫码登录获取用户凭证这一功能，当凭证失效时，
在启动面板用较为明显的信息栏提示用户登录"。

`--windowed` 的 exe 看不到界面，所以这里直接驱动**面板窗口的逻辑**
（与 exe 内是同一份代码），并验证：
  1. 有凭据 → 提示条是绿色的"已登录"；
  2. 凭据失效 → 提示条变**红色**、文案要求重新登录、按钮可用；
  3. 没凭据 → 黄色提示；
  4. 「扫码登录」能拿到二维码并渲染成 PNG 显示在窗口上（**真打一次接口**，
     所以这一步需要网络；失败时只警告不判死）。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.gui.panel_window import (
    ALERT_BG,
    OK_BG,
    WARN_BG,
    LauncherWindow,
)
from bili_danmaku.launcher import LauncherOptions, LoginStatus, PanelController

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class _StubController:
    """最小替身：只提供窗口构造与登录检查需要的两个方法。"""

    def __init__(self, status: LoginStatus) -> None:
        self.options = LauncherOptions(port=0, out_dir="build/_loginprobe")
        self._status = status

    def status(self):  # noqa: ANN201
        from bili_danmaku.launcher import PanelStatus

        return PanelStatus(running=False)

    def local_credential_status(self) -> LoginStatus:
        return self._status

    def check_login(self, **_kwargs) -> LoginStatus:
        return self._status

    def is_running(self) -> bool:
        return False


root = tk.Tk()
root.withdraw()
window = None
try:
    print("=" * 84)
    print("1. 有凭据（已登录）→ 绿色")
    print("=" * 84)
    window = LauncherWindow(
        root,
        _StubController(  # type: ignore[arg-type]
            LoginStatus(logged_in=True, source="file", has_sessdata=True, checked=True, uname="歪比巴布325")
        ),
    )
    window._apply_login_status(window.controller.check_login(), online=True)
    root.update_idletasks()
    text = window.login_label.cget("text")
    check("提示条是绿色", window.login_box.cget("bg") == OK_BG, window.login_box.cget("bg"))
    check("文案含昵称", "歪比巴布325" in text, text)

    print()
    print("=" * 84)
    print("2. 凭据失效 → 红色 + 要求重新登录（需求的核心场景）")
    print("=" * 84)
    window._apply_login_status(
        LoginStatus(
            logged_in=False,
            has_sessdata=True,
            checked=True,
            message="登录已失效（凭据过期或被踢），请重新扫码登录",
        ),
        online=True,
    )
    root.update_idletasks()
    text = window.login_label.cget("text")
    check("提示条是红色", window.login_box.cget("bg") == ALERT_BG, window.login_box.cget("bg"))
    check("文案说明失效", "失效" in text, text)
    check("文案要求重新登录", "重新" in text or "扫码" in text, text)
    check("扫码登录按钮可用", str(window.login_btn.cget("state")) == "normal")
    check("按钮文案是「扫码登录」", window.login_btn.cget("text") == "扫码登录")

    print()
    print("=" * 84)
    print("3. 没有凭据 → 黄色提示（引导式，不吓人）")
    print("=" * 84)
    window._apply_login_status(
        LoginStatus(
            logged_in=False,
            has_sessdata=False,
            message="未登录（未找到凭据，部分昵称会被平台打码）",
        ),
        online=False,
    )
    root.update_idletasks()
    check("提示条是黄色", window.login_box.cget("bg") == WARN_BG, window.login_box.cget("bg"))
    check("文案提到打码", "打码" in window.login_label.cget("text"))

    print()
    print("=" * 84)
    print("3b. 有凭据但网络不通 → 黄色 + 网络说明（**不**赶用户去重新登录）")
    print("=" * 84)
    window._apply_login_status(
        LoginStatus(
            logged_in=False,
            has_sessdata=True,
            checked=False,
            message="无法校验登录态（网络问题）：OSError",
        ),
        online=True,
    )
    root.update_idletasks()
    check("提示条是黄色（不是红色）", window.login_box.cget("bg") == WARN_BG, window.login_box.cget("bg"))
    check("文案说是网络问题", "网络" in window.login_label.cget("text"))

    print()
    print("=" * 84)
    print("4. 提示条位置在**状态区之上**（醒目）")
    print("=" * 84)
    order = [child for child in window.login_box.master.winfo_children()]
    names = [str(child) for child in order]
    login_index = names.index(str(window.login_box))
    status_index = names.index(str(window.status_dot.master.master))
    check("登录条排在状态区之前", login_index < status_index, f"login={login_index} status={status_index}")

    print()
    print("=" * 84)
    print("5. 真机：扫码登录能拿到二维码（需要网络）")
    print("=" * 84)
    controller = PanelController(LauncherOptions(port=0, out_dir="build/_loginprobe"))
    real = LauncherWindow(root, controller)
    try:
        real.start_qr_login()
        import time

        deadline = time.time() + 25
        while time.time() < deadline and real._login_session is not None:
            root.update()
            root.update_idletasks()
            time.sleep(0.2)
            if real.login_hint.cget("text"):
                pass
        hint = real.login_hint.cget("text")
        has_qr = real._qr_photo is not None
        print(f"     状态提示 = {hint!r}  二维码已渲染 = {has_qr}")
        if real.qr_or_none() if hasattr(real, "qr_or_none") else has_qr:
            pass
        check("拿到了二维码或明确的失败说明", has_qr or bool(hint))
        if not has_qr:
            print("     （没有二维码 —— 可能是网络不通；不算失败，但请人工确认）")
        real.cancel_qr_login()
    finally:
        pass
finally:
    try:
        if window is not None:
            window.stop_refresh()
        root.destroy()
    except Exception:
        pass

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 登录提示条四种状态正确，扫码登录可触发")
print("=" * 84)
