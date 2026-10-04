"""GUI 层的**可离线测试**部分：托盘菜单构建、状态文案、图标定位。

GUI 本身在 CI/沙箱里点不了（窗口会被抢占、菜单需要真人点击），
但"菜单结构与置灰逻辑""状态文案""图标候选路径"这些是可以断言的 ——
这里只测这些，实际的点击手感靠 `web/README.md` 风格的手测清单。
"""

from __future__ import annotations

import sys

import pytest

from bili_danmaku.launcher import PanelStatus
from bili_danmaku.gui import _win32
from bili_danmaku.gui.panel_window import describe_status, format_uptime
from bili_danmaku.gui.tray import build_menu_items

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="托盘仅 Windows")


def _status(**kwargs) -> PanelStatus:
    base = dict(running=False, url="", port=0, rooms=(), uptime_s=0.0, published=0, dropped=0)
    base.update(kwargs)
    return PanelStatus(**base)  # type: ignore[arg-type]


def _items(status: PanelStatus) -> list[_win32.MenuItem]:
    noop = lambda: None  # noqa: E731
    return build_menu_items(
        status,
        on_open=noop, on_copy=noop, on_toggle=noop, on_restart=noop,
        on_show=noop, on_logs=noop, on_folder=noop, on_quit=noop,
    )


# --------------------------------------------------------------------------- #
# 文案
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (60, "1m00s"), (200, "3m20s"), (3600, "1h00m"), (7500, "2h05m")],
)
def test_format_uptime(seconds: int, expected: str) -> None:
    assert format_uptime(seconds) == expected


def test_describe_status_running() -> None:
    text, color = describe_status(_status(running=True, url="http://127.0.0.1:8787/"))
    assert text == "运行中"
    assert color.startswith("#")


def test_describe_status_stopped_and_error() -> None:
    assert describe_status(_status())[0] == "已停止"
    text, _ = describe_status(_status(error="配置无效：boom"))
    assert "配置无效" in text


# --------------------------------------------------------------------------- #
# 托盘菜单
# --------------------------------------------------------------------------- #

def _labels(items: list[_win32.MenuItem]) -> list[str]:
    return [item.label for item in items if not item.separator]


def _by_label(items: list[_win32.MenuItem], label: str) -> _win32.MenuItem:
    for item in items:
        if not item.separator and item.label == label:
            return item
    raise AssertionError(f"菜单里没有 {label!r}；实际={_labels(items)}")


def test_menu_running_state() -> None:
    """运行中：状态行带运行时长、地址行显示 URL、停止/重启可用、启动项消失。"""
    status = _status(running=True, url="http://127.0.0.1:8787/", uptime_s=200, rooms=("804456",))
    items = _items(status)
    labels = _labels(items)
    assert any("运行中" in label for label in labels), labels
    assert "http://127.0.0.1:8787/" in labels
    assert "打开面板" in labels
    assert "停止采集" in labels
    assert "重启面板" in labels
    assert "启动采集" not in labels

    assert _by_label(items, "打开面板").enabled is True
    assert _by_label(items, "复制链接").enabled is True
    assert _by_label(items, "重启面板").enabled is True
    assert _by_label(items, "打开面板").default is True


def test_menu_stopped_state_greys_out_impossible_actions() -> None:
    """未运行：打开/复制/重启必须**置灰**，并把「启动采集」换上来。"""
    items = _items(_status())
    labels = _labels(items)
    assert "启动采集" in labels
    assert "停止采集" not in labels

    assert _by_label(items, "打开面板").enabled is False
    assert _by_label(items, "复制链接").enabled is False
    assert _by_label(items, "重启面板").enabled is False
    # 这两个任何时候都该可用（不依赖面板状态）
    assert _by_label(items, "显示启动面板").enabled is True
    assert _by_label(items, "退出（停止采集）").enabled is True


def test_menu_has_no_address_row_when_stopped() -> None:
    """未运行时不该出现空地址行（否则菜单出现一行空白）。"""
    items = _items(_status())
    # 只允许一行置灰的状态行
    disabled = [item for item in items if not item.separator and not item.enabled]
    # 状态行 + 打开面板 + 复制链接 + 重启面板（后三者因未运行而置灰）
    assert all(item.label for item in disabled)
    assert not any(item.label == "" for item in items if not item.separator)


def test_menu_has_separators_between_groups() -> None:
    items = _items(_status(running=True, url="http://x/"))
    assert sum(1 for item in items if item.separator) >= 3


def test_build_menu_creates_real_hmenu_and_mapping() -> None:
    """真的调一次 Win32：能建出 HMENU，并且命令 id 映射完整。"""
    items = _items(_status(running=True, url="http://127.0.0.1:1/"))
    hmenu, mapping = _win32.build_menu(items)
    try:
        assert hmenu, "CreatePopupMenu 失败"
        expected = sum(1 for item in items if not item.separator)
        assert len(mapping) == expected
        assert user32_count(hmenu) == len(items)  # 含分隔符
    finally:
        import ctypes

        _win32.user32.DestroyMenu(hmenu)


def user32_count(hmenu: int) -> int:
    from ctypes import wintypes

    return int(_win32.user32.GetMenuItemCount(wintypes.HMENU(hmenu)))


# --------------------------------------------------------------------------- #
# 图标定位
# --------------------------------------------------------------------------- #

def test_icon_candidates_include_source_tree_assets() -> None:
    """源码运行时必须能找到 `assets/app.ico`（仓库里已入库）。"""
    from bili_danmaku.gui._win32 import icon_candidates

    candidates = icon_candidates()
    assert any(path.name == "app.ico" for path in candidates)
    # 仓库里真的有这个文件（打包与窗口图标都依赖它）
    assert any(path.is_file() for path in candidates), f"没找到 app.ico，候选={candidates}"


def test_find_icon_returns_existing_path() -> None:
    found = _win32.find_icon()
    assert found is not None
    assert found.is_file()
    assert found.suffix == ".ico"


def test_load_icon_returns_handle_or_zero() -> None:
    """`load_icon` 至少要能拿到一个非 0 的 HICON（exe 资源或磁盘 ico 或系统兜底）。"""
    hicon = _win32.load_icon()
    assert isinstance(hicon, int)
    assert hicon != 0, "连系统兜底图标都拿不到，说明 LoadIconW 绑定有问题"


def test_tray_icon_can_create_message_window() -> None:
    """托盘的消息窗口必须能建出来（Shell_NotifyIcon 的前提）。"""
    tray = _win32.TrayIcon(tooltip="单元测试")
    try:
        assert tray._create_window(), "CreateWindowExW 失败"
        assert tray.hwnd != 0
        # 消息泵能跑（没有消息时返回 0，不该抛异常）
        assert tray.pump() >= 0
    finally:
        tray.hide()


# --------------------------------------------------------------------------- #
# CLI 接线：`tray` 命令与"无子命令即启动托盘"
# --------------------------------------------------------------------------- #

def test_tray_command_is_registered() -> None:
    """`tray` 必须注册在 CLI 组上（它定义在 main 之前，靠 add_command 补注册）。"""
    from bili_danmaku.cli import main as cli_main

    assert "tray" in cli_main.commands
    assert "verify" in cli_main.commands


def test_tray_help_works_via_runner() -> None:
    """`--help` 不该触发托盘（也证明命令签名正确）。"""
    from click.testing import CliRunner

    from bili_danmaku.cli import main as cli_main

    result = CliRunner().invoke(cli_main, ["tray", "--help"])
    assert result.exit_code == 0
    assert "托盘" in result.output


def test_no_subcommand_prints_help_when_not_frozen(monkeypatch) -> None:
    """**非冻结**时无子命令应打印帮助，而不是弹出托盘窗口。

    这是刻意的：命令行里敲 `bili-danmaku` 不该莫名其妙开一个 GUI。
    冻结（exe 双击）时才走托盘分支。
    """
    from click.testing import CliRunner

    from bili_danmaku import cli as cli_module

    monkeypatch.delattr(sys, "frozen", raising=False)
    result = CliRunner().invoke(cli_module.main, [])
    assert result.exit_code == 0
    assert "Usage" in result.output or "用法" in result.output


# --------------------------------------------------------------------------- #
# 控制台编码韧性（`--windowed` exe 踩到的真崩溃）
# --------------------------------------------------------------------------- #

def test_safe_echo_survives_non_utf8_console(monkeypatch) -> None:
    """控制台是 GBK 时，含 emoji 的输出不能抛 `UnicodeEncodeError`。

    回归：打包成 `--windowed` exe 后跑 `verify`，控制台代码页是 GBK，
    `click.echo("✅ …")` 抛 `UnicodeEncodeError`，进程**带异常退出**；
    而没有控制台时用户根本看不到原因。`safe_echo` 会把无法编码的字符降级成 `?`。
    """
    import io

    from bili_danmaku.cli import safe_echo

    raw = io.BytesIO()
    # 用 GBK 文本流包住，模拟 Windows 默认代码页
    stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)

    safe_echo("✅ 自检结果")  # 不该抛
    stream.flush()
    written = raw.getvalue().decode("gbk")
    assert "自检结果" in written, "中文信息应尽量保留"


def test_safe_echo_is_transparent_on_utf8(monkeypatch) -> None:
    """UTF-8 环境下 `safe_echo` 必须**原样输出**（含 emoji），不做降级。"""
    import io

    from bili_danmaku.cli import safe_echo

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)

    safe_echo("✅ 自检结果")
    stream.flush()
    assert "✅ 自检结果" in raw.getvalue().decode("utf-8")


# --------------------------------------------------------------------------- #
# 扫码登录：二维码弹窗的尺寸与事件（需求：二维码必须完整显示）
#
# 背景：二维码原来塞在**固定宽度**的启动面板里，会被裁掉一角 ——
# 而缺角的二维码扫不出来，用户看到的只是"扫了没反应"。
# 现在改成独立弹窗，下面这些就是"一定装得下"这条承诺的守门人。
# --------------------------------------------------------------------------- #

def test_png_size_reads_header_without_decoding() -> None:
    """从 PNG 文件头读宽高（只查前 24 字节，不解码图片）。"""
    from bili_danmaku.credentials import png_size

    assert png_size(None) is None
    assert png_size(b"") is None
    assert png_size(b"not a png at all........") is None
    # 签名对但长度不够（IHDR 没读全）也要拒绝，而不是读出垃圾数字
    assert png_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) is None

    # 最小合法头：签名 + 块长 + IHDR + 宽 + 高
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (300).to_bytes(4, "big") + (200).to_bytes(4, "big")
    assert png_size(header) == (300, 200)


def test_png_size_rejects_zero_dimensions() -> None:
    """宽或高为 0 的头是坏的：返回 None，别让调用方按 0 算窗口尺寸。"""
    from bili_danmaku.credentials import png_size

    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (0).to_bytes(4, "big") + (200).to_bytes(4, "big")
    assert png_size(header) is None


@pytest.mark.skipif(
    pytest.importorskip("qrcode", reason="需要 qrcode 才能生成真二维码") is None,
    reason="qrcode 不可用",
)
def test_png_size_on_a_real_qr_png() -> None:
    """真二维码的 PNG 头必须读得出尺寸，且是正方形。

    这条用例守的是**真东西**：解析写错时上面那些手工拼的头仍然能过，
    但真 PNG 会读不出来（例如把宽高的偏移记错 4 个字节）。
    """
    from bili_danmaku.credentials import png_size, render_qr_png

    png = render_qr_png("https://passport.bilibili.com/h5-app/passport/login/scan")
    assert png, "这台机器上 qrcode + Pillow 都在，应该能渲染出 PNG"
    size = png_size(png)
    assert size is not None
    assert size[0] == size[1], f"二维码应该是正方形，实际 {size}"
    assert size[0] >= 100, f"尺寸太小不可能扫得出来：{size}"


@pytest.mark.parametrize("side", [232, 296, 424, 600])
def test_qr_area_always_fits_the_image(side: int) -> None:
    """**核心承诺**：算出来的窗口尺寸永远装得下二维码（每边还留出白边）。"""
    from bili_danmaku.gui.qr_window import QR_PADDING_PX, qr_area_px

    area = qr_area_px((side, side))
    assert area >= side + 2 * QR_PADDING_PX, f"图 {side} 需要 ≥ {side + 2 * QR_PADDING_PX}，实际 {area}"


def test_qr_area_is_bounded_and_never_zero() -> None:
    """量不到图片尺寸时给保守值；极端大图也不超过上限。

    上限不是"随便定的"：高 DPI 下窗口一旦超过屏幕，用户连码都够不着，
    那比"稍微裁一点"更糟 —— 所以宁可封顶。
    """
    from bili_danmaku.gui.qr_window import QR_MAX_AREA_PX, qr_area_px

    assert qr_area_px(None) == QR_MAX_AREA_PX
    assert qr_area_px((0, 0)) == QR_MAX_AREA_PX
    assert qr_area_px((5000, 5000)) == QR_MAX_AREA_PX


class _StubSession:
    """登录会话替身：只提供弹窗用到的两个成员。"""

    def __init__(self) -> None:
        self.qr_png: bytes | None = None
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _StubLabel:
    def __init__(self) -> None:
        self.text = ""
        self.fg = ""

    def configure(self, **kwargs) -> None:  # noqa: ANN003
        if "text" in kwargs:
            self.text = str(kwargs["text"])
        if "fg" in kwargs:
            self.fg = str(kwargs["fg"])


def _bare_qr_window(session: _StubSession) -> tuple[object, list[str]]:
    """造一个**不走 `__init__`** 的弹窗对象：只测事件处理，不碰 tkinter。

    为什么要这样测：`QrLoginWindow.__init__` 会建真窗口，而 CI/沙箱里没有桌面。
    但"收到 `ok` 要自动关窗、收到 `failed` 要显示原因"这类**判断**恰恰是最该
    被守住的（写错就是"扫完了窗口不关"或"失败了什么都不说"）。
    """
    from bili_danmaku.gui.qr_window import QrLoginWindow

    window = object.__new__(QrLoginWindow)
    window.session = session
    window.closed = False
    window.outcome = ""
    window._photo = None
    window._shown_png = None
    window._area_px = 0
    window.status_label = _StubLabel()
    closed: list[str] = []
    window.close = lambda: closed.append("closed")  # type: ignore[method-assign]
    return window, closed


def test_qr_window_auto_closes_on_success_and_records_outcome() -> None:
    """**需求**：拿到凭证后自动关闭二维码弹窗。"""
    session = _StubSession()
    window, closed = _bare_qr_window(session)

    window.handle({"state": "ok"})

    assert closed == ["closed"], "登录成功后弹窗必须自己关掉"
    assert window.outcome == "ok", "面板要靠 outcome 区分'成功自动关'与'用户取消'"
    assert "成功" in window.status_label.text


def test_qr_window_stays_open_while_waiting() -> None:
    """等待/已扫码时**不能**关窗 —— 用户还没扫完。"""
    window, closed = _bare_qr_window(_StubSession())

    window.handle({"state": "waiting"})
    assert closed == []
    assert "等待" in window.status_label.text

    window.handle({"state": "scanned"})
    assert closed == []
    assert "确认" in window.status_label.text


def test_qr_window_shows_failure_reason_then_closes() -> None:
    window, closed = _bare_qr_window(_StubSession())

    window.handle({"state": "failed", "message": "二维码申请失败：网络错误"})
    assert closed == ["closed"]
    assert window.outcome == "failed"
    assert "网络错误" in window.status_label.text, "失败原因必须显示出来，不能只关窗"


def test_qr_window_expired_keeps_window_for_the_next_code() -> None:
    """二维码过期要**留在原地等换新码**，不能关窗（关了用户就得重新点一次）。"""
    window, closed = _bare_qr_window(_StubSession())

    window.handle({"state": "expired"})
    assert closed == []
    assert "失效" in window.status_label.text
    assert window._shown_png is None, "换新码时要允许重画"


def test_qr_window_cancel_does_not_overwrite_success() -> None:
    """已经判定成功之后再来的取消**不能**把结论改掉。

    真实时序里 `close()` 之后窗口还在收尾，用户手快点到"×"就会走到这里；
    覆盖了就会变成"登录成功了但面板说已取消"。
    """
    window, _ = _bare_qr_window(_StubSession())
    window.outcome = "ok"

    window.cancel()

    assert window.outcome == "ok"
