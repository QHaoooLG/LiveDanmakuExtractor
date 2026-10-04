"""V2-c 单测：面板托管运行时（状态文件 / 陈旧清理 / 优雅停止 / CLI 生命周期）。

这层的价值全在"边界"上：进程崩了状态文件会残留、停止请求可能失败、
非本机请求必须被拒 —— 所以用例都围绕这些边界写，而不是测正常路径能跑。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bili_danmaku import runtime
from bili_danmaku.cli import main
from bili_danmaku.runtime import WebRuntime

# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #


def make_runtime(tmp_path: Path, pid: int | None = None, **overrides) -> WebRuntime:
    data = {
        "pid": pid if pid is not None else os.getpid(),
        "host": "127.0.0.1",
        "port": 8787,
        "url": "http://127.0.0.1:8787/",
        "rooms": ["804456"],
        "out_dir": "data",
        "formats": ["jsonl", "csv", "sqlite"],
        "content_only": True,
        "started_at": time.time() - 42,
        "log_path": ".runtime/web.log",
        "frontend_built": True,
        "version": "0.1.0",
    }
    data.update(overrides)
    return WebRuntime(**data)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    state = tmp_path / "web.json"
    original = make_runtime(tmp_path)
    runtime.save_state(original, state)

    loaded = runtime.load_state(state)
    assert loaded is not None
    assert loaded.pid == original.pid
    assert loaded.url == original.url
    assert loaded.rooms == ["804456"]
    assert loaded.uptime > 0
    json.loads(state.read_text(encoding="utf-8"))  # 必须是合法 JSON


def test_load_missing_and_corrupt_state(tmp_path: Path) -> None:
    state = tmp_path / "web.json"
    assert runtime.load_state(state) is None

    state.write_text("{ 不是 JSON", encoding="utf-8")
    assert runtime.load_state(state) is None, "损坏的状态文件应被忽略而不是抛异常"

    state.write_text(json.dumps({"host": "127.0.0.1"}), encoding="utf-8")  # 缺 pid 等必填
    assert runtime.load_state(state) is None


def test_clear_state(tmp_path: Path) -> None:
    state = tmp_path / "web.json"
    runtime.save_state(make_runtime(tmp_path), state)
    assert runtime.clear_state(state) is True
    assert not state.exists()
    assert runtime.clear_state(state) is False, "重复清理应返回 False"


def test_describe_contains_key_facts(tmp_path: Path) -> None:
    text = make_runtime(tmp_path).describe()
    assert "127.0.0.1:8787" in text
    assert "804456" in text
    assert "jsonl, csv, sqlite" in text
    assert "已运行" in text


# --------------------------------------------------------------------------- #
# 进程存活与陈旧状态
# --------------------------------------------------------------------------- #


def test_pid_alive_detects_current_process() -> None:
    assert runtime.pid_alive(os.getpid()) is True
    assert runtime.pid_alive(0) is False
    assert runtime.pid_alive(-1) is False


def test_pid_alive_for_exited_process() -> None:
    """起一个立刻退出的进程，等它退出后必须判定为"不存活"。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=30)
    assert runtime.pid_alive(proc.pid) is False


def test_stale_state_is_cleaned_automatically(tmp_path: Path, monkeypatch) -> None:
    """崩溃后残留的状态文件不能让下次 `web start` 永远拒绝启动。"""
    state = tmp_path / "web.json"
    ghost = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    ghost.wait(timeout=30)
    runtime.save_state(make_runtime(tmp_path, pid=ghost.pid), state)

    assert runtime.live_runtime(state) is None
    assert not state.exists(), "陈旧状态文件应被自动清理"

    # 存活进程的实例要能正常读到
    runtime.save_state(make_runtime(tmp_path, pid=os.getpid()), state)
    live = runtime.live_runtime(state)
    assert live is not None and live.pid == os.getpid()


# --------------------------------------------------------------------------- #
# 优雅停止
# --------------------------------------------------------------------------- #


def test_shutdown_url_points_at_control_endpoint(tmp_path: Path) -> None:
    assert runtime.shutdown_url(make_runtime(tmp_path)) == (
        f"http://127.0.0.1:8787{runtime.SHUTDOWN_PATH}"
    )
    assert runtime.SHUTDOWN_PATH == "/api/admin/shutdown"


def test_request_shutdown_reports_connection_error(tmp_path: Path) -> None:
    """端口没人听时不能抛异常，而要返回可读的失败说明。"""
    instance = make_runtime(tmp_path, host="127.0.0.1", port=1)
    ok, detail = runtime.request_shutdown(instance, timeout=1.0)
    assert ok is False
    assert detail


def test_stop_runtime_when_process_already_gone(tmp_path: Path) -> None:
    state = tmp_path / "web.json"
    ghost = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    ghost.wait(timeout=30)
    runtime.save_state(make_runtime(tmp_path, pid=ghost.pid), state)

    ok, detail = runtime.stop_runtime(runtime.load_state(state), state_file=state)
    assert ok is True
    assert "已不在运行" in detail
    assert not state.exists()


def test_stop_runtime_waits_and_falls_back_to_force(tmp_path: Path, monkeypatch) -> None:
    """优雅停止请求失败时：允许强杀，并且说明里必须写明"可能丢数据"。"""
    state = tmp_path / "web.json"
    instance = make_runtime(tmp_path, pid=os.getpid())
    runtime.save_state(instance, state)

    monkeypatch.setattr(runtime, "request_shutdown", lambda *a, **k: (False, "HTTP 403"))
    killed: list[int] = []
    monkeypatch.setattr(
        runtime, "force_kill", lambda pid: (killed.append(pid) or True, "taskkill 成功")
    )
    monkeypatch.setattr(runtime, "wait_pid_exit", lambda pid, timeout, **k: True)

    ok, detail = runtime.stop_runtime(runtime.load_state(state), state_file=state)
    assert ok is True
    assert killed == [os.getpid()]
    assert "强杀" in detail and "未落盘" in detail
    assert not state.exists()


def test_stop_runtime_no_force_respects_flag(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "web.json"
    instance = make_runtime(tmp_path, pid=os.getpid())
    runtime.save_state(instance, state)

    monkeypatch.setattr(runtime, "request_shutdown", lambda *a, **k: (False, "HTTP 403"))
    called: list[int] = []
    monkeypatch.setattr(runtime, "force_kill", lambda pid: (called.append(pid) or True, "x"))

    ok, detail = runtime.stop_runtime(
        runtime.load_state(state), state_file=state, allow_force=False
    )
    assert ok is False
    assert called == [], "显式禁止强杀时不应调用 force_kill"
    assert "未允许强杀" in detail


def test_stop_runtime_timeout_then_force(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "web.json"
    instance = make_runtime(tmp_path, pid=os.getpid())
    runtime.save_state(instance, state)

    monkeypatch.setattr(runtime, "request_shutdown", lambda *a, **k: (True, '{"stopping":true}'))
    monkeypatch.setattr(runtime, "wait_pid_exit", lambda pid, timeout, **k: False)
    monkeypatch.setattr(runtime, "force_kill", lambda pid: (True, "taskkill /F"))

    ok, detail = runtime.stop_runtime(runtime.load_state(state), state_file=state, wait=0.01)
    assert ok is True
    assert "等待" in detail and "强杀" in detail


def test_stop_runtime_graceful_success(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "web.json"
    instance = make_runtime(tmp_path, pid=os.getpid())
    runtime.save_state(instance, state)

    monkeypatch.setattr(runtime, "request_shutdown", lambda *a, **k: (True, '{"stopping":true}'))
    monkeypatch.setattr(runtime, "wait_pid_exit", lambda pid, timeout, **k: True)

    ok, detail = runtime.stop_runtime(runtime.load_state(state), state_file=state)
    assert ok is True
    assert "已优雅停止" in detail
    assert "flush" in detail, "要明确告诉用户数据已落盘"
    assert not state.exists()


# --------------------------------------------------------------------------- #
# 命令行动态
# --------------------------------------------------------------------------- #


def test_spawn_detached_rejects_non_string_argv(tmp_path: Path) -> None:
    """参数里混进 None 会被 Popen 转成字面量 "None"，必须提前拦住。"""
    with pytest.raises(ValueError, match="非字符串参数"):
        runtime.spawn_detached([sys.executable, None], log_path=tmp_path / "x.log")  # type: ignore[list-item]


def test_spawn_detached_writes_to_log(tmp_path: Path) -> None:
    """后台进程的 stdout/stderr 必须落到日志文件（分离后没有终端可写）。"""
    log = tmp_path / "run.log"
    proc = runtime.spawn_detached(
        [sys.executable, "-c", "print('detached-hello')"],
        log_path=log,
    )
    assert proc.wait(timeout=60) == 0

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "detached-hello" not in log.read_text(encoding="utf-8"):
        time.sleep(0.1)
    assert "detached-hello" in log.read_text(encoding="utf-8")


def test_server_argv_uses_module_entrypoint() -> None:
    """命令行必须是 `python -m bili_danmaku …` 形式。

    注意 `argv[0]` **不再断言等于 `sys.executable`** —— 那正是 §5.12 的缺陷来源
    （venv 的转发器 stub 会多建一个 console）。解释器的选择由
    `test_server_argv_avoids_venv_redirector_stub` 专门守着。
    """
    argv = runtime.server_argv("web", "_serve", "--managed")
    assert Path(argv[0]).is_file()
    assert argv[1:3] == ["-m", "bili_danmaku"]
    assert argv[3:] == ["web", "_serve", "--managed"]


def test_web_group_and_subcommands_registered() -> None:
    web = main.commands["web"]
    assert {"start", "stop", "status", "logs", "restart", "_serve"} <= set(web.commands)


def test_web_status_when_not_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_FILE", tmp_path / "web.json")
    result = CliRunner().invoke(main, ["web", "status"])
    assert result.exit_code == 0
    assert "未在运行" in result.output

    as_json = CliRunner().invoke(main, ["web", "status", "--json"])
    assert json.loads(as_json.output) == {"running": False}


def test_web_stop_without_instance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_FILE", tmp_path / "web.json")
    result = CliRunner().invoke(main, ["web", "stop"])
    assert result.exit_code == 0
    assert "无需停止" in result.output


def test_web_start_refuses_second_instance(tmp_path: Path, monkeypatch) -> None:
    """已有实例在跑时必须拒绝（除非 --force），且不该尝试 spawn。"""
    monkeypatch.setattr(runtime, "STATE_FILE", tmp_path / "web.json")
    runtime.save_state(make_runtime(tmp_path, pid=os.getpid()), tmp_path / "web.json")

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        runtime, "spawn_detached", lambda argv, **k: spawned.append(argv) or object()
    )

    result = CliRunner().invoke(main, ["web", "start", "--rooms", "804456"])
    assert result.exit_code == 2
    assert "已有面板在运行" in result.output


# --------------------------------------------------------------------------- #
# 绕开 venv 转发器 stub（HANDOVER §5.12）
# --------------------------------------------------------------------------- #


def test_server_argv_avoids_venv_redirector_stub() -> None:
    """`server_argv()` 必须**绕开 venv 的转发器 stub**。

    回归 §5.12：`.venv\\Scripts\\python.exe` 是转发器 stub，会再起一个 base
    解释器；那个 base 解释器是 console 程序又拿不到父 console，Windows 就
    **新分配一个 conhost** —— 用户看到"多出来一个空终端窗口"，而且
    **关掉它面板就停**。绕开 stub 后顶层进程本身就是 server，没有 console 可关。
    """
    argv = runtime.server_argv("web", "_serve", "--managed")
    assert argv[0] != sys.executable, "仍在使用 venv 的 stub（sys.executable）"
    assert Path(argv[0]).is_file(), f"挑出来的解释器不存在: {argv[0]}"
    assert argv[1:4] == ["-m", "bili_danmaku", "web"]


def test_child_python_injects_sys_path_for_base_interpreter() -> None:
    """绕开 stub 时必须注入 `PYTHONPATH`，否则 base 解释器 import 不到本项目。"""
    exe, env = runtime.child_python()

    if exe == sys.executable:
        # 非 venv 环境（或拿不到 base 解释器）：此时不需要注入环境
        assert env == {}
        return

    assert "PYTHONPATH" in env
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries, "PYTHONPATH 不能为空"
    for entry in entries:
        # 空串会被解释成当前目录（安全隐患），相对路径依赖 cwd —— 都不允许
        assert entry, "PYTHONPATH 里出现了空串"
        assert Path(entry).is_absolute(), f"相对路径不能进 PYTHONPATH: {entry}"
        assert Path(entry).is_dir(), f"PYTHONPATH 里的路径不存在: {entry}"
    assert env.get("PYTHONNOUSERSITE") == "1"


def test_child_python_env_can_import_the_project() -> None:
    """真正的判据：用该解释器 + 环境**能 import 到本项目**。"""
    exe, env = runtime.child_python()
    full_env = dict(os.environ)
    full_env.update(env)
    full_env.pop("PYTHONHOME", None)

    result = subprocess.run(
        [exe, "-c", "import bili_danmaku; print(bili_danmaku.__file__)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90, env=full_env,
    )
    assert result.returncode == 0, (
        f"base 解释器 import 不到本项目：\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "bili_danmaku" in result.stdout


def test_venv_root_is_detected_in_this_environment() -> None:
    """本项目跑在 venv 里，`_venv_root()` 应当能识别出来（否则 stub 也绕不开）。"""
    root = runtime._venv_root()
    if root is None:
        pytest.skip("当前不是 venv 环境")
    assert (root / "pyvenv.cfg").is_file()


# --------------------------------------------------------------------------- #
# 端口占用提示（CLI 与托盘并存的语义）
# --------------------------------------------------------------------------- #


def test_port_is_free_reports_true_when_unused() -> None:
    """空闲端口应报告为可用（用系统分配的临时端口，避免撞上固定的 8787）。"""
    import socket

    from bili_danmaku.cli import _port_is_free

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = int(probe.getsockname()[1])
    # 上面那个 socket 还开着，但我们查的是**别的**仍空闲的端口
    assert _port_is_free(free_port + 1) or _port_is_free(free_port + 2)


def test_port_is_free_reports_false_when_occupied() -> None:
    """被占用的端口必须报告为不可用（这是"托盘已在跑"的判据）。"""
    import socket

    from bili_danmaku.cli import _port_is_free

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = int(blocker.getsockname()[1])
        assert _port_is_free(port) is False
    # 释放后就该空闲了
    assert _port_is_free(port) is True


def test_resolve_web_port_prefers_explicit_value() -> None:
    """显式给了 `--port` 就直接用它（不读配置）。"""
    from bili_danmaku.cli import _resolve_web_port

    assert _resolve_web_port(9001, None) == 9001
    assert _resolve_web_port("9002", None) == 9002


def test_resolve_web_port_returns_zero_for_garbage() -> None:
    """无法解析时返回 0（= "不确定"），调用方据此跳过提示。"""
    from bili_danmaku.cli import _resolve_web_port

    assert _resolve_web_port("不是端口", None) == 0
    assert _resolve_web_port(0, None) == 0
    assert _resolve_web_port(-5, None) == 0


def test_resolve_web_port_falls_back_to_config_default() -> None:
    """没给 `--port` 时取配置默认值（用"是否被占用"提示需要它）。"""
    from bili_danmaku.cli import _resolve_web_port

    port = _resolve_web_port(None, None)
    assert port > 0, "配置默认端口应当能解析出来"
