"""面板的托管运行时：后台启动、状态文件、优雅停止。

为什么需要这个模块（而不是"再开一个终端"）：

* `bili-danmaku web` 是**前台阻塞**进程，关掉终端就停；
  要"常驻"就必须能分离启动（`DETACHED_PROCESS`）并把 stdout/stderr 落到文件。
* 分离之后**没有终端可以 Ctrl+C**，所以必须另有一条**可控的优雅停止**通道。

Windows 上的两个实测结论决定了这里的实现：

* `CTRL_BREAK_EVENT` 对独立进程组里的 uvicorn **无效**（8 秒不退出）；
* `taskkill /PID`（不带 `/F`）被系统拒绝（`Access denied`）。

所以停止流程是「**让服务自己退出**」：CLI 读状态文件 → 向仅回环可访问的
`POST /api/admin/shutdown` 发请求 → 服务端把 `Server.should_exit` 置位 →
uvicorn 正常收尾 → `lifespan` 的 `finally` 调 `panel.stop()` → 每个房间
`bus.stop()` 把队列里没落盘的批次 flush 掉。**这条路是唯一保证不丢数据的。**
超时才退化为强杀（会丢队列中几百毫秒的批次，因此要明确告知用户）。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("bili_danmaku.runtime")

#: 状态目录（工作区内，已 gitignore）
RUNTIME_DIR = Path(".runtime")
STATE_FILE = RUNTIME_DIR / "web.json"
LOG_FILE = RUNTIME_DIR / "web.log"

#: 优雅停止后等待进程退出的秒数；超时才强杀
STOP_WAIT_SECONDS = 30.0

#: 控制端点路径（只做停止，不做别的；越少越好）
SHUTDOWN_PATH = "/api/admin/shutdown"


# --------------------------------------------------------------------------- #
# 进程存活判定
# --------------------------------------------------------------------------- #


def pid_alive(pid: int) -> bool:
    """判断进程是否还活着。

    用 `OpenProcess`（Windows）/ `kill(pid, 0)`（POSIX），**不做进程名匹配** ——
    匹配 `python.exe` 会撞到别的 Python 进程，反而误判。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
            handle, ctypes.byref(exit_code)
        )
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        # STILL_ACTIVE == 259
        return bool(ok) and exit_code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #


@dataclass
class WebRuntime:
    """一个正在运行（或刚退出的）面板实例。"""

    pid: int
    host: str
    port: int
    url: str
    rooms: list[str]
    out_dir: str
    formats: list[str]
    content_only: bool
    started_at: float
    log_path: str
    frontend_built: bool
    version: str = ""

    @property
    def uptime(self) -> float:
        return max(time.time() - self.started_at, 0.0)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["uptime_s"] = round(self.uptime, 1)
        return data

    def describe(self) -> str:
        hours, remainder = divmod(int(self.uptime), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime = f"{hours}h{minutes}m" if hours else (f"{minutes}m{seconds}s" if minutes else f"{seconds}s")
        rooms = ", ".join(self.rooms) if self.rooms else "（无，可在页面上添加）"
        return (
            f"地址        {self.url}\n"
            f"pid         {self.pid}（已运行 {uptime}）\n"
            f"预置房间    {rooms}\n"
            f"落盘格式    {', '.join(self.formats)}  →  {self.out_dir}\n"
            f"采集范围    {'仅文字弹幕 / SC' if self.content_only else '全部事件'}\n"
            f"前端        {'已构建' if self.frontend_built else '未构建（降级页）'}\n"
            f"日志        {self.log_path}"
        )


def state_path(state_file: str | Path | None = None) -> Path:
    return Path(state_file) if state_file else STATE_FILE


def load_state(state_file: str | Path | None = None) -> WebRuntime | None:
    """读状态文件；不存在或损坏返回 None。"""
    path = state_path(state_file)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return WebRuntime(
            pid=int(payload["pid"]),
            host=str(payload["host"]),
            port=int(payload["port"]),
            url=str(payload["url"]),
            rooms=[str(item) for item in payload.get("rooms") or []],
            out_dir=str(payload.get("out_dir") or ""),
            formats=[str(item) for item in payload.get("formats") or []],
            content_only=bool(payload.get("content_only", True)),
            started_at=float(payload.get("started_at") or 0.0),
            log_path=str(payload.get("log_path") or ""),
            frontend_built=bool(payload.get("frontend_built", False)),
            version=str(payload.get("version") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("状态文件损坏，将忽略：%s（%s）", path, exc)
        return None


def save_state(runtime: WebRuntime, state_file: str | Path | None = None) -> Path:
    path = state_path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(runtime)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_state(state_file: str | Path | None = None) -> bool:
    path = state_path(state_file)
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:  # pragma: no cover - 文件被占用等
        log.warning("无法删除状态文件 %s: %s", path, exc)
        return False


def live_runtime(state_file: str | Path | None = None) -> WebRuntime | None:
    """返回**确实还活着**的实例；陈旧状态文件会被自动清理。

    这一步是"崩溃后不自动重启、但下次启动要能起来"的关键：
    进程被强杀时没有机会删状态文件，于是下次 `web start` 会因为
    "已有实例"而拒绝启动 —— 所以每次读取都要顺手校验 pid。
    """
    runtime = load_state(state_file)
    if runtime is None:
        return None
    if pid_alive(runtime.pid):
        return runtime
    log.info("状态文件里的进程 %s 已不存在，清理陈旧记录", runtime.pid)
    clear_state(state_file)
    return None


# --------------------------------------------------------------------------- #
# 分离启动
# --------------------------------------------------------------------------- #


def detached_flags() -> int:
    """把子进程从当前终端/控制台里摘出去。"""
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return 0


def _hidden_startup_info() -> Any:
    """Windows：显式隐藏窗口。

    只靠 `creationflags` 不够 —— 父进程是控制台程序时，子进程仍可能继承
    控制台/标准句柄，于是"关掉父终端"会连带把它带走（实测：面板起来后
    父进程一退出，子进程也跟着消失）。
    `STARTF_USESHOWWINDOW` + `SW_HIDE` 是让它在后台安静运行的标准做法。
    """
    if os.name != "nt":
        return None
    import subprocess

    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0  # SW_HIDE
    return info


def _venv_root() -> Path | None:
    """若当前运行在 venv 里，返回 venv 根目录；否则 None。

    判据用 **`pyvenv.cfg` 是否在 `sys.executable` 附近**（Windows 布局是
    `.venv\\Scripts\\python.exe` + `.venv\\pyvenv.cfg`），比看 `VIRTUAL_ENV`
    环境变量可靠 —— 后者在"手动调 venv 里的 python"时经常是空的。
    """
    for parent in Path(sys.executable).parents:
        if (parent / "pyvenv.cfg").is_file():
            return parent
    return None


def child_python() -> tuple[str, dict[str, str]]:
    """返回 `(可执行文件, 要注入的环境变量)`，用于启动**长期存活的面板子进程**。

    为什么不能用 `sys.executable`（= venv 的 `python.exe`）：它在 Windows 上是
    **转发器 stub**，会再起一个 base 解释器，而那个 base 解释器是 **console 程序**、
    又拿不到父进程的 console ⇒ **Windows 给它新分配一个 conhost**。
    用户看到的就是"`web start` 之后多出来一个空终端窗口"，而且**关掉它面板就停**
    （详见本模块关于 console 的说明）。

    绕开 stub 直接起 base 解释器后，顶层进程本身就是 server ⇒
    **没有多余的 console 可关**，那个缺陷从根上消失。

    代价是 base 解释器不认识 venv 的 `sys.path`，所以把**当前进程的 `sys.path`
    原样注入** `PYTHONPATH`：

    * 这比"猜 `src/` 与 venv 的 site-packages"稳 —— `sys.path` 是**事实**，
      而且顺带覆盖了 `pip install -e .` 的 `.pth` 展开结果；
    * 只保留**真实存在**的绝对路径：空串会被解释成当前目录（安全隐患），
      相对路径则依赖 cwd，两者都不能塞进 `PYTHONPATH`。

    拿不到 base 解释器时**退回 stub**（返回空 env）—— 宁可保留"多余 console"
    这个问题，也不要起不来。
    """
    base = getattr(sys, "_base_executable", None)
    if not base or not Path(base).is_file():
        return sys.executable, {}

    entries: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry)
        if candidate.is_absolute() and candidate.is_dir():
            text = str(candidate)
            if text not in entries:
                entries.append(text)

    env: dict[str, str] = {}
    if entries:
        env["PYTHONPATH"] = os.pathsep.join(entries)
    venv = _venv_root()
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
    # 不让 base 解释器受用户级 site-packages 影响，保证与 venv 行为一致
    env["PYTHONNOUSERSITE"] = "1"
    return str(base), env


def spawn_detached(
    argv: list[str],
    *,
    log_path: Path,
    cwd: str | Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.Popen:
    """后台启动一个进程，stdout/stderr 追加到日志文件。

    日志**必须是文件**：分离后的进程没有终端可写，继承的句柄可能已经失效。

    注意 `close_fds`：Windows 上也要传 **True**。我们显式重定向了
    stdout/stderr/ stdin，子进程**不需要**再继承任何句柄；否则它会一直持有
    父进程的 stdout 管道写端，导致 `bili-danmaku web start | ...` 这种管道
    永远等不到 EOF（实测：`web start` 命令本身早就打印完并退出了，
    PowerShell 却在管道上挂了几分钟）。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 参数里混进 None 时 Popen 会把它 str() 成 "None"（Windows 上尤其隐蔽），
    # 这里直接拦下来，避免子进程拿着非法参数启动失败后难以定位。
    for item in argv:
        if item is None or not isinstance(item, str):
            raise ValueError(f"argv 里出现非字符串参数: {item!r}（完整参数: {argv}）")

    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
        # PYTHONHOME 会盖掉 venv 注入的 sys.path，必须清掉（见 child_python）
        env.pop("PYTHONHOME", None)

    handle = log_path.open("a", encoding="utf-8", buffering=1)
    try:
        return subprocess.Popen(  # noqa: S603 - 参数由本进程自己拼，无外部输入
            argv,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            creationflags=detached_flags(),
            startupinfo=_hidden_startup_info(),
            close_fds=True,
            env=env,
        )
    except OSError as exc:
        # WinError 2 的报错信息（"系统找不到指定的文件"）与真实原因经常不相干，
        # 所以把实际参数一起带出来，避免下次又要靠猜。
        raise OSError(
            f"启动后台进程失败: {type(exc).__name__}: {exc}\n"
            f"  executable = {argv[0]!r}（存在: {Path(argv[0]).is_file()}）\n"
            f"  argv       = {argv!r}\n"
            f"  cwd        = {os.getcwd()!r}\n"
            f"  flags      = {detached_flags():#010x}"
        ) from exc
    finally:
        # 父进程持有的句柄要关掉，否则子进程日志不会被及时刷出/无法删除
        handle.close()


def server_argv(*args: str) -> list[str]:
    """构造后台进程的命令行：`python -m bili_danmaku web <参数>`。

    用 `-m bili_danmaku`（`__main__.py`）而不是 `bili-danmaku.exe`：
    前者在"未安装 console script"的环境里也能跑，且不受 PATH 影响。

    解释器选择见 `child_python()` —— **刻意避开 venv 的转发器 stub**，
    否则会多出一个 console 窗口，并造成"关终端连坐停面板"。
    """
    executable, _env = child_python()
    return [executable, "-m", "bili_danmaku", *args]


# --------------------------------------------------------------------------- #
# 优雅停止
# --------------------------------------------------------------------------- #


def shutdown_url(runtime: WebRuntime) -> str:
    return f"http://{runtime.host}:{runtime.port}{SHUTDOWN_PATH}"


def request_shutdown(runtime: WebRuntime, *, token: str = "", timeout: float = 5.0) -> tuple[bool, str]:
    """向控制端点发一次停止请求，返回 `(是否被接受, 说明)`。"""
    request = urllib.request.Request(shutdown_url(runtime), method="POST", data=b"")
    if token:
        request.add_header("X-Bili-Token", token)
    # 让服务端能校验来源（CSRF 防护）
    request.add_header("Origin", f"http://{runtime.host}:{runtime.port}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status == 200, body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return False, f"HTTP {exc.code}: {detail[:200]}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def wait_pid_exit(pid: int, timeout: float, *, interval: float = 0.2) -> bool:
    """等进程退出；返回是否在超时前退出。"""
    deadline = time.monotonic() + max(timeout, 0.0)
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(interval)
    return not pid_alive(pid)


def force_kill(pid: int) -> tuple[bool, str]:
    """强杀（**会丢队列里未落盘的批次**，只在优雅停止失效时使用）。"""
    if os.name == "nt":
        result = subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        message = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, message
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return False, str(exc)
    return True, "SIGKILL"


def stop_runtime(
    runtime: WebRuntime,
    *,
    token: str = "",
    wait: float = STOP_WAIT_SECONDS,
    allow_force: bool = True,
    state_file: str | Path | None = None,
) -> tuple[bool, str]:
    """优雅停止一个面板实例。

    返回 `(是否真的停了, 过程说明)`。说明里会写清用的是哪条路径 ——
    因为强杀意味着"可能丢了少量未落盘数据"，必须让用户看见。
    """
    if not pid_alive(runtime.pid):
        clear_state(state_file)
        return True, "进程已不在运行（已清理陈旧状态文件）"

    accepted, detail = request_shutdown(runtime, token=token)
    if not accepted:
        if not allow_force:
            return False, f"优雅停止请求未被接受（{detail}），且未允许强杀"
        killed, kill_detail = force_kill(runtime.pid)
        clear_state(state_file)
        if killed and wait_pid_exit(runtime.pid, 5.0):
            return True, f"优雅停止不可用（{detail}），已强杀（可能丢失少量未落盘数据）"
        return False, f"无法停止：{detail}；强杀结果={kill_detail}"

    if wait_pid_exit(runtime.pid, wait):
        clear_state(state_file)
        return True, f"已优雅停止（数据已 flush 落盘）；服务端回应：{detail[:120]}"

    if not allow_force:
        return False, f"等待 {wait:.0f}s 后进程仍在，未允许强杀"
    killed, kill_detail = force_kill(runtime.pid)
    clear_state(state_file)
    if killed:
        return True, f"等待 {wait:.0f}s 未退出，已强杀（可能丢失少量未落盘数据）：{kill_detail[:120]}"
    return False, f"等待超时且强杀失败：{kill_detail}"
