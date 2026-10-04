"""在**真实桌面**上确认二维码弹窗真的出现、且尺寸装得下整张码。

## 为什么还要这一步

`tests/test_gui.py` 与 `tools/verify_login_banner.py` 已经覆盖了"尺寸算得对"
和"事件处理对"，但两者都**看不到真窗口**：一个是纯函数/替身，另一个读的是
Tk 自己报告的数字。而这次的缺陷恰恰是**看起来对、实际显示不下** ——
所以必须有一个"窗口真的在屏幕上、标题对、内容区真的够大"的证据。

这里用 ctypes 枚举顶层窗口（`EnumWindows`），找标题含「扫码登录」的那个，
读它的客户区尺寸。**不需要点鼠标**，也不会抢焦点。

用法（需要真实桌面 + 网络）::

    .\\.venv\\Scripts\\python.exe tools\\verify_qr_window_desktop.py --exe dist\\exe\\B站弹幕提取姬.exe
    .\\.venv\\Scripts\\python.exe tools\\verify_qr_window_desktop.py            # 用 .venv 里的 CLI
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.credentials import png_size  # noqa: E402
from bili_danmaku.gui.qr_window import QR_PADDING_PX, qr_area_px  # noqa: E402
from bili_danmaku.credentials import render_qr_png  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)


def _make_process_dpi_aware() -> str:
    """把这个进程标成 DPI 感知，返回结果描述。

    **不做这一步读数会是假的**：非 DPI 感知的进程拿到的窗口尺寸会被 Windows
    按系统缩放**虚拟化放大**（实测 Tk 报 `585x585`，而 `GetClientRect` 报
    `800x800` —— 比值正是本机的 1.3346 缩放）。那样"装不装得下"就没法判断了。
    """
    # Windows 10 1703+ 的 per-monitor v2（值 -4）
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor v2"
    except AttributeError:  # pragma: no cover - 老系统没这个导出
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:  # noqa: BLE001 - 尽力而为
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:  # noqa: BLE001
        pass
    return "失败（读数会偏大）"

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _windows_with_title(needle: str) -> list[tuple[int, str, int, int]]:
    """枚举可见的顶层窗口，返回标题含 `needle` 的 `(hwnd, title, 宽, 高)`。

    只统计**可见**窗口：Tk 在 `withdraw()` 期间会建出 1×1 的隐藏窗口，
    把它们算进来会得出"窗口太小"的错误结论（这个坑在本轮真踩过）。
    """
    found: list[tuple[int, str, int, int]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if needle not in title:
            return True
        rect = wintypes.RECT()
        # 客户区尺寸：窗口边框不算，"装不装得下"要看内容区
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            print(
                f"     [枚举] hwnd={hwnd} pid={pid.value} "
                f"client={rect.right - rect.left}×{rect.bottom - rect.top} title={title!r}"
            )
            found.append((hwnd, title, rect.right - rect.left, rect.bottom - rect.top))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="真桌面上验收二维码弹窗")
    parser.add_argument("--exe", default="", help="要启动的 exe；留空则用 .venv 的 CLI")
    parser.add_argument("--port", type=int, default=8797, help="面板端口")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待二维码的秒数")
    args = parser.parse_args()

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if (detail and not ok) else ''}")
        if not ok:
            failures.append(label)

    print("=" * 84)
    print("真实桌面：扫码登录的二维码弹窗")
    print("=" * 84)
    print(f"  本进程 DPI 感知：{_make_process_dpi_aware()}")

    # 先算出"这次应该需要多大"——用与弹窗同一套逻辑，避免两边标准不一致
    sample = render_qr_png("https://passport.bilibili.com/h5-app/passport/login/scan?x=1")
    size = png_size(sample)
    need = qr_area_px(size)
    print(f"  参照二维码 {size} → 按同一套公式需要的窗口边长 ≈ {need}px")
    print(f"  （实际二维码内容更长，尺寸只会更大；按 {QR_PADDING_PX}px/边留白算）")

    command = (
        [args.exe, "web", "--port", str(args.port)]
        if args.exe
        else [sys.executable, "-m", "bili_danmaku", "tray", "--port", str(args.port)]
    )
    probe_path = ROOT / "build" / "qr_window_probe.txt"
    probe_path.unlink(missing_ok=True)
    runner = ROOT / "tools" / "_desktop_qr_runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")

    import subprocess

    process = subprocess.Popen(
        [sys.executable, str(runner), *command],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    # 子进程的输出也落一份：失败时"没找到窗口"可能是网络不通，也可能是触发逻辑
    # 自己抛了异常 —— 不留下证据就只能靠猜（这里踩过一次）。
    log_path = ROOT / "build" / "qr_window_runner.log"

    try:
        found: list[tuple[int, str, int, int]] = []
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            found = _windows_with_title("扫码登录")
            if found:
                break
            if process.poll() is not None:
                break
            time.sleep(0.5)

        check(
            "屏幕上出现了标题含「扫码登录」的窗口（且可见）",
            bool(found),
            "没找到 —— 可能是网络不通拿不到二维码，或者窗口没被映射",
        )
        if found:
            hwnd, title, width, height = found[0]
            # 等子进程把"弹窗自己算出来的尺寸"写出来再收工（它在拿到二维码后
            # 还要多等一拍才写），否则读到的会是上一次运行留下的旧文件
            for _ in range(20):
                if probe_path.is_file():
                    break
                time.sleep(0.5)

            probe = {}
            if probe_path.is_file():
                raw = probe_path.read_text(encoding="utf-8").strip()
                print(f"     {raw}")
                probe = dict(
                    item.split("=", 1) for item in raw.split() if "=" in item
                )

            # 第 1 条：**布局层面**装得下 —— 二维码控件的请求尺寸 ≥ 图片本身，
            # 且窗口按"控件 + 两侧留白"来定尺寸。这是"一定完整显示"的本体。
            image_px = int(probe.get("image", 0) or 0)
            measure_px = int(probe.get("measure", 0) or 0)
            qr_px = int(probe.get("qr", 0) or 0)
            check(
                "二维码控件按图片真实大小排布（含 DPI 缩放）",
                image_px >= qr_px > 0,
                f"控件 {image_px} vs 图 {qr_px}",
            )
            check(
                f"窗口尺寸 = 控件 + 两侧留白（{measure_px} ≥ {qr_px + 2 * QR_PADDING_PX}）",
                measure_px >= qr_px + 2 * QR_PADDING_PX,
                f"measure={measure_px}",
            )

            # 第 2 条：**屏幕上真的够大**。这里刻意把标尺写清楚：
            # 枚举者与子进程的 DPI 上下文不同，两个数不能直接比 ——
            # 所以只要求"实际客户区 ≥ 设计要求"，并要求是整数倍量级（缩放）。
            if measure_px:
                ratio = height / measure_px
                check(
                    f"实际客户区 {height}px ≥ 设计要求 {measure_px}px",
                    height >= measure_px,
                    f"实际 {height}px",
                )
                check(
                    f"两个读数只差 DPI 缩放（比值 {ratio:.2f}，应在 0.8–2.5）",
                    0.8 <= ratio <= 2.5,
                    f"比值 {ratio:.2f} —— 差了这么多说明量的可能不是同一个窗口",
                )
            check("窗口标题写明了用途", "扫码" in title and "登录" in title)
            check("客户区是正方形（二维码是方的，边长应当一致）", abs(width - height) <= 8,
                  f"{width}×{height}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        try:
            log_path.write_text(process.stdout.read() if process.stdout else "", encoding="utf-8")
        except Exception:  # pragma: no cover - 诊断用，失败不影响判定
            pass
        runner.unlink(missing_ok=True)

    print()
    print("=" * 84)
    if failures:
        print(f"❌ 失败 {len(failures)} 项：{failures}")
        return 1
    print("✅ 真桌面上二维码弹窗真的出现，且客户区装得下整张码")
    print("=" * 84)
    return 0


#: 启动托盘并**自动点一下「扫码登录」**：不需要人操作就能把弹窗弄出来。
#:
#: 用脚本而不是让用户手点，是为了让这个验收能被反复跑。它做三件事：
#: 起托盘（`run_tray` 会在后台线程里跑）、等窗口建好、调用 `start_qr_login()`。
_RUNNER = '''\
"""由 tools/verify_qr_window_desktop.py 生成，不要手改。"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.gui import tray as tray_module
from bili_danmaku.gui.qr_window import QR_MAX_AREA_PX, QR_PADDING_PX

_original = tray_module.LauncherWindow


class _AutoQrLauncherWindow(_original):
    """建好窗口后自动触发一次扫码登录，其余行为与原来一致。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def _trigger():
            time.sleep(1.5)
            try:
                self.root.after(0, self.start_qr_login)
                # ⚠️ 要等**真的拿到二维码**再记尺寸：在那之前窗口只有"标题 + 按钮"
                # 那么高，是个偏小的形状；采样太早会把过渡态当成最终结果。
                window = None
                for _ in range(180):
                    time.sleep(0.5)
                    window = self._qr_window
                    if window is not None and getattr(window, "_shown_png", None) is not None:
                        # 再等一拍，让 `_fit()` 的 geometry 落到 `winfo_*` 上
                        time.sleep(1.0)
                        break
                if window is not None:
                    # 同**同一时刻**用两种方式量同一个窗口：Tk 报的尺寸与
                    # Win32 `GetClientRect` 报的尺寸。两者不一致时才知道该信谁
                    # （实测踩过：两个进程的 DPI 上下文不同，读数差一个缩放假）。
                    import ctypes as _ctypes
                    from ctypes import wintypes as _wintypes

                    from bili_danmaku.credentials import png_size as _png_size

                    _user32 = _ctypes.WinDLL("user32", use_last_error=True)
                    _user32.SetProcessDpiAwarenessContext(_ctypes.c_void_p(-4))
                    rect = _wintypes.RECT()
                    hwnd = int(window.win.winfo_id())
                    _user32.GetClientRect(hwnd, _ctypes.byref(rect))
                    _size = _png_size(getattr(window, "_shown_png", None)) or (0, 0)
                    qr_side = max(_size)
                    line = (
                        "area=%s measure=%s image=%s qr=%s win32client=%sx%s winfo=%sx%s pad=%s"
                        % (
                            getattr(window, "_area_px", None),
                            window._measure(),
                            window.image_box.winfo_reqwidth(),
                            qr_side,
                            rect.right - rect.left,
                            rect.bottom - rect.top,
                            window.win.winfo_width(),
                            window.win.winfo_height(),
                            QR_PADDING_PX,
                        )
                        # 用 `chr(10)` 而不是字面量 `\\n`：这一段是**字符串里的字符串**
                        # （`_RUNNER` 是普通三引号），写 `\\n` 会在生成 runner 时就变成
                        # 真换行，把字符串截断成语法错误（实测踩到）
                        + chr(10)
                    )
                    Path("build/qr_window_probe.txt").write_text(line, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                Path("build/qr_window_probe.txt").write_text(
                    "trigger failed: %r" % (exc,) + chr(10), encoding="utf-8"
                )

        threading.Thread(target=_trigger, daemon=True).start()


tray_module.LauncherWindow = _AutoQrLauncherWindow

port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8787
sys.exit(tray_module.run_tray(port=port, auto_start=False, open_browser=False))
'''


if __name__ == "__main__":
    sys.exit(main())
