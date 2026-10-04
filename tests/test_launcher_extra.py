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
)

#: `_default_log_file` 是 `PanelController` 的静态方法（私有但值得直接测）
_default_log_file = PanelController._default_log_file


def _controller(tmp_path: Path, **options) -> PanelController:
    return PanelController(LauncherOptions(port=0, out_dir=str(tmp_path), **options))


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
