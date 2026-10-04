"""验证 exe 里的**登录提示条**：四种状态都要显示对，且扫码登录可用。

需求："在启动面板添加扫码登录获取用户凭证这一功能，当凭证失效时，
在启动面板用较为明显的信息栏提示用户登录"。

`--windowed` 的 exe 看不到界面，所以这里直接驱动**面板窗口的逻辑**
（与 exe 内是同一份代码），并验证：
  1. 有凭据 → 提示条是绿色的"已登录"；
  2. 凭据失效 → 提示条变**红色**、文案要求重新登录、按钮可用；
  3. 没凭据 → 黄色提示；
  4. 「扫码登录」弹出**独立窗口**，二维码在**那个窗口**里渲染，且窗口尺寸
     **一定装得下**整张码（这一步真打一次接口，需要网络；连不上时只告警不判死）。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.credentials import png_size, render_qr_png
from bili_danmaku.gui.panel_window import (
    ALERT_BG,
    OK_BG,
    WARN_BG,
    LauncherWindow,
)
from bili_danmaku.gui.qr_window import QR_PADDING_PX, qr_area_px
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
    print("5. 二维码弹窗的尺寸：**一定装得下整张码**（纯函数，不需要网络）")
    print("=" * 84)
    # 先验尺寸承诺，它比"能不能联网"重要得多 —— 扫不出来的根因就在这里。
    for side in (240, 296, 400, 600):
        area = qr_area_px((side, side))
        check(
            f"图 {side}px → 窗口 {area}px（每边留白 ≥ {QR_PADDING_PX}）",
            area > side + 2 * QR_PADDING_PX or area >= 720,
            f"area={area}",
        )
    check("量不到图片尺寸时给保守值（不返回 0）", qr_area_px(None) >= 320, str(qr_area_px(None)))
    check("极端大图也不超过上限 720（否则窗口会跑到屏幕外）", qr_area_px((2000, 2000)) == 720)

    rendered = render_qr_png("https://example.invalid/qr", box_size=8, border=2)
    if rendered:
        size = png_size(rendered)
        check("能从真 PNG 的头部读出尺寸", size is not None, str(size))
        if size:
            area = qr_area_px(size)
            check(
                f"真二维码 {size[0]}×{size[1]} 装得进 {area}px 的窗口",
                area >= max(size),
                f"area={area} vs 图 {max(size)}",
            )
    else:
        print("     （跳过：这台机器上没有 qrcode/Pillow，无法生成 PNG）")

    print()
    print("=" * 84)
    print("6. 真机：扫码登录弹出**独立窗口**（需要网络）")
    print("=" * 84)
    controller = PanelController(LauncherOptions(port=0, out_dir="build/_loginprobe"))
    real = LauncherWindow(root, controller)
    try:
        real.start_qr_login()
        window_obj = real._qr_window
        check("扫码登录会建出独立的二维码窗口", window_obj is not None)
        if window_obj is not None:
            check("独立窗口不是启动面板本身", window_obj.win is not root)
            check("面板里**不再**有二维码画布", not hasattr(real, "qr_canvas"))

            import time

            deadline = time.time() + 25
            while time.time() < deadline and not window_obj.closed and window_obj._shown_png is None:
                root.update()
                root.update_idletasks()
                time.sleep(0.2)

            hint = str(window_obj.status_label.cget("text"))
            has_qr = window_obj._shown_png is not None
            print(f"     弹窗状态 = {hint!r}  二维码已渲染 = {has_qr}")
            check("拿到了二维码，或给出了明确的失败说明", has_qr or bool(hint))
            if has_qr:
                # ⚠️ 先 `update()` 再读几何：Tk 的 `geometry()` 请求要等一次
                # 事件处理才落到 `winfo_width()` 上，不刷新会读到默认的 `1x1`
                # （这不是窗口真的只有 1px）。
                root.update()
                size = png_size(window_obj._shown_png)
                label = window_obj.image_label
                need_w = label.winfo_reqwidth()
                need_h = label.winfo_reqheight()
                print(f"     二维码 {size}  label 需要 {need_w}×{need_h}")
                print(f"     弹窗 geometry={window_obj.win.winfo_geometry()} "
                      f"内容区={window_obj.win.winfo_width()}x{window_obj.win.winfo_height()}")
                if size:
                    # 判定用**图片控件的请求尺寸**（它已经含 Tk 的 DPI 缩放），
                    # 比窗口外框更贴近"装不装得下"这件事
                    check(
                        "图片控件按图片真实大小排布（含 DPI 缩放）",
                        need_w >= size[0] and need_h >= size[1],
                        f"label {need_w}×{need_h} vs 图 {size}",
                    )
                    check(
                        "窗口内容区装得下图片控件 + 两侧留白",
                        window_obj.win.winfo_width() >= need_w + 2 * QR_PADDING_PX - 2,
                        f"内容区 {window_obj.win.winfo_width()} vs 需要 {need_w + 2 * QR_PADDING_PX}",
                    )
            else:
                print("     （没有二维码 —— 可能是网络不通；不算失败，但请人工确认）")
            real.cancel_qr_login()
        check("取消后面板按钮恢复可点", str(real.login_btn.cget("state")) == "normal")
        check("取消后会话与窗口引用都清干净", real._login_session is None and real._qr_window is None)
    finally:
        pass

    print()
    print("=" * 84)
    print("7. 登录成功后**自动关窗**（把会话换成替身，不需要真扫码）")
    print("=" * 84)
    # 真扫码没法自动化（要人拿手机），但"拿到 ok 之后会发生什么"必须验到：
    # 自动关窗 → 面板按钮恢复 → 提示条变绿。这一段是最容易只写一半的地方
    # （只关窗不刷新，用户会以为没成功）。
    auto = LauncherWindow(root, controller)

    class _FakeSession:
        """假会话：面板收尾时只会调 `cancel()`。"""

        def __init__(self) -> None:
            self.qr_png = None
            self.running = True
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    class _FakeQrWindow:
        """假弹窗：只保留面板真正会用的成员（避免在无桌面下建真窗口）。"""

        def __init__(self, _master, session, *, on_closed=None) -> None:
            self.session = session
            self.on_closed = on_closed
            self.closed = False
            self.outcome = ""
            self.win = root

        def close(self) -> None:
            if self.closed:
                return
            self.closed = True
            if self.on_closed is not None:
                callback, self.on_closed = self.on_closed, None
                callback()

    import bili_danmaku.gui.panel_window as panel_module

    original = panel_module.QrLoginWindow
    panel_module.QrLoginWindow = _FakeQrWindow
    try:
        fake_session = _FakeSession()
        auto._login_session = fake_session
        window_obj = _FakeQrWindow(root, fake_session, on_closed=auto._on_qr_window_closed)
        auto._qr_window = window_obj
        auto.login_btn.configure(state="disabled", text="登录中…")
        # 模拟"会话回了 ok"：真弹窗在这条路径上会先记 outcome 再自己关
        window_obj.outcome = "ok"
        window_obj.close()
        root.update_idletasks()

        check("登录成功后弹窗自动关闭", window_obj.closed is True)
        check("面板按钮恢复可点", str(auto.login_btn.cget("state")) == "normal")
        check("提示条报告成功", "成功" in str(auto.login_hint.cget("text")),
              str(auto.login_hint.cget("text")))
        check("会话与窗口引用清干净", auto._login_session is None and auto._qr_window is None)
        check("成功时**没有**把会话当成取消（outcome 仍是 ok）", window_obj.outcome == "ok")
    finally:
        panel_module.QrLoginWindow = original
        auto.shutdown()
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
print("✅ 登录提示条四种状态正确，扫码登录弹独立二维码窗口且装得下整张码")
print("=" * 84)
