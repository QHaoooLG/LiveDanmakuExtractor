"""启动器核心：**在同一进程内**托管面板，供托盘应用与单文件 exe 使用。

为什么要有这个模块（对应 DEC-6 / HANDOVER §5.13）：

* `cli.py::_run_web_foreground()` 是**前台阻塞**的，且 `web start` 会 spawn 子进程 ——
  而那条路径在 Windows 上有个已定位的缺陷：venv 的 `python.exe` 是转发器 stub，
  它起的 base 解释器会**新分配一个 console**，关掉那个窗口就连坐杀掉面板
  （见 HANDOVER §5.12）。
* 把 uvicorn 跑在**本进程的一个后台线程**里，就完全没有子进程、没有多余 console，
  那个缺陷在架构上消失；同时托盘/启动面板可以直接读 `hub` 状态，不必发 HTTP。

本模块刻意**不碰 GUI**（tkinter/ctypes 在 `gui/` 里），因此可以完全离线单测：
`start()` 用一个后台线程跑 uvicorn，`stop()` 走优雅路径（`should_exit` →
lifespan 的 `panel.stop()` → 各房间 `bus.stop()` flush 落盘）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .login import LoginStatus, QrLoginSession, check_status, local_status

log = logging.getLogger("bili_danmaku.launcher")

#: 重新导出，保持 `from bili_danmaku.launcher import LoginStatus` 这类旧写法可用。
#: 登录逻辑本身已搬到 `login.py`（见那边的说明），这里只是转出。
__all__ = ["LauncherOptions", "LoginStatus", "PanelController", "PanelStatus", "QrLoginSession"]

#: 等待"服务真的起来 / 真的停掉"的默认超时（秒）
DEFAULT_START_TIMEOUT = 20.0
DEFAULT_STOP_TIMEOUT = 30.0


@dataclass
class LauncherOptions:
    """启动面板需要的全部参数（与 `cli.py` 的 web 选项一一对应）。"""

    rooms: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = 8787
    out_dir: str = "data"
    formats: tuple[str, ...] = ("jsonl", "csv", "sqlite")
    keep_raw: bool = True
    content_only: bool = True
    cookie_file: str | None = None
    config_path: str | None = None
    open_browser: bool = False
    #: 日志文件路径。留空时**自动**判断：冻结运行（exe）或没有可用 stderr 时
    #: 落到 `.runtime/web.log`（见 `PanelController._default_log_file`）。
    log_file: str = ""


@dataclass
class PanelStatus:
    """一次状态快照（供启动面板/托盘菜单渲染）。"""

    running: bool
    url: str = ""
    host: str = ""
    port: int = 0
    rooms: tuple[str, ...] = ()
    started_at: float = 0.0
    uptime_s: float = 0.0
    published: int = 0
    dropped: int = 0
    frontend_built: bool = False
    dist: str = ""
    error: str = ""


class PanelController:
    """在**本进程后台线程**里托管一个面板实例。

    生命周期：
        start() → （uvicorn 在子线程 serve）→ stop() → 线程 join

    线程安全：`start/stop` 各自加锁；重复 `start()` 不会起第二个实例。
    """

    def __init__(self, options: LauncherOptions | None = None) -> None:
        self.options = options or LauncherOptions()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._loop: Any = None
        self._panel: Any = None
        self._hub: Any = None
        self._config: Any = None
        self._port = 0
        self._started_at = 0.0
        self._last_error = ""
        self._shutdown_event = threading.Event()

    # ------------------------------------------------------------------ #
    # 内部：构建 config / 依赖
    # ------------------------------------------------------------------ #

    def _free_port(self, host: str) -> int:
        """挑一个当前空闲的端口（走 `bind_port` 的"系统分配"分支）。

        早期 `server.bind_port(host, 0)` 会返回**错误的端口号**（实测返回 `1`），
        所以这里曾自己 `bind(:0)` 绕开它。该缺陷已在 `bind_port` 里修掉
        （返回值一律取自 `getsockname()`），于是改为直接复用 ——
        少一份重复实现，也就少一处行为漂移。
        """
        from .server import bind_port

        return bind_port(host, 0)

    def _build_config(self) -> Any:
        """组装 `Config`。

        刻意**不经过** `cli._build_config(ctx, overrides)` —— 那个辅助函数要求一个
        `click.Context`（因为 `--config` 的值存在 `ctx.obj` 里）。启动器/core 不该依赖
        click 的调用栈，所以这里直接调 `Config.load` 并复用同一套 setup_logging。
        选项语义与 `cli._web_config` 保持一致。
        """
        from .config import Config
        from .utils import setup_logging

        overrides: dict[str, Any] = {}
        if self.options.out_dir:
            overrides.setdefault("sinks", {})["out_dir"] = self.options.out_dir
        if self.options.formats:
            overrides.setdefault("sinks", {})["types"] = list(self.options.formats)
        overrides.setdefault("sinks", {})["keep_raw"] = self.options.keep_raw
        overrides.setdefault("general", {})["content_only"] = self.options.content_only
        overrides.setdefault("web", {})["host"] = self.options.host
        overrides.setdefault("web", {})["port"] = self.options.port

        config = Config.load(self.options.config_path, overrides=overrides or None)
        # 关键：**GUI/无控制台场景必须落日志文件**。
        # `setup_logging` 默认只写 stderr，而 `--windowed` 打包的进程没有 stderr，
        # 于是"退出后 web.log 是 0 字节"（实测踩到）。
        #
        # 这里做**默认兜底**而不是只依赖调用方传参：只要进程没有可用的 stderr
        # （冻结运行、或 stdout/stderr 为 None），就自动落到 `.runtime/web.log`。
        # 调用方显式给了 `log_file` 则以它为准。
        setup_logging(
            config.general.log_level,
            log_file=self.options.log_file or self._default_log_file() or None,
        )
        return config

    @staticmethod
    def _default_log_file() -> str:
        """没有可用 stderr 时，返回默认日志文件路径（否则空串 = 只用 stderr）。"""
        frozen = bool(getattr(sys, "frozen", False))
        no_streams = sys.stderr is None or sys.stdout is None
        if not (frozen or no_streams):
            return ""
        from .runtime import LOG_FILE

        return str(LOG_FILE)

    def _uvicorn_log_config(self, config: Any) -> dict[str, Any]:
        """让 uvicorn 的 logger 也写进同一份日志文件（没有文件时退回默认行为）。

        uvicorn 的 `uvicorn.error`/`uvicorn.access` 是 `propagate=False` 且自带
        handler 的独立 logger，`dictConfig` 会**接管**它们 —— 所以这里直接给出
        与 `setup_logging` 一致的格式与目标。
        """
        from .utils import _DATE_FORMAT, _LOG_FORMAT  # noqa: PLC0415 - 与日志模块同源

        level = str(config.general.log_level).upper()
        handlers: list[str] = ["stderr"]
        handler_defs: dict[str, Any] = {
            "stderr": {"class": "logging.StreamHandler", "formatter": "plain", "stream": "ext://sys.stderr"}
        }
        log_file = self.options.log_file or self._default_log_file()
        if log_file:
            handler_defs["file"] = {
                "class": "logging.FileHandler",
                "formatter": "plain",
                "filename": log_file,
                "mode": "a",
                "encoding": "utf-8",
            }
            handlers.append("file")

        return {
            "version": 1,
            # 不要 disable_existing_loggers：否则会掐掉项目自己的 logger
            "disable_existing_loggers": False,
            "formatters": {"plain": {"format": _LOG_FORMAT, "datefmt": _DATE_FORMAT}},
            "handlers": handler_defs,
            "loggers": {
                "uvicorn": {"handlers": handlers, "level": level, "propagate": False},
                "uvicorn.error": {"handlers": handlers, "level": level, "propagate": False},
                "uvicorn.access": {"handlers": handlers, "level": "WARNING", "propagate": False},
            },
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self, *, wait: bool = True, timeout: float = DEFAULT_START_TIMEOUT) -> PanelStatus:
        """启动面板。返回启动后的状态（`running=False` + `error` 表示失败）。

        `wait=True` 会等到**服务真的接受连接**（或超时）才返回，
        这样调用方拿到状态就能放心 `webbrowser.open()`。
        """
        with self._lock:
            if self.is_running():
                return self.status()

            import uvicorn

            from .server import PanelService, WebHub, bind_port, create_app, find_dist
            # `_StopHandle` 定义在 cli.py（控制端点只依赖这个可调用对象，
            # 不依赖 uvicorn 内部结构）；这里复用它，避免第二份实现漂移。
            from .cli import _StopHandle

            try:
                config = self._build_config()
            except Exception as exc:  # 配置非法：明确报错，不要半启动
                self._last_error = f"配置无效：{type(exc).__name__}: {exc}"
                log.error(self._last_error)
                return self.status()

            # 先落到 self 上再往下走：`_load_credentials()` 要读 `self._config`。
            # （曾经把它排在 `_load_credentials()` 之后，直接 AttributeError。）
            self._config = config

            # 端口自适应：port=0 表示"自己挑一个空闲端口"（verify 与随机端口场景用）。
            if config.web.port:
                actual = bind_port(config.web.host, config.web.port)
            else:
                actual = self._free_port(config.web.host)
            dist = find_dist()

            credentials = self._load_credentials()
            hub = WebHub()
            panel = PanelService(config, credentials, hub)

            handle = _StopHandle()
            app = create_app(
                config,
                credentials,
                hub,
                panel,
                initial_rooms=self.options.rooms,
                on_shutdown=handle.request,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=config.web.host,
                    port=actual,
                    log_level=str(config.general.log_level).lower(),
                    access_log=False,
                    # uvicorn 有自己的 logger（`uvicorn.error`/`uvicorn.access`），
                    # `propagate=False` 且自带 handler ⇒ **不会**经过根 logger 的文件
                    # handler。托盘/exe 没有 stderr 时那些 "Started server process"
                    # 就全丢了。这里显式让它写同一份日志文件。
                    log_config=self._uvicorn_log_config(config),
                )
            )
            handle.server = server

            self._hub = hub
            self._panel = panel
            self._server = server
            self._port = actual
            self._started_at = time.time()
            self._last_error = ""
            self._shutdown_event.clear()

            self._thread = threading.Thread(
                target=self._serve, name="panel-uvicorn", daemon=True
            )
            self._thread.start()

        if wait:
            self._wait_until_listening(timeout=timeout)
        if self.options.open_browser and self.is_running():
            self._open_browser_async()
        return self.status()

    def _serve(self) -> None:
        """子线程入口：跑 uvicorn 直到 `should_exit` 被置位。

        顺带记下**这个线程的事件循环**：`PanelService.add_room` 是协程，
        必须回到同一个循环里执行（它的内部状态绑定在那个 loop 上）。
        主线程要加房间时用 `asyncio.run_coroutine_threadsafe` 丢回来。
        """
        import asyncio

        async def _main() -> None:
            self._loop = asyncio.get_running_loop()
            try:
                await self._server.serve()
            finally:
                self._loop = None

        try:
            asyncio.run(_main())
        except Exception as exc:  # pragma: no cover - 子线程里的意外
            self._last_error = f"服务线程异常：{type(exc).__name__}: {exc}"
            log.exception("面板服务线程退出")
        finally:
            self._shutdown_event.set()

    def _wait_until_listening(self, *, timeout: float) -> None:
        """轮询端口，直到能建立 TCP 连接（说明 uvicorn 已完成启动）。"""
        import socket

        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() < deadline:
            if self._thread is not None and not self._thread.is_alive():
                if not self._last_error:
                    self._last_error = "服务线程已退出（可能是端口或配置问题），详见日志"
                return
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect((self.options.host, self._port))
                    return
                except OSError:
                    time.sleep(0.1)

    def _open_browser_async(self) -> None:
        import webbrowser

        url = self.url
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    def stop(self, *, timeout: float = DEFAULT_STOP_TIMEOUT) -> PanelStatus:
        """优雅停止：置位 `should_exit` → uvicorn 收尾 → `panel.stop()` flush 落盘。

        超时也不强杀（**强杀会丢未落盘批次**）；把实际情况如实报给调用方。
        """
        with self._lock:
            thread = self._thread
            server = self._server
            if server is None or thread is None:
                return self.status()

            server.should_exit = True
            self._shutdown_event.set()

        if thread.is_alive():
            thread.join(timeout=timeout)
        if thread.is_alive():
            self._last_error = f"等待 {timeout:.0f}s 后服务线程仍在运行（未强杀，以免丢数据）"
            log.warning(self._last_error)
        else:
            with self._lock:
                self._thread = None
                self._server = None
        return self.status()

    def restart(self, **kwargs: Any) -> PanelStatus:
        self.stop()
        return self.start(**kwargs)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def url(self) -> str:
        return f"http://{self.options.host}:{self._port}/" if self._port else ""

    def status(self) -> PanelStatus:
        """读一次状态快照；`published`/`dropped` 直接来自**同进程的 hub**。"""
        hub = self._hub
        published = dropped = 0
        if hub is not None:
            try:
                # 属性名以 hub.py 为准：published（自启动累计）/ dropped。
                # 注意不要写成 cumulative_total —— 那是 /api/health 的**响应字段名**，
                # 不是 WebHub 上的属性（实测 GET 过，踩过这个坑）。
                published = int(getattr(hub, "published", 0) or 0)
                dropped = int(getattr(hub, "dropped", 0) or 0)
            except Exception:  # pragma: no cover - 防御性
                pass

        from .server import find_dist

        dist = find_dist()
        running = self.is_running()
        return PanelStatus(
            running=running,
            url=self.url,
            host=self.options.host,
            port=self._port,
            rooms=tuple(self.options.rooms),
            started_at=self._started_at,
            uptime_s=(time.time() - self._started_at) if self._started_at else 0.0,
            published=published,
            dropped=dropped,
            frontend_built=dist is not None,
            dist=str(dist or ""),
            error="" if running or not self._last_error else self._last_error,
        )

    # ------------------------------------------------------------------ #
    # 房间
    # ------------------------------------------------------------------ #

    def add_room(self, room: str) -> tuple[bool, str]:
        """加一个房间（走 `PanelService`，与 Web 端同一条路径）。

        `PanelService.add_room` 是**协程**，且其状态绑定在服务线程的 loop 上，
        所以这里必须用 `run_coroutine_threadsafe` 丢回那个 loop，而不是
        新开一个 `asyncio.run`（跨 loop 用同一个 service 会出问题）。
        """
        panel = self._panel
        loop = self._loop
        if panel is None:
            return False, "面板未启动"
        if loop is None or not loop.is_running():
            return False, "面板服务线程未就绪"

        import asyncio

        future = asyncio.run_coroutine_threadsafe(panel.add_room(room), loop)
        try:
            runtime = future.result(timeout=30.0)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, str(getattr(runtime, "room_id", runtime))

    # ------------------------------------------------------------------ #
    # 凭据
    # ------------------------------------------------------------------ #

    def _load_credentials(self) -> Any:
        """加载凭据；失败不阻断启动（面板仍可只读浏览历史）。"""
        from .cli import _load_creds

        try:
            return _load_creds(self._config, self.options.cookie_file)
        except SystemExit:
            log.warning("凭据加载失败（或缺失），面板将以无凭据状态启动")
            return None

    def local_credential_status(self) -> LoginStatus:
        """**不联网**看一眼本地凭据（首屏立刻有说法）。实现见 `login.local_status`。"""
        return local_status(self.options.cookie_file, config=self._config)

    def check_login(self, *, timeout: float = 10.0) -> LoginStatus:
        """**联网**校验凭据是否仍有效（判断"凭证失效"的权威判据）。

        实现搬到 `login.check_status()` —— 那边是**与界面无关**的独立模块，
        所以能直接单测，不必构造整个 `PanelController`（这正是这次拆分的目的）。
        配置还没建出来时（面板没启动过）先建一个，否则拿不到 `api` 段。
        """
        config = self._config
        if config is None:
            try:
                config = self._build_config()
            except Exception as exc:  # noqa: BLE001 - 建配置失败也要给个可读结论
                return LoginStatus(message=f"无法读取配置：{type(exc).__name__}: {exc}")
        return check_status(
            self.options.cookie_file, config=config, timeout=timeout
        )

    # ------------------------------------------------------------------ #
    # 扫码登录（GUI 用；实现在 `login.QrLoginSession`）
    # ------------------------------------------------------------------ #

    def new_login_session(self, **kwargs: Any) -> QrLoginSession:
        """建一个扫码登录会话（凭据文件跟随本控制器的配置）。"""
        return QrLoginSession(cookie_file=self.options.cookie_file, **kwargs)
