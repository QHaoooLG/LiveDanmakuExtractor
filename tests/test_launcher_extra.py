"""补 `launcher.py` 的用例：uvicorn 日志配置、默认日志文件、状态快照边界。

这些是**没有界面也能测**的部分（`PanelController` 的纯逻辑），
之前缺测所以覆盖率压在 85% 的门禁线上。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bili_danmaku.launcher import (
    LauncherOptions,
    PanelController,
    PanelStatus,
    format_bytes,
    scan_storage,
)

#: `_default_log_file` 是 `PanelController` 的静态方法（私有但值得直接测）
_default_log_file = PanelController._default_log_file


def _controller(tmp_path: Path, **options) -> PanelController:
    # `setdefault` 而不是直接传：调用方要能覆盖 out_dir（存储位置相关用例需要）
    options.setdefault("out_dir", str(tmp_path))
    return PanelController(LauncherOptions(port=0, **options))


# --------------------------------------------------------------------------- #
# 默认日志文件：`--windowed` 的 exe 没有 stderr，不落文件就等于没有日志
# --------------------------------------------------------------------------- #

def test_default_log_file_empty_when_streams_available(monkeypatch) -> None:
    """普通运行（有 stdout/stderr、非冻结）**不该**默认写文件 —— 别误伤 CLI。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert _default_log_file() == ""


def test_default_log_file_when_frozen(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    from bili_danmaku import runtime

    monkeypatch.setattr(runtime, "LOG_FILE", tmp_path / "web.log")
    assert _default_log_file().endswith("web.log")


def test_default_log_file_when_stderr_missing(monkeypatch, tmp_path: Path) -> None:
    """`--windowed` 打包后 `sys.stderr` 是 None ⇒ 必须落文件。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "stderr", None)
    from bili_danmaku import runtime

    monkeypatch.setattr(runtime, "LOG_FILE", tmp_path / "web.log")
    assert _default_log_file().endswith("web.log")


# --------------------------------------------------------------------------- #
# uvicorn 日志配置：它的 logger 是 propagate=False 的独立 logger，
# 不显式接管就**不会**经过根 logger 的文件 handler
# --------------------------------------------------------------------------- #

def test_uvicorn_log_config_without_file_only_stderr(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller._config = _config_stub()

    config = controller._uvicorn_log_config(controller._config)
    assert config["handlers"].keys() == {"stderr"}
    assert config["loggers"]["uvicorn.error"]["handlers"] == ["stderr"]
    assert config["loggers"]["uvicorn.error"]["propagate"] is False
    assert config["disable_existing_loggers"] is False, "否则会掐掉项目自己的 logger"


def test_uvicorn_log_config_adds_file_handler_when_log_file_set(tmp_path: Path) -> None:
    log_file = tmp_path / "sub" / "web.log"
    controller = _controller(tmp_path, log_file=str(log_file))
    controller._config = _config_stub()

    config = controller._uvicorn_log_config(controller._config)
    assert set(config["handlers"]) == {"stderr", "file"}
    assert config["handlers"]["file"]["filename"] == str(log_file)
    assert config["handlers"]["file"]["encoding"] == "utf-8", "中文日志必须显式 UTF-8"
    assert config["loggers"]["uvicorn"]["handlers"] == ["stderr", "file"]


def _config_stub():
    """最小 Config 替身：`_uvicorn_log_config` 只用到 `general.log_level`。"""

    class _General:
        log_level = "INFO"

    class _Config:
        general = _General()

    return _Config()


# --------------------------------------------------------------------------- #
# 状态快照
# --------------------------------------------------------------------------- #

def test_status_before_start_is_stopped(tmp_path: Path) -> None:
    status = _controller(tmp_path).status()
    assert status.running is False
    assert status.url == "", "没启动时不该编造一个 URL"
    assert status.port == 0
    assert status.started_at == 0.0
    assert status.uptime_s == 0.0
    assert status.error == "", "没启动过也不算错误"


def test_status_reports_frontend_and_dist(tmp_path: Path) -> None:
    """前端产物存在时，状态里要能直接看到（托盘/面板都靠它判断"是不是降级页"）。"""
    status = _controller(tmp_path).status()
    assert status.frontend_built is True
    assert status.dist and Path(status.dist, "index.html").is_file()


def test_restart_without_start_is_safe(tmp_path: Path) -> None:
    """没启动就 restart：不该抛异常。"""
    controller = _controller(tmp_path)
    # restart = stop + start；这里只验证 stop 那半段在"未启动"时是安全的
    assert controller.stop().running is False


def test_new_login_session_uses_option_cookie_file(tmp_path: Path) -> None:
    """扫码会话要跟随本控制器的凭据文件（避免 GUI 与面板各算一遍）。"""
    controller = _controller(tmp_path, cookie_file=".secrets/custom.json")
    session = controller.new_login_session()
    assert str(session.cookie_file).replace("\\", "/") == ".secrets/custom.json"


def test_add_room_without_panel_reports_clearly(tmp_path: Path) -> None:
    ok, detail = _controller(tmp_path).add_room("804456")
    assert ok is False
    assert "未启动" in detail


def test_panel_status_is_a_dataclass_with_defaults() -> None:
    status = PanelStatus(running=False)
    assert status.rooms == ()
    assert status.published == 0 and status.dropped == 0
    assert status.frontend_built is False


# --------------------------------------------------------------------------- #
# 存储位置：占用统计（**只读**）与切换
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (3 * 1024**3, "3.0 GB"),
    ],
)
def test_format_bytes(size: int, expected: str) -> None:
    assert format_bytes(size) == expected


def test_scan_storage_reports_missing_directory(tmp_path: Path) -> None:
    info = scan_storage(tmp_path / "nope")
    assert info.exists is False
    assert info.error == "目录不存在"
    assert info.total_bytes == 0
    assert info.total_text == "0 B"


def test_scan_storage_summarises_by_extension(tmp_path: Path) -> None:
    """占用要按后缀分出来 —— 用户最先想知道的是"哪个文件在吃盘"。"""
    (tmp_path / "danmaku.db").write_bytes(b"x" * 3000)
    (tmp_path / "a.jsonl").write_bytes(b"y" * 1000)
    (tmp_path / "b.jsonl").write_bytes(b"z" * 500)
    info = scan_storage(tmp_path)

    assert info.exists is True
    assert info.writable is True
    assert info.total_bytes == 4500
    assert info.file_count == 3
    by_ext = dict(info.by_extension)
    assert by_ext[".db"] == 3000
    assert by_ext[".jsonl"] == 1500
    # 降序：最大的在前
    assert info.by_extension[0][0] == ".db"


def test_scan_storage_ignores_subdirectories(tmp_path: Path) -> None:
    """落盘结构是扁平的：子目录不该被算进来（也不能因此给出偏小的数字而不说）。"""
    (tmp_path / "flat.jsonl").write_bytes(b"x" * 100)
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "deep.jsonl").write_bytes(b"y" * 10_000)

    info = scan_storage(tmp_path)
    assert info.file_count == 1
    assert info.total_bytes == 100, "子目录里的文件不该计入"


def test_scan_storage_marks_truncation(tmp_path: Path) -> None:
    """文件极多时到上限就停，并**如实标记**截断（不能静默给出偏小的数字）。"""
    for index in range(10):
        (tmp_path / f"f{index}.jsonl").write_bytes(b"x")
    info = scan_storage(tmp_path, limit=4)
    assert info.truncated is True
    assert info.file_count == 4


def test_scan_storage_reports_unwritable(tmp_path: Path, monkeypatch) -> None:
    """不可写要标出来（落盘会失败，这比"占用多少"重要得多）。"""
    monkeypatch.setattr("bili_danmaku.launcher._is_writable", lambda path: False)
    assert scan_storage(tmp_path).writable is False


# --------------------------------------------------------------------------- #
# 存储位置：切换语义
# --------------------------------------------------------------------------- #

def test_current_out_dir_prefers_config(tmp_path: Path) -> None:
    """配置里的 `sinks.out_dir` 优先于选项 —— 否则界面显示的目录与实际写盘的对不上。"""
    controller = _controller(tmp_path)
    controller._config = type("C", (), {"sinks": type("S", (), {"out_dir": "from-config"})()})()
    assert controller.current_out_dir() == "from-config"


def test_set_out_dir_creates_and_switches(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    target = tmp_path / "newplace"
    ok, message = controller.set_out_dir(str(target))
    assert ok is True
    assert target.is_dir(), "应当自动创建目录"
    assert controller.current_out_dir() == str(target)
    assert "重启" not in message, "没在运行时不该提'重启生效'"


def test_set_out_dir_warns_when_running(tmp_path: Path, monkeypatch) -> None:
    """运行中切换必须**明确说"重启后才生效"**。

    悄悄"改成功了但这次仍写旧目录"是最糟的体验 —— 用户会以为没生效，
    或者更糟：以为数据写进了新目录。
    """
    controller = _controller(tmp_path)
    monkeypatch.setattr(controller, "is_running", lambda: True)

    target = tmp_path / "while-running"
    ok, message = controller.set_out_dir(str(target))
    assert ok is True
    assert "重启" in message, message
    assert "仍写入原目录" in message or "原目录" in message


def test_set_out_dir_rejects_blank(tmp_path: Path) -> None:
    ok, message = _controller(tmp_path).set_out_dir("   ")
    assert ok is False
    assert "不能为空" in message


def test_set_out_dir_rejects_unwritable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bili_danmaku.launcher._is_writable", lambda path: False)
    ok, message = _controller(tmp_path).set_out_dir(str(tmp_path / "ro"))
    assert ok is False
    assert "不可写" in message


def test_set_out_dir_does_not_touch_existing_data(tmp_path: Path) -> None:
    """**核心语义**：切换目录既不迁移也不删除已有数据。"""
    old = tmp_path / "old"
    old.mkdir()
    keep = old / "danmaku.db"
    keep.write_bytes(b"precious")

    controller = _controller(tmp_path, out_dir=str(old))
    ok, _ = controller.set_out_dir(str(tmp_path / "fresh"))
    assert ok is True

    assert keep.is_file(), "旧数据必须原样留在原地"
    assert keep.read_bytes() == b"precious"
    assert list((tmp_path / "fresh").iterdir()) == [], "新目录应当是空的，不做迁移"


def test_storage_info_follows_current_out_dir(tmp_path: Path) -> None:
    """`storage_info()` 要读**实际在用**的目录（配置优先），不是死读 options。"""
    controller = _controller(tmp_path, out_dir=str(tmp_path))
    (tmp_path / "a.jsonl").write_bytes(b"x" * 2048)
    info = controller.storage_info()
    assert info.exists is True
    assert info.total_bytes == 2048
