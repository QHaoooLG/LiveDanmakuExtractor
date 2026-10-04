"""`launcher.PanelController` 的离线用例。

这一层是"托盘 + 启动面板"的核心：它在**本进程后台线程**里托管 uvicorn，
替代 `cli.py` 那条 spawn 子进程的路径（后者在 Windows 上会多出一个 console，
关掉就连坐杀面板 —— 见 HANDOVER §5.12）。

用例刻意只依赖本地回环端口，不需要登录态、不访问 B 站。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bili_danmaku.launcher import LauncherOptions, PanelController


@pytest.fixture()
def controller(tmp_path: Path):
    """一个用临时目录、随机端口的控制器；用例结束必定优雅停止。"""
    ctrl = PanelController(
        LauncherOptions(
            host="127.0.0.1",
            port=0,  # 自己挑空闲端口，避免与在跑的面板撞车
            out_dir=str(tmp_path / "data"),
            rooms=(),
            open_browser=False,
        )
    )
    yield ctrl
    ctrl.stop(timeout=30)


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def test_free_port_returns_usable_port(controller: PanelController) -> None:
    """`_free_port` 必须返回一个**当前真的能绑定**的端口。

    存在的理由：`server.bind_port(host, 0)` 对 0 会返回错误端口（实测返回 1），
    所以这里自己挑。
    """
    import socket

    port = controller._free_port("127.0.0.1")
    assert 1024 < port < 65536, f"端口看起来不合理: {port}"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))  # 不该抛 OSError


def test_start_then_stop_is_graceful(controller: PanelController) -> None:
    """启动后能访问首页；停止后线程结束、端口释放。"""
    status = controller.start(wait=True, timeout=30)
    assert status.running is True, f"启动失败：{status.error}"
    assert status.port > 0
    assert status.url == f"http://127.0.0.1:{status.port}/"

    code, body = _get(status.url)
    assert code == 200
    assert body  # 非空（真前端或降级页都算通过；本仓库有 web/dist）

    stopped = controller.stop(timeout=30)
    assert stopped.running is False
    assert controller.is_running() is False


def test_status_reports_frontend_and_hub(controller: PanelController) -> None:
    """状态快照要带上前端定位结果与 hub 计数（托盘直接读它，不发 HTTP）。"""
    status = controller.start(wait=True, timeout=30)
    assert status.running

    # 本仓库有 web/dist，所以前端必须被定位到
    assert status.frontend_built is True
    assert status.dist and Path(status.dist, "index.html").is_file()

    # hub 计数属性名必须是 published/dropped（不是 cumulative_total ——
    # 那是 /api/health 的响应字段名，踩过这个坑）
    assert status.published == 0
    assert status.dropped == 0
    assert status.uptime_s >= 0


def test_health_endpoint_matches_in_process_option(controller: PanelController) -> None:
    """面板的 `content_only` 应来自 LauncherOptions（默认只采文字弹幕）。"""
    status = controller.start(wait=True, timeout=30)
    assert status.running

    code, body = _get(f"http://127.0.0.1:{status.port}/api/health")
    assert code == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["content_only"] is True
    assert payload["frontend_built"] is True


def test_start_is_idempotent(controller: PanelController) -> None:
    """重复 `start()` 不该起第二个实例（端口保持同一个）。"""
    first = controller.start(wait=True, timeout=30)
    assert first.running
    second = controller.start(wait=True, timeout=30)
    assert second.running
    assert second.port == first.port


def test_stop_before_start_is_safe(controller: PanelController) -> None:
    """没启动就 stop：不报错、状态是 stopped。"""
    assert controller.is_running() is False
    status = controller.stop()
    assert status.running is False


def test_restart_keeps_serving(controller: PanelController) -> None:
    """restart = stop + start，重启后仍能访问。"""
    assert controller.start(wait=True, timeout=30).running
    status = controller.restart(wait=True, timeout=30)
    assert status.running, f"重启失败：{status.error}"
    code, _ = _get(status.url)
    assert code == 200


def test_add_room_rejects_bad_input(controller: PanelController) -> None:
    """坏房间号要**报错**而不是猜（复用 rooms.py 的解析规则）。"""
    controller.start(wait=True, timeout=30)
    ok, detail = controller.add_room("这不是房间号")
    assert ok is False
    assert detail


def test_add_room_before_start_reports_clearly(controller: PanelController) -> None:
    """未启动时加房间：返回明确错误，而不是抛异常。"""
    ok, detail = controller.add_room("804456")
    assert ok is False
    assert "未启动" in detail


def test_invalid_config_reports_error_without_half_start(tmp_path: Path) -> None:
    """配置非法时要**干净失败**：running=False + error 有内容，且不留下线程。"""
    ctrl = PanelController(
        LauncherOptions(
            host="127.0.0.1",
            port=0,
            out_dir=str(tmp_path / "data"),
            formats=("不存在的格式",),  # 会被 Config 校验拒绝
        )
    )
    try:
        status = ctrl.start(wait=True, timeout=15)
        assert status.running is False
        assert status.error, "配置非法时必须给出可读的错误说明"
        assert ctrl.is_running() is False
    finally:
        ctrl.stop()
