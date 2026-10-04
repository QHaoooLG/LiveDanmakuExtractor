"""GUI 层的**可离线测试**部分：托盘菜单构建、状态文案、图标定位。

GUI 本身在 CI/沙箱里点不了（窗口会被抢占、菜单需要真人点击），
但"菜单结构与置灰逻辑""状态文案""图标候选路径"这些是可以断言的 ——
这里只测这些，实际的点击手感靠 `web/README.md` 风格的手测清单。
"""

from __future__ import annotations

import sys
from pathlib import Path

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
