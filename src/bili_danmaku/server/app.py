"""FastAPI 应用：REST + WebSocket。

前端是构建产物（`web/dist`）；**若前端未构建，服务仍会启动**并给出一个
内建提示页，而不是直接报错 —— 面板不该因为缺 Node 而让整个工具不可用。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Sequence

import anyio
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .. import __version__
from .. import store as store_module
from ..analysis import analyze, assess_health, words_from_buckets
from ..annotations import AnnotationStore, resolve_annotations_path, summarize
from ..config import Config
from ..credentials import Credentials
from ..models import Event
from ..rooms import parse_room_input
from ..store import DanmakuStore, StoreError
from ..text import add_custom_words, jieba_available, load_stopwords
from .hub import ANALYSIS_BUCKET_S, ANALYSIS_WINDOW_MESSAGES, TooManySubscribers, WebHub
from .panel import PanelService

log = logging.getLogger("bili_danmaku.server.app")

#: WS 空闲多久后发一次探测包（用于发现对端已断开）
WS_IDLE_PROBE_SECONDS = 15.0
#: 控制类消息（stats）推送间隔
STATS_PUSH_SECONDS = 2.0
#: `/api/history` 在**没有** room_id 过滤时的兜底时间窗（秒）。
#: 实测无房间过滤会退化成全表 SCAN + 临时排序（39.8 ms/页），加窗后退回索引区间扫描。
HISTORY_PAGE_WINDOW_S = 3600.0

#: CSV 导出的 UTF-8 BOM。中文版 Excel 会按系统码页（GBK）打开无 BOM 的 CSV，
#: 导致中文全部乱码 —— 浏览器下载的文件又没有"charset"线索可用，所以必须带 BOM。
#: 与本机 CLI 导出（`cli._open_output` 用 `utf-8-sig`）保持一致。
CSV_BOM = "\ufeff"

FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>bili-danmaku 面板</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#14161a;color:#e6e6e6;
      margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
 .card{max-width:640px;padding:32px 36px;background:#1c1f26;border:1px solid #2c313a;border-radius:12px}
 h1{font-size:20px;margin:0 0 12px}
 code{background:#0f1115;padding:2px 6px;border-radius:4px;color:#7dd3fc}
 ol{line-height:1.9;padding-left:22px}
 .muted{color:#8b93a1;font-size:13px;margin-top:18px}
</style></head><body><div class="card">
<h1>前端尚未构建</h1>
<p>后端已正常运行，但没找到 <code>web/dist</code> 下的前端产物。</p>
<ol>
 <li><code>cd web</code></li>
  <li><code>npm install</code></li>
  <li><code>npm run build</code></li>
  <li>刷新本页</li>
</ol>
<p class="muted">REST 与 WebSocket 接口不受影响，可直接访问
<code>/api/health</code>、<code>/api/rooms</code>、<code>/ws/live</code>。</p>
</div></body></html>
"""


class AddRoomBody(BaseModel):
    room: int | str = Field(..., description="房间号（支持短号）")


class BookmarkBody(BaseModel):
    """新增标记的请求体（V3-a2）。

    `recv_ts` 必填：它是排序与区间导出的唯一依据。
    `anchor_id` 可选：非空表示"钉在某条弹幕上"，被钉的弹幕后来被清理也不影响标注可用。
    """

    room_id: int = Field(..., description="房间号（真实房间号）")
    recv_ts: float = Field(..., gt=0, description="标记的时间点（秒级时间戳）")
    label: str | None = Field(None, max_length=120, description="短标签，缺省「标记」")
    note: str | None = Field(None, max_length=2000, description="备注")
    session_id: str | None = None
    anchor_id: int | None = Field(None, description="钉住的弹幕 id（可空 = 纯时间点）")


def find_dist() -> Path | None:
    """定位前端构建产物目录。

    候选位置按"离包最近"排序：

    0. **`<sys._MEIPASS>/web_dist`** —— **仅冻结（PyInstaller）时存在**。
       见下方"冻结场景"说明，这是打包成 exe 后**唯一**能命中的候选；
    1. `bili_danmaku/web_dist` —— **wheel 里随包分发**的前端（见 `tools/build_wheel.py`）。
       装完即可用，不依赖工作区目录结构；
    2. `./web/dist` —— 在项目根目录启动时使用（开发与源码运行的常规路径）；
    3. `<仓库根>/web/dist` —— 从别的工作目录启动、但包仍在源码树里时的兜底。

    都找不到就返回 None，调用方会返回内建提示页 —— 面板不该因为缺 Node 而不可用。

    **冻结场景（必须理解，否则打包出来的 exe 只会显示降级页）**：
    PyInstaller 会把 `bili_danmaku` 打进 `base_library.zip`，于是
    `bili_danmaku.__file__` 是一个**合成路径**（形如
    `<_MEIPASS>/bili_danmaku/__init__.py`），**磁盘上并没有这个目录** ——
    所以候选 1 在冻结后必然落空。而 `--add-data "<web/dist>;web_dist"` 会把前端放到
    `<_MEIPASS>/web_dist`。实测确认：onefile 与 onedir 两种模式都会因此让
    `find_dist()` 返回 None。

    冻结时的候选按"最可能命中"排序：`_MEIPASS/web_dist`（onefile 与 onedir 都成立）
    → `<exe目录>/_internal/web_dist`（onedir 的等价位置）
    → `<exe目录>/web_dist`（把前端放在 exe 旁边的自定义布局）。
    多列几个是刻意的：`sys._MEIPASS` 在个别 PyInstaller 版本/模式下未必等于
    `_internal`，而**打包场景下没有工作区兜底**，命中不了就只剩降级页。
    """
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "web_dist")
    if getattr(sys, "frozen", False):
        # 冻结时分发目录同样值得找：onedir 的 _internal、以及"前端放 exe 旁边"的布局。
        # 只加一层，避免在别的工作目录里把无关的 web_dist 捞进来。
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "_internal" / "web_dist")
        candidates.append(exe_dir / "web_dist")

    candidates += [
        Path(__file__).resolve().parent.parent / "web_dist",
        Path.cwd() / "web" / "dist",
        Path(__file__).resolve().parents[3] / "web" / "dist",
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def bind_port(host: str, port: int, span: int = 20) -> int:
    """返回一个可用端口：port → port+1…port+span → 交由系统分配。

    刻意**不设 SO_REUSEADDR**：Windows 上它会像 SO_REUSEPORT 一样
    允许"抢占"已占用的端口，从而把冲突检测变成假阳性。

    **`port=0` 表示"交给系统分配"**（绑到 :0 取内核给的临时端口），
    此时不做范围扫描 —— 传 0 的调用方要的就是一个随机可用端口。

    注意：本函数返回**它实际绑定成功**的那个端口号。
    早期实现在 `port=0` 时把 0 也丢进了扫描列表，于是 `bind(("127.0.0.1", 0))`
    成功、却把 `sock.getsockname()` 之前写死的 `candidate`（迭代值 0）当成结果返回，
    调用方拿到 `0`（某些平台上表现为 `1`）这种**无效端口**，
    对外报出的地址根本连不上。改成下面的 `_try_bind` 后，返回值一律取自
    `getsockname()`，不会再出现"报了没绑上的端口"。
    """
    if port:
        # 先试请求的端口，再顺延 span 个
        for candidate in range(port, port + span + 1):
            bound = _try_bind(host, candidate)
            if bound is not None:
                return bound
    # 请求的端口（或整个顺延区间）都不可用：交系统分配
    bound = _try_bind(host, 0)
    if bound is not None:
        return bound
    raise RuntimeError("找不到可用端口")


def _try_bind(host: str, port: int) -> int | None:
    """尝试绑定 `host:port`；成功返回**内核实际分配的端口**，失败返回 None。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return None
        return int(sock.getsockname()[1])


def loopback_host(host: str) -> bool:
    """主机名/地址是否指向本机。

    用 `ipaddress` 而不是字符串前缀判断：这样才能正确覆盖
    `127.0.0.0/8` 整段、IPv6 的 `::1`、以及 IPv4-mapped 的 `::ffff:127.0.0.1`。
    """
    import ipaddress

    text = (host or "").strip().lower()
    if not text:
        return False
    if text == "localhost":
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    # IPv4-mapped IPv6（`::ffff:127.0.0.1`）：`is_loopback` 对它是 False，
    # 但语义上就是回环 —— 双栈/代理场景很常见，必须显式认出来。
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


#: 测试与外部引用的别名（避免下划线开头的私有名字跨模块用）
_loopback_host = loopback_host


def create_app(
    config: Config,
    credentials: Credentials,
    hub: WebHub,
    panel: PanelService,
    initial_rooms: tuple[int | str, ...] = (),
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。

    `on_shutdown`：被 `POST /api/admin/shutdown` 触发时要调用的回调，
    由 CLI 传成「把 uvicorn 的 `Server.should_exit` 置位」。
    走这条路退出时，`lifespan` 的 `finally` 会执行 `panel.stop()`，
    每个房间的 `bus.stop()` 会把队列里没落盘的批次 flush 掉。
    """
    dist = find_dist()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await panel.start()
        # 预置房间：单个失败（房间不存在/已达上限）不应阻止面板启动
        for room in initial_rooms:
            try:
                runtime = await panel.add_room(room)
                log.info("预置房间 %s 已启动采集", runtime.room.room_id)
            except Exception as exc:  # noqa: BLE001
                log.error("预置房间 %s 失败: %s", room, exc)

        push_task = asyncio.create_task(_stats_pusher(panel, hub), name="stats-pusher")
        # **后台预热采集库**（不阻塞启动）。
        #
        # 实测（`tools/probe_usage_flow.py`，真库 281,890 行）：
        # 面板起来后**第一次**开库的几个请求明显更慢 ——
        # `/api/sessions?limit=50` 冷 389 ms / 热 34 ms，
        # `/api/room-names` 冷 **2159 ms** / 热 360 ms。
        # 原因是那一次要把索引页与表页从磁盘拉进页缓存（WAL + 冷 cache）。
        # 这里在启动后**趁用户还没点**先跑一遍同样的轻量查询，把这份成本挪到空闲时。
        warm_task = asyncio.create_task(_warm_store(config), name="store-warmup")
        log.info("面板就绪：前端 %s", dist or "未构建（使用内建提示页）")
        try:
            yield
        finally:
            push_task.cancel()
            warm_task.cancel()
            await asyncio.gather(push_task, warm_task, return_exceptions=True)
            # 关键收尾：每个房间的 bus.stop() 会把队列里没落盘的批次 flush 掉。
            # 「停止面板」按钮 → 控制端点 → 这里的路径就是靠它保证不丢数据。
            await panel.stop()
            log.info("面板已停止（所有 sink 已 flush）")

    app = FastAPI(title="bili-danmaku", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.hub = hub
    app.state.panel = panel
    app.state.on_shutdown = on_shutdown

    # ------------------------------------------------------------------ #
    # 前端
    # ------------------------------------------------------------------ #

    if dist is not None:
        from fastapi.staticfiles import StaticFiles

        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        if dist is not None:
            return FileResponse(dist / "index.html")
        return HTMLResponse(FALLBACK_HTML)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Any:
        if dist is not None and (dist / "favicon.ico").is_file():
            return FileResponse(dist / "favicon.ico")
        return JSONResponse({"detail": "no favicon"}, status_code=404)

    # ------------------------------------------------------------------ #
    # 管理端点：停止面板
    #
    # 只有一个端点，而且只做"停止"。安全边界：
    #   * **只接受回环来源**：非本机请求一律 403 —— 即便面板监听 0.0.0.0，
    #     局域网也无权停服务；
    #   * **校验 Origin**：浏览器里的按钮是跨源友好的简单 POST，
    #     必须确认请求来自本机页面（缺失 Origin 视为非浏览器调用，放行）；
    #   * 配置了 `web.token` 时必须带上。
    # ------------------------------------------------------------------ #

    def _origin_ok(request: Request) -> bool:
        """校验 `Origin` 是否来自本机页面。

        只比对**主机名**、不比对端口：面板端口可变（被占用会自动换），
        写死端口会出现"换了端口就打不开"的坑；而回环主机名已足够约束来源
        （跨站请求的 Origin 一定是外部域名）。
        反代/外部域名场景本来就该配 `web.token`。
        """
        origin = request.headers.get("origin")
        if not origin:
            # 非浏览器调用（curl / CLI）没有 Origin，不校验
            return True
        from urllib.parse import urlparse

        hostname = urlparse(origin).hostname or ""
        return loopback_host(hostname)

    @app.post("/api/admin/shutdown")
    async def shutdown(request: Request) -> Any:
        if not loopback_host(request.client.host if request.client else ""):
            raise HTTPException(status_code=403, detail="仅允许本机请求停止面板")
        if not _origin_ok(request):
            raise HTTPException(status_code=403, detail="来源校验失败")
        if config.web.token and request.headers.get("x-bili-token") != config.web.token:
            raise HTTPException(status_code=403, detail="缺少或错误的 X-Bili-Token")

        handler: Callable[[], None] | None = getattr(app.state, "on_shutdown", None)
        if handler is None:  # pragma: no cover - 仅测试构造 app 时会走到
            raise HTTPException(status_code=409, detail="当前进程不支持从接口停止")

        if getattr(app.state, "shutdown_requested", False):
            return {"stopping": True, "note": "已接受过停止请求"}
        app.state.shutdown_requested = True
        log.info("收到停止请求，正在收尾（flush 落盘）…")
        # 用 BackgroundTask 而不是 `loop.call_later`：它的语义正是
        # "响应发完之后再执行"，而且跟着请求生命周期走 ——
        # `call_later` 在测试客户端（portal 事件循环）里根本不会被跑到。
        return JSONResponse(
            {"stopping": True, "note": "正在 flush 落盘并退出"},
            background=BackgroundTask(handler),
        )

    # ------------------------------------------------------------------ #
    # REST
    # ------------------------------------------------------------------ #

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """存活探针 + 前端状态 + **采集健康提示**。

        `attention` 是 `analysis.assess_health()` 的结果。放在这里而不是新开一个
        端点，是因为面板本来就在轮询 `/api/health`（搭车返回，不多一轮轮询）。
        它回答的是"**现在需要我做什么吗**" —— 例如凭据在采集中失效时，
        用户不该只看到"重连中"而要去翻日志。
        """
        payload = panel.health()
        payload["version"] = __version__
        payload["uptime_s"] = round(time.time() - app.state.started_at, 1)
        payload["frontend_built"] = dist is not None
        payload["attention"] = assess_health(panel.rooms())
        return payload

    @app.get("/api/rooms")
    async def list_rooms() -> dict[str, Any]:
        return {"rooms": panel.rooms()}

    @app.get("/api/room-info")
    async def room_info(room: str = Query(..., min_length=1, description="房间号（支持短号）")) -> dict[str, Any]:
        """房间号 → 面板展示用的房间/主播信息。

        右侧栏「XXX 的直播间」在用户**输入房间号时**就要显示出来，
        所以这里刻意与「添加监控」解耦：查信息不启动采集。
        已查过的房间号由 `PanelService` 在进程内缓存，重复查询不再打网络接口。
        """
        try:
            payload = await panel.lookup_room(room)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TimeoutError as exc:
            # 504 而不是 400：房间号本身没问题，是这次请求没等到答案。
            # 前端据此**立刻**解除 `busy` 并提示"网络慢，请重试"。
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 房间不存在/接口失败统一 400
            raise HTTPException(status_code=400, detail=f"查询房间失败: {exc}") from exc
        return payload

    @app.post("/api/rooms", status_code=201)
    async def add_room(body: AddRoomBody) -> dict[str, Any]:
        # 先把"输入本身能不能解析"判掉 → 400。
        # 必须与下面的 `ValueError → 409` 分开：`panel.add_room` 用 `ValueError`
        # 表示"已在监控中 / 已达上限"（冲突），而**解析失败是参数错误**。
        # 两者混在一起会让"粘了一个坏链接"报成 409「已在监控中」，完全误导。
        try:
            parsed = parse_room_input(body.room)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            runtime = await panel.add_room(parsed)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=f"添加房间失败: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 房间不存在/接口失败统一 400
            raise HTTPException(status_code=400, detail=f"添加房间失败: {exc}") from exc
        self_snapshot = runtime.snapshot()
        hub.publish_control("rooms", {"rooms": panel.rooms()})
        return {"room": self_snapshot}

    @app.post("/api/rooms/{room_id}/reconnect", status_code=200)
    async def reconnect_room(room_id: int) -> dict[str, Any]:
        """**手动重连**：让已放弃自动重连的房间再试一次（需求 2026-10-09）。

        为什么要有这个端点：自动重连连续失败 5 次后采集器会**刻意停下**
        （无限重连只会对已经出问题的服务端一直打请求，而用户只看到一个永远
        转不完的"重连中"）。停下的那一刻必须有人能推一把 —— 网络恢复了、
        或者用户刚重新扫码登录过，只有他知道。

        响应里 `accepted` 表示"是否真的排了一次重连"：当前连接正常时会返回
        `accepted: false` + 原因，**不是错误** —— 那其实是好消息。
        404 只用于"房间不在监控中"；采集任务已结束时 409，因为那种情况
        该重新添加房间，而不是重连。
        """
        try:
            payload = panel.reconnect_room(room_id)
        except ValueError as exc:
            detail = str(exc)
            code = 404 if "不在监控中" in detail else 409
            raise HTTPException(status_code=code, detail=detail) from exc
        hub.publish_control("rooms", {"rooms": panel.rooms()})
        return payload

    @app.delete("/api/rooms/{room_id}", status_code=200)
    async def delete_room(room_id: int) -> dict[str, Any]:
        removed = await panel.remove_room(room_id)
        if not removed:
            raise HTTPException(status_code=404, detail=f"房间 {room_id} 不在监控中")
        hub.publish_control("rooms", {"rooms": panel.rooms()})
        return {"removed": room_id}

    @app.get("/api/events")
    async def list_events(
        room_id: int | None = None,
        type: str | None = None,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = Query(100, ge=1, le=1000),
        #: 分页偏移。**必须有上限**（P0-1，2026-10-10）：
        #: 之前这里既没有上限、又把 `limit + offset` 整段取出来再切片，
        #: 于是 `offset=200000` 会一次物化 20 万个 `Event`（每个还带原始帧），
        #: 是一个可以直接打爆内存的口子。现在 offset 下推到 SQL 的
        #: `LIMIT ? OFFSET ?`（见 `DanmakuStore.iter_events`），
        #: 并且这里再夹一个上限，避免"深度翻页"退化成全表扫描。
        offset: int = Query(0, ge=0, le=100_000),
    ) -> dict[str, Any]:
        types = [item.strip() for item in type.split(",") if item.strip()] if type else None

        def _read(opened: DanmakuStore) -> tuple[int, list[Event]]:
            total = opened.count(room_id=room_id, types=types, keyword=q, since=since, until=until)
            # 分页下推到 SQL：只构造 limit 行（不再取 limit+offset 再切片）
            events = list(
                opened.iter_events(
                    room_id=room_id,
                    types=types,
                    keyword=q,
                    since=since,
                    until=until,
                    limit=limit,
                    offset=offset,
                )
            )
            return total, events

        # ⚠️ 必须先判断"有没有库"，再决定走哪条路：`_run_with_store` 会把读放到
        # 工作线程里，而 `_memory_events` 只读内存环形缓冲。没有库时若还去开库，
        # 冷库上的读取会**堵住事件循环**（实测 `/api/health` 被拖到 2.4 秒）。
        if _open_store(config) is None:
            return _memory_events(hub, room_id, types, q, limit)

        total, events = await _run_with_store(config, _read)

        return {
            "total": total,
            "items": [event.to_dict() for event in events],
            "source": "sqlite",
        }

    # ------------------------------------------------------------------ #
    # 历史浏览（V3-a）：游标分页 / 时间轴密度 / 会话列表
    #
    # 三个端点都**只读**、都不启动采集。它们的存在理由：
    # 数据一直是全的，缺的是"能翻页、能定位"的通路。
    #
    # 与 `/api/events` 的分工（故意不合并）：
    #   * `/api/events` 面向脚本/小结果集：完整 Event、支持 offset；
    #   * `/api/history` 面向面板翻页：**瘦投影** + `(recv_ts, id)` 复合游标。
    # 不要把 `/api/history` 的投影逻辑塞回 `/api/events` —— 那会悄悄改窄既有契约。
    # ------------------------------------------------------------------ #

    def _history_filters(
        room_id: int | None,
        session_id: str | None,
        type: str | None,
        q: str | None,
        since: float | None = None,
        until: float | None = None,
    ) -> dict[str, Any]:
        """组装 `/api/history` 的过滤条件。

        `since`/`until` 是**必须支持**的（不是"顺手加的"）：时间轴跳转要靠
        `until = 所点格子的结束时刻` 把窗口右端**锚进那一格**。以前端点没接这两个
        参数，前端传了也被 FastAPI **静默忽略**（不是 400），于是窗口右端落在别处、
        整页偏到上一格 —— 就是"点了历史时段却没跳过去"的根因之一。
        """
        filters: dict[str, Any] = {}
        if room_id is not None:
            filters["room_id"] = room_id
        if session_id:
            filters["session_id"] = session_id
        if type:
            filters["types"] = [item.strip() for item in type.split(",") if item.strip()]
        if q:
            filters["keyword"] = q
        if since is not None:
            filters["since"] = float(since)
        if until is not None:
            filters["until"] = float(until)
        return filters

    def _anchor_ts(store: Any, filters: dict[str, Any]) -> tuple[float | None, float | None]:
        """该过滤范围内的数据时间边界，作为"全部房间"兜底时间窗的锚点。

        用一个只有 `room_id` 的最小过滤集去算边界：`session_id`/关键词会改变范围，
        而锚点要的是"这个库/房间最新到哪"，不该被当前关键词缩小。
        """
        scope = {key: filters[key] for key in ("room_id",) if key in filters}
        return store.time_range(**scope)

    @app.get("/api/history")
    async def history(
        room_id: int | None = None,
        session_id: str | None = None,
        type: str | None = None,
        q: str | None = None,
        before_ts: float | None = None,
        before_id: int | None = None,
        after_ts: float | None = None,
        after_id: int | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = Query(
            store_module.DEFAULT_HISTORY_LIMIT, ge=1, le=store_module.MAX_HISTORY_LIMIT
        ),
        window_s: float | None = Query(
            None,
            ge=1,
            le=86400,
            description="无 room_id 时的兜底时间窗（秒）；缺省 1 小时",
        ),
    ) -> dict[str, Any]:
        """游标分页取一页历史事件（升序）。

        游标是 **`(recv_ts, id)` 复合键**：`recv_ts` 会并列（同一批推送同刻），
        只传 `before_ts` 会在页边界漏条，所以 `before_id`/`after_id` 必须一起传。

        `since`/`until` 是**时间轴跳转的锚点**：点某一格时前端传
        `until = 该格结束时刻`，窗口右端才会落在**所点的这一格**里。
        """
        before = (before_ts, before_id) if before_ts is not None and before_id is not None else None
        after = (after_ts, after_id) if after_ts is not None and after_id is not None else None
        if before is not None and after is not None:
            raise HTTPException(status_code=400, detail="before_* 与 after_* 不能同时使用")

        filters = _history_filters(room_id, session_id, type, q, since, until)
        pane = HISTORY_PAGE_WINDOW_S if window_s is None else float(window_s)

        # DB 工作整体在工作线程里跑：慢查询不该冻住整个面板。见 `_run_with_store`。
        def work(opened: DanmakuStore) -> dict[str, Any]:
            lo, hi = _anchor_ts(opened, filters)
            try:
                items, has_older, has_newer = opened.iter_history(
                    limit=limit,
                    before=before,
                    after=after,
                    window_s=pane,
                    anchor_ts=hi,
                    **filters,
                )
            except ValueError as exc:  # 游标参数自相矛盾
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"items": items, "has_older": has_older, "has_newer": has_newer, "lo": lo, "hi": hi}

        result = await _run_with_store(config, work)
        items = result["items"]
        oldest = (items[0]["recv_ts"], items[0]["id"]) if items else None
        newest = (items[-1]["recv_ts"], items[-1]["id"]) if items else None
        # `window_s` 报告**实际生效**的兜底窗：有 `room_id`、或显式给了 `since`/`until`
        # 时它根本不参与过滤（见 `store._window_clause`），这里必须如实回 0 ——
        # 回一个没生效的 3600 会让调用方以为那段时间被切过。
        window_used = room_id is None and since is None and until is None
        return {
            "items": items,
            "has_older": result["has_older"],
            "has_newer": result["has_newer"],
            "oldest": oldest,
            "newest": newest,
            "source": "sqlite",
            "room_id": room_id,
            "session_id": session_id,
            "window_s": pane if window_used else 0.0,
            "range": {"lo": result["lo"], "hi": result["hi"]},
        }

    @app.get("/api/density")
    async def density(
        request: Request,
        room_id: int | None = None,
        session_id: str | None = None,
        type: str | None = None,
        since: float | None = None,
        until: float | None = None,
        bucket_s: float | None = Query(
            None, ge=1, le=86400, description="桶宽（秒）；缺省按数据跨度自适应"
        ),
        max_slots: int | None = Query(
            None,
            ge=100,
            le=20000,
            description=(
                "**渲染预算**：这一屏最多能画多少格（客户端按视口宽给，缺省 2000）。"
                "手选档位超过预算时会被**自动夹粗**，响应里带 `bucket_s_clamped`。"
            ),
        ),
    ) -> dict[str, Any]:
        """时间轴密度：按桶聚合的条数分布（时间轴/密度条的数据源）。

        聚合在 SQL 里完成（实测 ~1 ms / 104 桶）。**不要**退回"把事件搬到前端再算" ——
        那正是旧的 `iter_events(limit=20100)` 路径在干的事（约 0.9 s）。

        ## 这条端点加了"三道闸"

        需求方现场：采集久了以后点粒度会卡滞，**连续点几下**就报 30 s 超时。
        实测根因不是单发慢（87 ms），而是**同时飞太多条**：
        单发里 54 ms 是持 GIL 的 Python 折算 ⇒ 并发超线性退化
        （1→67ms、2→239ms、4→912ms、8→2.8s、16→6.0s，≈ n^1.7），
        而前端连点不同档位时去重键各不相同 ⇒ 真的会并发发一堆。

        1. **短 TTL 缓存**（`_DENSITY_CACHE_TTL_S`）：同一组参数（定时刷新、反复点同一档）
           直接命中，不再扫库；
        2. **单飞（single-flight）**：同一组参数的并发请求**共享同一次查询**；
        3. **并发上限**（`_DENSITY_MAX_PARALLEL`）：不同的档位/房间最多同时算 2 个，
           其余的排队 —— 把"互相拖到 30 s"变成"依次很快算完"。

        另外：客户端断开时（`AbortController` 超时/切档）通过
        `_watch_disconnect()` 置位 `cancel_event`，让工作线程里的折算**真的收手** ——
        否则"超时后用户再点"会不断往后台堆查询。
        """
        filters: dict[str, Any] = {}
        if room_id is not None:
            filters["room_id"] = room_id
        if session_id:
            filters["session_id"] = session_id
        types = [item.strip() for item in type.split(",") if item.strip()] if type else None

        def work(opened: DanmakuStore, cancel_event: Any) -> dict[str, Any]:
            # ⚠️ 自适应步长必须按**实际会出数的范围**算 —— `since`/`until` 必须一起传。
            #
            # 曾经的写法是 `time_range(**filters)`（不含 since/until）：步长按**整库跨度**
            # 挑，而桶只出 `since` 之后的那一小段。实测（房间 721 = 3044248，库里有
            # 9/30 起的历史、而本次采集才几分钟）：
            #
            # | 量 | 实测值 |
            # |----|--------|
            # | 轴跨度 | **8.85 天** |
            # | 自适应步长 | **7200 秒（2 小时）** |
            # | 本次采集落进的桶 | **1 个** |
            #
            # 于是前端拿到的"密度"只有一个点 —— 需求方看到的"曲线完全不对"。
            lo, hi = opened.time_range(**{**filters, "since": since, "until": until})
            step = bucket_s if bucket_s is not None else opened.estimate_bucket_s(lo, hi)
            # **手选的档位也要过渲染预算**。
            # 预算由客户端给（它才知道视口多宽），缺省 2000 ——
            # 实测 1 分钟档在 24 小时跨度下是 0.83 px/格（糊），
            # 到 7.8 天（11,259 格）前端会放弃补零 ⇒ 曲线变"斜坡"。
            clamped_step = _clamp_bucket_s(step, lo, hi, max_slots)
            payload = opened.density(
                bucket_s=clamped_step,
                since=since,
                until=until,
                include_types=types,
                cancel_event=cancel_event,
                **filters,
            )
            if clamped_step != step:
                # 只在实际夹过档时才加这个键：正常响应的形状与前**逐字一致**
                payload["bucket_s_clamped"] = True
            return payload

        return await _density_response(
            config,
            request,
            key=_density_cache_key(room_id, session_id, types, since, until, bucket_s, max_slots),
            work=work,
        )

    @app.get("/api/sessions")
    async def sessions(
        room_id: int | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        """抓取会话列表（含起止时间与事件数），供"按某次抓取浏览历史"用。"""
        items = await _run_with_store(config, lambda opened: opened.sessions(limit=limit, room_id=room_id))
        return {"items": items, "source": "sqlite"}

    @app.get("/api/stats/export")
    async def stats_export(
        room_id: int | None = None,
        session_id: str | None = None,
        type: str | None = None,
        since: float | None = None,
        until: float | None = None,
        bucket_s: float | None = Query(
            None, ge=1, le=86400,
            description="每时段弹幕量的格宽（秒）；缺省 5 分钟（需求约定的默认档）",
        ),
        format: str = Query(
            "csv",
            pattern="^(csv|xlsx)$",
            description="导出格式；`csv` 默认，`xlsx` 需装了 openpyxl",
        ),
    ) -> dict[str, Any]:
        """导出**统计数据**：弹幕统计 + 每时段弹幕量，**同一个文件多张表**。

        与 `/api/export` 的区别（两者都保留，用途不同）：

        | 端点 | 内容 | 形状 |
        |------|------|------|
        | `/api/export` | **原始事件**逐条 | 流式下载，大数据量 |
        | `/api/stats/export` | **聚合统计**（概要/类型/时段/发言 TOP） | JSON，前端拼文件名后另存 |

        为什么这个不是流式下载：需求要求文件名是
        `<主播名字>_<房间号>_<日期>_<当天导出编号>.csv`，而"当天导出编号"只有
        **前端**能维护；服务端 `Content-Disposition` 的名字在流式下载下也不受控。
        所以这里只回内容，**文件名交给前端**（见 `web/src/preferences.js`
        的 `buildExportFilename`）。

        内容里保持**不带 BOM**，由前端加 —— 这样这个端点在调试时（curl / 测试）
        读到的就是干净的 UTF-8 文本。

        `format=xlsx` 时多回一个 `xlsx` 字段（**base64**，因为 JSON 塞不下二进制），
        同时**仍然回 `csv`**：这样前端下载失败或用户其实想要文本时不用再请求一次，
        而"openpyxl 没装"也只影响 `xlsx` 字段（`None`），不会让整个导出 409。
        """
        from ..export_stats import DEFAULT_BUCKET_S, build_stats_csv, build_stats_xlsx

        types = [item.strip() for item in type.split(",") if item.strip()] if type else None

        def _collect(opened: DanmakuStore) -> dict[str, Any]:
            # 过滤条件统一在这里组装；`types` 用 store 的关键字名，`density`/`top_senders`
            # /`time_range` 都认同一套（`_build_where`），所以只写一次。
            #
            # 注意 `time_range` **不接** `types`：它只算时间边界，类型不影响 min/max，
            # 传进去只会让 `_build_where` 多一个无用的 `type IN (...)`。
            filters: dict[str, Any] = {}
            if room_id is not None:
                filters["room_id"] = room_id
            if session_id:
                filters["session_id"] = session_id
            if types:
                filters["types"] = types

            lo, hi = opened.time_range(**{k: v for k, v in filters.items() if k != "types"})
            start = since if since is not None else lo
            end = until if until is not None else hi

            step = float(bucket_s) if bucket_s else DEFAULT_BUCKET_S
            # P0-4：**夹格宽**，与 `/api/density` 同一套渲染预算。
            #
            # 此前这里没有夹档：默认「全库跨度 + 用户选的格宽」。面板会传用户选的
            # 档位（≥60 秒），但 API 允许 `bucket_s=1`，而跨度可以是整库的几个月
            # ⇒ `density()` 要产出几十万个桶，每个桶一个 dict，再被 `_density_payload`
            # 复制进列表、被 `build_stats_tables` 复制成行矩阵（**两次**），
            # 最终 body 还要整体 `json.dumps` 一遍。单次请求的峰值因此可以上百 MB。
            #
            # 预算与 density 一致（`_DENSITY_DEFAULT_MAX_SLOTS`）：2000 格。
            clamped_step = _clamp_bucket_s(step, start, end, _STATS_EXPORT_MAX_SLOTS)
            density = opened.density(bucket_s=clamped_step, since=start, until=end, **filters)
            senders = opened.top_senders(limit=10, since=start, until=end, **filters)

            # 选了具体房间就用它；否则从桶里挑**事件最多**的那个房间当"这份报告的主房间"
            # （文件名需要一个房间号，而为空会让文件名退化得没有信息量）。
            effective_room = room_id
            if effective_room is None:
                effective_room = _busiest_room(
                    opened, since=start, until=end, types=types, session_id=session_id
                )

            # 这份 dict 是给 **CSV / xlsx 生成器**用的**内部结构**，不是响应体。
            export_input: dict[str, Any] = {
                "exported_at": time.time(),
                "since": start,
                "until": end,
                "bucket_s": clamped_step,
                "room_id": effective_room,
                "anchor_name": panel.anchor_name(effective_room),
                "total": sum(int(v or 0) for v in (density.get("by_type") or {}).values()),
                "by_type": density.get("by_type") or {},
                "buckets": density.get("buckets") or [],
                "top_senders": senders,
            }

            # P0-4：**响应体只带前端真正要的东西**。
            #
            # `web/src/App.vue` 的导出流程只读四个字段：
            # `payload.xlsx` / `payload.csv` / `payload.anchor_name` / `payload.room_id`。
            # 而旧实现是 `{"csv": …, **payload}` —— 把 `buckets`（最多 2000 个桶）
            # 和 `top_senders` 又**原样塞回 JSON**，等于把同样的数据在内存里多留一份
            # 并多序列化一次（每个桶还要转义成 JSON）。那些字段前端一次都没读过。
            #
            # 仍然保留 `bucket_s` / `since` / `until` / `total` / `by_type`：
            # 它们极小，而且是调试/脚本（curl）读这个端点时最有用的元信息。
            result: dict[str, Any] = {
                "format": format,
                "bucket_s": clamped_step,
                "since": start,
                "until": end,
                "room_id": effective_room,
                "anchor_name": export_input["anchor_name"],
                "total": export_input["total"],
                "by_type": export_input["by_type"],
                "csv": build_stats_csv(export_input),
            }
            if clamped_step != step:
                # 与 `/api/density` 同一约定：**只在实际夹过档时才加这个键**，
                # 正常响应的形状与以前逐字一致（老调用方不受影响）。
                result["bucket_s_clamped"] = True
            if format == "xlsx":
                book = build_stats_xlsx(export_input)
                # `None` = 这台机器没装 openpyxl。不报错，让前端退回 CSV。
                result["xlsx"] = base64.b64encode(book).decode("ascii") if book else None
            return result

        # 整段（聚合 + CSV + xlsx）都在**工作线程**里跑：它是全项目最重的同步计算，
        # 放在事件循环上会让面板连 `/api/health` 都不回（通用护栏）。
        return await _run_with_store(config, _collect)

    # ------------------------------------------------------------------ #
    # 标注（V3-a2）：标记 / 笔记 / 按标记区间导出
    #
    # 存**独立库**（`{out_dir}/annotations.db`，可配 `sinks.annotations_path`），
    # 一张表都不往 `danmaku.db` 里加 —— 理由见 `annotations.py` 的模块 docstring。
    #
    # 与历史浏览端点的区别：这三个是**写**接口（以及一个读）。
    # 它们**不需要** sqlite sink：标注只依赖时间戳，采集没开 sqlite 也能用。
    # ------------------------------------------------------------------ #

    def _open_annotations(*, readonly: bool = False) -> AnnotationStore:
        """打开标注库。只读模式下库不存在时返回一个 `available=False` 的实例。

        刻意**不**在只读路径上 409：面板首屏就会拉一次标注列表，
        此时用户可能一条都还没打过 —— 那不是错误，应该正常返回空列表。
        """
        return AnnotationStore(resolve_annotations_path(config), readonly=readonly)

    @app.get("/api/room-names")
    async def room_names(
        rooms: str = Query(
            "",
            description="逗号分隔的房间号；返回 `{room_id: 昵称}`",
        ),
        live: bool = Query(
            False,
            description=(
                "是否顺带向 B 站查**权威主播昵称**（每个房间一次请求）。"
                "缺省 false = 只读采集库，秒回但可能与真实主播名不一致"
            ),
        ),
    ) -> dict[str, Any]:
        """按房间号批量取"昵称"，供右侧栏的**房间号下拉列表**显示（需求 #8）。

        ## 两级来源（这里踩过一致性坑，改动前先读）

        需求方实测反馈"房间号和主播昵称**未匹配**"。核实：确实不匹配 ——
        取"该房间留言最多的 `uname`"这个**启发式**在热门房间上会选到**观众**
        （实测 4/4 全错：8432038 推出 `Neuf-Sept`、真主播是 `血狼破军`；
        804456 推出 `南下扶苏`、真主播是 `六式zy`）。
        原因是热门房间里观众刷屏比主播勤，这个假设根本不成立。

        所以改成两级：

        1. `live=true` → 走 `BiliApi.anchor_info()` 拿**权威昵称**（`uname` 字段，
           与右侧栏「XXX 的直播间」**同一个接口**，所以两处显示的必然一致）；
        2. 没查到的（风控/房间不存在/`live=false`）→ 回退到**库推导值**，
           并在 `guessed` 里列出来，前端据此**只标"曾采集"而不显示成主播名**。

        `live=false`（缺省）只读库、**不联网** —— 面板首屏要快，不该为了几个昵称
        阻塞或触发风控；前端拿到结果后再补一次 `live=true` 的请求（见
        `App.fillRoomHistoryNames`）。
        """
        ids: list[int] = []
        for chunk in (rooms or "").split(","):
            text = chunk.strip()
            if text.isdigit():
                ids.append(int(text))
        if not ids:
            return {"items": {}, "guessed": [], "source": "sqlite"}

        # ⚠️ 刻意用 `config.sinks.types` 判断，而不是"库文件在不在"：
        # 上一次运行留下的 `danmaku.db` 会让 ls 看起来"有库"，但配置里没启用 sqlite
        # 时读它是**误导**（那正是历史端点统一 409 的理由）。
        # 这里不 409 是因为下拉列表少个昵称无所谓，但也不能拿旧库的数据冒充。
        items: dict[str, str] = {}
        guessed: list[str] = []
        if "sqlite" in (config.sinks.types or []):
            # 只读库这一步也放工作线程：实测在 1 GB 真库上「开库 +
            # `room_anchor_names` 聚合」要 **322 ms**（`probe_read_order.py`）——
            # 单看不致命，但它是**首屏**路径，没有理由让它堵住事件循环。
            # 库不可用（未启用/打不开）时 `_run_with_store` 抛 409，这里**吞掉**：
            # 下拉列表少个昵称而已，不该让整个端点变红（与历史端点的口径**故意不同**）。
            try:
                names = await _run_with_store(config, lambda opened: opened.room_anchor_names(ids))
            except HTTPException:
                names = {}
            for room, name in (names or {}).items():
                items[str(room)] = name
                guessed.append(str(room))

        source = "sqlite" if items else "none"

        if live and ids:
            from ..api import BiliApi
            from ..credentials import load_credentials

            creds = load_credentials(getattr(getattr(config, "api", None), "cookie_file", None))
            # `live=true` 的**整体截止时间**：房间数最多 12 个，原来是**串行**逐个查，
            # 每个房间 `anchor_info` 超时 10 s ⇒ 最坏约 145 s。面板首屏会带
            # `live=true` 补一次昵称，那就是一段长达两分钟的"卡住"
            # （实测黑洞代理下 2 个房间要 24.4 s）。
            # 改成并发 + 总超时：**超时就放弃权威值、保留库推导名**，
            # 下拉列表最多短暂显示"曾采集"，但绝不会让页面停住。
            deadline = _resolve_timeout(config)
            results: list[Any] = []
            finished = False
            try:
                async with BiliApi(config, creds) as api:

                    async def _one(room: int) -> tuple[int, str]:
                        info = await api.anchor_info(room)
                        return room, str((info or {}).get("uname") or "").strip()

                    # 并发上限：同时打 12 个请求对风控不友好，4 路足够把
                    # "串行 12 × 10 s" 压到 "并发 3 批 × 10 s"。
                    gate = asyncio.Semaphore(4)

                    async def _gated(room: int) -> tuple[int, str]:
                        async with gate:
                            return await _one(room)

                    try:
                        results = list(
                            await asyncio.wait_for(
                                asyncio.gather(*(_gated(room) for room in ids), return_exceptions=True),
                                timeout=deadline,
                            )
                        )
                        finished = True
                    except TimeoutError:
                        log.warning(
                            "批量查主播昵称超时（%d 个房间，上限 %.0fs）：保留库推导名",
                            len(ids),
                            deadline,
                        )
            except Exception as exc:  # noqa: BLE001 - 联网失败不该让下拉列表变红
                log.debug("主播昵称批量查询失败：%s", exc)

            for item in results:
                if isinstance(item, BaseException):
                    log.debug("查主播昵称失败：%s", item)
                    continue
                room, name = item
                if not name:
                    continue
                items[str(room)] = name
                # 权威值来了 ⇒ 它不再是"猜的"
                if str(room) in guessed:
                    guessed.remove(str(room))

            # 只有"整批都问完了"才算 live 源：超时退出时剩下的仍是库推导名，
            # 这时候报 `live` 会让前端误以为全都是权威值。
            if finished:
                source = "live"

        return {"items": items, "guessed": guessed, "source": source}

    @app.get("/api/annotations")
    async def list_annotations(
        room_id: int | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = Query(500, ge=1, le=2000),
    ) -> dict[str, Any]:
        """列出标记（按时间升序）。库不存在时返回空列表而不是错误。"""
        opened = _open_annotations(readonly=True)
        try:
            items = opened.list(room_id=room_id, since=since, until=until, limit=limit)
        finally:
            opened.close()
        return {"items": items, "summary": summarize(items), "source": "annotations"}

    @app.post("/api/annotations", status_code=201)
    async def create_annotation(body: BookmarkBody) -> dict[str, Any]:
        """新增标记。

        `anchor_id` 只是溯源信息：**不校验它是否真实存在** ——
        校验会引入对采集库的依赖，而标注本身只靠 `recv_ts` 就能用
        （被钉的弹幕清理掉之后标注依然有效，这是刻意的）。
        """
        opened = _open_annotations()
        try:
            try:
                item = opened.add(
                    room_id=body.room_id,
                    recv_ts=body.recv_ts,
                    label=body.label,
                    note=body.note,
                    session_id=body.session_id,
                    anchor_id=body.anchor_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            opened.close()
        return {"item": item}

    @app.delete("/api/annotations/{bookmark_id}")
    async def delete_annotation(bookmark_id: int) -> dict[str, Any]:
        opened = _open_annotations()
        try:
            if not opened.delete(bookmark_id):
                raise HTTPException(status_code=404, detail=f"标记 {bookmark_id} 不存在")
        finally:
            opened.close()
        return {"removed": bookmark_id}

    @app.get("/api/annotations/export")
    async def export_annotation_range(
        format: str = Query("csv", pattern="^(csv|jsonl|txt)$"),
        bookmark_id: int | None = Query(None, description="起点标记"),
        to_bookmark_id: int | None = Query(None, description="终点标记（缺省 = 到区间末尾）"),
        room_id: int | None = None,
        type: str | None = None,
        q: str | None = None,
        limit: int | None = Query(None, ge=1, le=500_000),
    ) -> Any:
        """按标记区间导出（V3-a2 的"标记 → 切片"）。

        区间 = `[起点标记的时间, 终点标记的时间]`：
          * 只给 `bookmark_id` 时，终点自动取**下一个标记**（没有则到该房间数据的末尾）——
            这是"把这个标记到下一个标记之间导出来"的直觉语义；
          * 同时给 `to_bookmark_id` 时按两者排序后的区间走（给反了会自动正过来）。
        """
        if bookmark_id is None:
            raise HTTPException(status_code=400, detail="必须提供 bookmark_id 作为区间起点")

        opened = _open_annotations(readonly=True)
        if not opened.available:
            raise HTTPException(status_code=404, detail="还没有任何标记，无法按区间导出")
        try:
            try:
                start = opened.get(bookmark_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"标记 {bookmark_id} 不存在") from exc
            scope_room = room_id if room_id is not None else int(start["room_id"])
            since = float(start["recv_ts"])

            if to_bookmark_id is not None:
                try:
                    end = opened.get(to_bookmark_id)
                except KeyError as exc:
                    raise HTTPException(
                        status_code=404, detail=f"标记 {to_bookmark_id} 不存在"
                    ) from exc
                until = float(end["recv_ts"])
                if until < since:  # 给反了就正过来，而不是导出空结果
                    since, until = until, since
            else:
                following = opened.list(room_id=scope_room, since=since, limit=2)
                # `since` 取闭区间，所以第一条就是起点自己，要往后取一条才是"下一个标记"
                later = [item for item in following if float(item["recv_ts"]) > since]
                until = float(later[0]["recv_ts"]) if later else None
        finally:
            opened.close()

        # 先把"能不能导"判掉（409/错误码必须在开始流式输出**之前**发出）。
        # 这里只做**可用性探针**（开库/关库，毫秒级）：真正的 `count()` 交给
        # 工作线程 —— 带 `q` 的 COUNT 在大库上是全表 LIKE 扫描，放事件循环上
        # 会让整个面板（含 WS 推送）停摆（通用护栏）。
        _open_store_or_409(config).close()
        types = [item.strip() for item in type.split(",") if item.strip()] if type else None
        total = await _run_with_store(
            config,
            lambda opened: opened.count(
                room_id=scope_room, types=types, keyword=q, since=since, until=until
            ),
        )

        return _stream_export(
            format,
            lambda: _iter_range_events(
                config,
                room_id=scope_room,
                types=types,
                keyword=q,
                since=since,
                until=until,
                limit=limit,
            ),
            total=total,
            range_info={"since": since, "until": until, "room_id": scope_room},
        )

    # ------------------------------------------------------------------ #
    # 感知分析（V3-b 3A）：本地统计，**数据不出本机**
    #
    # 数据源是 `hub` 的有界窗口（`LiveWindow`），不是数据库 —— 它回答的是
    # "**现在**大家在聊什么"，与 `/api/stats` 的累计口径、`/api/density` 的历史分布
    # 是三件不同的事。窗口大小与内存上限见 `hub.py` 顶部的常量。
    #
    # 这个端点**不做任何外发**：LLM 那类"把弹幕发出去"的能力属于可选扩展，
    # 必须由用户自备 key 且默认关闭 —— 不要顺手加在这里。
    # ------------------------------------------------------------------ #

    def _analysis_stopwords() -> tuple[bool, set[str]]:
        """读分析用的停用词：`(是否有自定义, 词集合)`。

        配置没给文件时返回 `(False, set())` —— 此时**不要**去动 hub 已经算好的词表，
        因为 hub 的增量分词本来就用了内置停用词，重复过滤没有意义还会有开销。
        """
        path = (config.analysis.stopwords_file or "").strip()
        if not path:
            return False, set()
        try:
            return True, load_stopwords(path)
        except (OSError, FileNotFoundError) as exc:
            # 配置写错不该让 /api/analysis 整个失败：退回内置表并记一条日志
            log.warning("停用词文件读不了，本次忽略：%s（%s）", path, exc)
            return False, set()

    def _analysis_custom_words(room_id: int | None) -> list[str]:
        """分析用的用户词典 = 配置里的词 + **当前房间的主播昵称**。

        主播昵称是这里最有价值的一条：弹幕里大量出现主播名，而 jieba 的通用词典
        不认识它，会把 `军哥` 切成 `军`+`哥` —— 榜单上就出现半截词
        （需求方反馈的"不符合日常表达"有一大半是这类）。

        取名字用 `getattr` 兜底：这个端点在测试里会配一个最小 panel 替身，
        而"主播名的用户词典"纯粹是锦上添花，**不该**因为它缺失就让整个分析 500。
        """
        words = [str(item).strip() for item in (config.analysis.custom_words or [])]
        lookup = getattr(panel, "anchor_name", None)
        if callable(lookup):
            for target in _analysis_rooms(room_id):
                try:
                    name = lookup(target)
                except Exception:  # noqa: BLE001 - 取名字失败不影响分析
                    continue
                if name:
                    words.append(name)
        return [word for word in words if word]

    def _analysis_rooms(room_id: int | None) -> list[int | None]:
        """`/api/analysis` 要覆盖的房间：指定了就只用它，否则用全部监控中的房间。"""
        if room_id is not None:
            return [room_id]
        return [runtime.get("room_id") for runtime in panel.rooms()]

    @app.get("/api/analysis")
    async def analysis(
        room_id: int | None = None,
        top: int = Query(20, ge=5, le=100, description="词频榜长度"),
        tokenizer: str = Query("auto", pattern="^(auto|jieba|fallback)$"),
    ) -> dict[str, Any]:
        """当前窗口的本地感知分析（词频 / 热点突增 / 疑似刷屏 / 疑似异常账号）。

        词频做了三件事让榜单"更像人话"（需求方反馈过"统计的词有一部分不符合
        日常表达"）：① 提交**用户词典**（配置 + 当前房间主播名）防止专名被切碎；
        ② 用配置里的**停用词**替换内置表；③ 按**说过的人数**排序，
        把"一个人刷 20 遍"压下去（`rank_by` 可切回按次数）。
        """
        messages, buckets = hub.window_snapshot(room_id)
        custom_words = _analysis_custom_words(room_id)
        apply_stopwords, stopwords = _analysis_stopwords()

        # 用户词典要在**分词之前**就位（hub 的增量分词也用它）。
        # 注意 hub 是逐条增量分词的，所以这里补提交只对**之后**进入窗口的弹幕生效 ——
        # 这也是为什么配置项建议在面板启动时就写好。
        if custom_words:
            add_custom_words(custom_words)

        words = words_from_buckets(
            buckets, top=top, by=config.analysis.rank_by
        )
        if apply_stopwords and stopwords:
            # hub 的增量词表是按**内置**停用词过滤的，所以这里只做"额外剔除"：
            # 把用户停用词里命中的词从榜上删掉。想连计数口径一起改（例如
            # 保留内置表没有的词）需要重跑 `word_frequency(messages, ...)`。
            words = _drop_stopwords(words, stopwords)
            words["stopword_filter"] = len(stopwords)

        payload = analyze(
            messages,
            buckets=buckets,
            tokenizer=tokenizer,
            top_words=top,
            words=words,
        )
        payload["room_id"] = room_id
        payload["bucket_s"] = ANALYSIS_BUCKET_S
        payload["window_cap"] = ANALYSIS_WINDOW_MESSAGES
        payload["source"] = "memory"
        payload["custom_words"] = len(custom_words or ())
        return payload

    @app.get("/api/stats")
    async def stats(
        room_id: int | None = None,
        window: int = Query(
            0,
            ge=0,
            le=3600,
            description="统计窗口（秒）；**0 = 自面板启动以来累计**（默认口径）",
        ),
    ) -> dict[str, Any]:
        """面板统计。

        `window=0`（默认）走**累计口径**：数据来自 `hub` 的内存计数，
        统计范围就是"这个面板从启动到现在"，不含历史补种与往次运行的库内数据 ——
        这正是"默认从启动时一直统计"的语义。
        传具体秒数则回落到 SQLite 的滑动窗口统计（可看最近时段）。
        """
        cumulative = window <= 0
        since = time.time() - window if not cumulative else hub.started_at
        payload: dict[str, Any] = {
            "rooms": panel.rooms(),
            "hub": hub.stats,
            "window_s": 0 if cumulative else window,
            "mode": "cumulative" if cumulative else "window",
            "cumulative": hub.cumulative,
            "by_type": hub.by_room_type(room_id),
            "msgs_total": (
                hub.cumulative_by_room.get(room_id, 0) if room_id is not None else hub.cumulative_total
            ),
            "source": "memory",
        }
        if cumulative:
            # 累计口径下"速率"要按 hub 的运行时长算，不能除以 0
            elapsed = max(time.time() - hub.started_at, 1.0)
            payload["msgs_per_min"] = round(payload["msgs_total"] * 60.0 / elapsed, 1)
            payload["window_started_at"] = hub.started_at
            # 发言 TOP：**"全部房间"这条完全不碰库**（2026-10-09 实测修正，见
            # `hub.cumulative_top_senders`）：不带 room_id 的全库 `GROUP BY uid`
            # 在 1 GB 库上要 2.6~35 秒，而它跑在事件循环上 ⇒ 整个面板卡死。
            #
            # 只有"选中了具体房间"才查库 —— 那条走 `idx_event_room_ts_type`，
            # 实测 0.2~7 ms，可以忽略。
            if room_id is None:
                senders, scope = _cumulative_top_senders(None, None, hub=hub)
                payload["top_senders"] = senders
                payload["top_senders_scope"] = scope
            else:
                await _fill_room_top_senders(
                    payload, config=config, room_id=room_id, since=hub.started_at, scope=None
                )
            return payload

        # 滑动窗口：以 hub 内存计数为主，SQLite 只用来补延迟分位与发言榜
        elapsed = max(window, 1)
        payload["msgs_per_min"] = round(payload["msgs_total"] * 60.0 / elapsed, 1)

        # ⚠️ DB 这一段**整体在工作线程里跑**：
        # 面板里所有 SQLite 调用都是同步的，直接跑在事件循环上时**慢查询会停住
        # 整个面板**（实测一条 2.5 秒的查询把 `/api/health` 从 21 ms 拖到 2412 ms）。

        def _window_stats(store: DanmakuStore) -> dict[str, Any]:
            scope: dict[str, Any] = {"room_id": room_id}
            p50, p95 = store.latency_percentiles(since=since, **scope)
            return {
                "source": "sqlite",
                "db": str(store.path),
                "by_source": store.by_source(since=since, **scope),
                "latency_p50_ms": p50,
                "latency_p95_ms": p95,
                "top_senders": _window_top_senders(store, room_id, since=since),
            }

        try:
            payload.update(await _run_with_store(config, _window_stats))
        except HTTPException:
            # 没有库：滑动窗口下仍能给"最近 N 条"的内存视图（与既有行为一致）
            payload["recent"] = len(hub.recent(room_id, limit=1000))
        return payload

    @app.get("/api/export")
    async def export(
        format: str = Query("csv", pattern="^(csv|jsonl|txt)$"),
        room_id: int | None = None,
        type: str | None = None,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int | None = Query(None, ge=1, le=500_000),
        raw: bool = Query(
            False,
            description=(
                "是否包含**原始协议帧**（`raw`）。缺省 false = 瘦投影导出（快 3~5 倍）；"
                "true 用于取证：解析器出过问题时可以拿原始帧重新解析"
            ),
        ),
    ) -> Any:
        # 可用性检查必须发生在**返回响应之前**：一旦开始流式输出，状态码就已经发出去了，
        # 那时再抛 HTTPException 只会变成一个断掉的 200（这个坑实测踩过）。
        if _open_store(config) is None:
            raise HTTPException(status_code=409, detail="未启用 sqlite sink，无法导出")

        types = [item.strip() for item in type.split(",") if item.strip()] if type else None
        # 文件名：`<主播昵称>_<日期>_<时间>_弹幕导出.csv`（需求 2026-10-09 第七轮）。
        # 昵称从面板缓存/采集库取（与右侧栏「XXX 的直播间」同源）；取不到就退化成
        #「房间弹幕」。**流式下载仍然流式** —— 只换了头部那行文件名。
        return _stream_export(
            format,
            lambda: _iter_range_events(
                config,
                room_id=room_id,
                types=types,
                keyword=q,
                since=since,
                until=until,
                limit=limit,
                slim=not raw,
            ),
            filename=_export_filename(
                (getattr(panel, "anchor_name", None) or (lambda _room: ""))(room_id), format
            ),
        )


    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        params = websocket.query_params
        types = _split_csv(params.get("types"))
        rooms = {int(item) for item in _split_csv(params.get("room_id")) or [] if item.isdigit()}
        keyword = params.get("keyword")

        await websocket.accept()
        # 订阅者数量有上限：超了回一条明确的错误再关，而不是静默挤掉别人。
        # 放在 `accept()` 之后是为了让客户端能**收到**这条说明（否则只会看到握手失败）。
        try:
            subscriber = hub.subscribe(types=types, rooms=rooms or None, keyword=keyword)
        except TooManySubscribers as exc:
            await websocket.send_text(
                json.dumps({"t": "error", "data": {"code": "too_many_subscribers", "hint": str(exc)}},
                           ensure_ascii=False)
            )
            await websocket.close(code=1013)  # 1013 = try again later
            return
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "t": "hello",
                        "data": {
                            "version": __version__,
                            "rooms": panel.rooms(),
                            "filter": {
                                "types": sorted(types) if types else None,
                                "rooms": sorted(rooms) if rooms else None,
                                "keyword": keyword,
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            )
            # 回填最近事件，避免新连接看到空白。
            # 必须带上订阅者的过滤条件，否则过滤订阅会先收到一批不想要的类型。
            # `recent()` 现在返回 `data` 记录（dict），这里用 `encode_event()`
            # 编成与实时推送**同一个出口**的帧，保证两者形状一致。
            for record in hub.recent(
                next(iter(rooms)) if len(rooms) == 1 else None,
                limit=200,
                accept=lambda rec: hub.accepts(subscriber, rec),
            ):
                await websocket.send_text(hub.encode_event(record))

            while True:
                try:
                    payload = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=WS_IDLE_PROBE_SECONDS
                    )
                except asyncio.TimeoutError:
                    # 空闲时发探测包；对端已断开会在 send 处抛错
                    await websocket.send_text(json.dumps({"t": "ping"}))
                    continue

                await websocket.send_text(payload)
                subscriber.sent += 1

                if subscriber.is_slow:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "t": "error",
                                "data": {
                                    "code": "SLOW_CONSUMER",
                                    "dropped": subscriber.dropped,
                                    "hint": "客户端消费过慢，已断开以免影响采集",
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - 连接异常不应影响服务
            log.debug("WS 会话结束: %s: %s", type(exc).__name__, exc)
        finally:
            hub.unsubscribe(subscriber)
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    app.state.started_at = time.time()
    return app


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _split_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _drop_stopwords(words: dict[str, Any], stopwords: set[str]) -> dict[str, Any]:
    """从**已算好的**词频榜里剔除自定义停用词。

    `hub.LiveWindow` 的增量词频用的是内置停用词表，所以自定义停用词只能在这里
    做"事后剔除"。这样做的好处是**零额外分词开销**（榜单只有几十项，
    而重跑 `word_frequency(messages)` 要把窗口内上千条弹幕重新切一遍）。

    代价：被剔除的词不会腾出榜位（榜单长度可能变短），而且 `total_words`
    仍然是剔除前的口径。这是刻意的取舍 —— 榜单是给人看的，
    少几条比每次请求多花几十毫秒更重要（面板 6 秒刷一次）。
    """
    if not stopwords:
        return words
    lowered = {word.lower() for word in stopwords}
    items = [item for item in (words.get("items") or []) if str(item.get("word", "")).lower() not in lowered]
    result = dict(words)
    result["items"] = items
    return result


def _busiest_room(
    store: DanmakuStore,
    *,
    since: float | None,
    until: float | None,
    types: list[str] | None,
    session_id: str | None = None,
) -> int | None:
    """挑出该范围内**事件最多**的房间号（导出文件名需要一个房间号）。

    为什么需要一个"代表房间"：需求要求文件名是 `<主播名字>_<房间号>_...`，
    而"全部房间"这个范围本身没有房间号。与其留空（文件名失去信息量），
    不如取数据量最大的那个 —— 它几乎总是用户关心的那一个。

    为什么在 Python 里累加而不是 `GROUP BY room_id`：`density()` 已经按
    `recv_ts, type` 聚合过一轮（走覆盖索引），再为文件名单独发一条
    `GROUP BY room_id` 的全表聚合不划算。这里对**已按房间过滤过**的小结果集做一次
    `by_room` 请求即可，代价可控。
    """
    try:
        counts = store.count_by_room(since=since, until=until, types=types, session_id=session_id)
    except (AttributeError, StoreError):
        return None
    if not counts:
        return None
    return int(max(counts.items(), key=lambda kv: kv[1])[0])


def _open_store(config: Config) -> DanmakuStore | None:
    from ..cli import _default_db_path

    path = _default_db_path(config)
    try:
        return DanmakuStore(path)
    except StoreError:
        return None


def _resolve_timeout(config: Config) -> float:
    """交互路径（房间解析、批量昵称）的**整体**兜底超时，见 `ApiConfig.resolve_timeout`。

    与 `PanelService._resolve_timeout` 是同一个口径：两处都读同一个配置项，
    只是这里的分支不经过 `PanelService`（`/api/room-names` 直接建 `BiliApi`）。
    """
    value = getattr(getattr(config, "api", None), "resolve_timeout", None)
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return 12.0
    return timeout if timeout > 0 else 12.0


def _open_store_or_409(config: Config) -> DanmakuStore:
    """历史浏览类端点统一的取库入口。

    两条前置条件都要判：
      1. **配置里要启用了 sqlite sink** —— 否则一个"上次运行遗留的 danmaku.db"
         会让历史接口返回陈旧数据，而用户以为自己在看这次的采集（`/api/export`
         早就是这个口径：未启用 sqlite 直接 409）；
      2. 库要真的能打开。
    这样"看到 409"的原因始终是同一个，不需要用户猜。
    """
    if "sqlite" not in config.sinks.types:
        raise HTTPException(status_code=409, detail="未启用 sqlite sink，无法浏览历史")
    opened = _open_store(config)
    if opened is None:
        raise HTTPException(status_code=409, detail="未启用 sqlite sink，无法浏览历史")
    return opened


#: 取不到库时统一 409 的文案（与 `_open_store_or_409` 保持一致）。
STORE_409_DETAIL = "未启用 sqlite sink，无法浏览历史"


async def _run_with_store(config: Config, work: Callable[[DanmakuStore], Any]) -> Any:
    """在**工作线程**里 `开库 → work(store) → 关库`，把结果带回事件循环。

    ## 为什么必须挪到线程里（2026-10-09 实测，这是"web 端卡死"的通用护栏）

    面板里所有 SQLite 调用都是**同步**的（`opened.xxx()` 直接写在 `async def` 里），
    于是任何一条慢查询都会**停住整个事件循环** —— 实测一条 2.5 秒的查询把
    `/api/health` 从 21 ms 拖到 **2412 ms**，期间不响应任何请求、WebSocket 也不推。
    用户看到的就是"整页卡死"。放进线程之后，慢查询只拖慢它自己。

    ⚠️ **开库/干活/关库必须在同一个线程**（`sqlite3` 连接不能跨线程使用），
    所以整段一起交给 `to_thread`；`work` 收到的连接只在它自己线程里用。

    库不可用时抛 `HTTPException(409)`（沿用 `_open_store_or_409` 的文案），
    调用方可以 `except HTTPException` 走降级分支。
    """

    def runner() -> Any:
        if "sqlite" not in config.sinks.types:
            raise HTTPException(status_code=409, detail=STORE_409_DETAIL)
        opened = _open_store(config)
        if opened is None:
            raise HTTPException(status_code=409, detail=STORE_409_DETAIL)
        try:
            return work(opened)
        finally:
            opened.close()

    return await anyio.to_thread.run_sync(runner)


# --------------------------------------------------------------------------- #
# `/api/density` 的三道闸
#
# 实测：单发 87 ms 里 54 ms 是持 GIL 的 Python 折算 ⇒ 并发请求**超线性**变慢
# （1→67ms、2→239ms、4→912ms、8→2.8s、16→6.0s），而前端连点不同粒度档位时
# 去重键各不相同 ⇒ 真的会并发发一堆，约 40 并发就撞上前端 30 s 超时。
# 所以这里：**同参数合并成一次查询** + **结果短 TTL 缓存** + **不同的参数排队**。
# --------------------------------------------------------------------------- #

#: 密度结果的缓存时长（秒）。前端定时刷新是 6 s，用户连点通常落在同一两档上 ——
#: 4 s 足够把"同一组参数的重复请求"全部吃掉，又不会让曲线明显落后于实时数据。
_DENSITY_CACHE_TTL_S = 4.0
#: 最多同时算几个**不同参数**的密度（多出来的排队）。
#: 2 是实测选的：单发 ~90 ms，排一队也只是线性增长，而并发 8 就已经 2.8 s 了。
_DENSITY_MAX_PARALLEL = 2

#: 密度缓存的**条数上限**（P0-2，2026-10-10）。
#:
#: 原实现只有 TTL、**没有任何上限与清扫**：过期判断只发生在"读那个 key 时"
#: （`_density_response` 里的 `if cached is not None and now - cached[0] <= TTL`），
#: 于是每个没被再读一次的 key 就**永久驻留**。key 里含 `since`/`until` 浮点时间戳，
#: 前端每次拖动时间轴都会产生新 key ⇒ 长期运行只增不减。
#: payload 里还带着 `buckets` 列表（最多 max_slots 个桶），单条缓存可以很大。
#:
#: 32 的依据：TTL 只有 4 秒，真正需要同时活着的只有"当前这一两档 + 少量并发客户端"，
#: 32 条足够覆盖，超出的必然是早就没用的历史参数。
_DENSITY_CACHE_MAXSIZE = 32

#: 限流器表的条数上限（见 `_density_limiter`）。生产里只有一个事件循环，
#: 这个上限只在测试反复建循环时才起作用。
_DENSITY_LIMITER_MAXSIZE = 8

#: `key -> (写入时刻, payload)`；`key -> asyncio.Future`（正在算的那一次）
_density_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_density_inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}
#: 每个事件循环一个限流器（`anyio` 的信号量绑在事件循环上，
#: 而测试会反复新建循环，所以按 `id(loop)` 存一份并按需创建）。
_density_limiters: dict[int, Any] = {}


def _density_cache_put(key: str, payload: dict[str, Any]) -> None:
    """写入密度缓存，并顺手做**过期清扫 + 容量淘汰**。

    两条都必须有（原实现两条都没有）：
      * **清扫**：过期项只在自己被读到的时候才会被发现，没被再读的会永久驻留；
      * **上限**：即使都在 TTL 内，拖动时间轴也能瞬间产生上百个不同 key。
    """
    now = time.time()
    _density_cache[key] = (now, payload)

    # 先清扫过期项（O(n)，n <= MAXSIZE，很便宜）
    expired = [
        existing
        for existing, (stamp, _) in _density_cache.items()
        if now - stamp > _DENSITY_CACHE_TTL_S
    ]
    for existing in expired:
        _density_cache.pop(existing, None)

    # 仍然超限就淘汰最旧的（按写入时刻）
    if len(_density_cache) > _DENSITY_CACHE_MAXSIZE:
        overflow = len(_density_cache) - _DENSITY_CACHE_MAXSIZE
        oldest = sorted(_density_cache.items(), key=lambda item: item[1][0])[:overflow]
        for existing, _ in oldest:
            _density_cache.pop(existing, None)


def reset_density_cache() -> None:
    """清空缓存/在飞表/限流器（**测试用**）。

    这些都是模块级状态（必须跨请求复用），不清就没法分别断言
    "第一次真的查库"与"第二次命中缓存/合并"两条路径。
    """
    _density_cache.clear()
    _density_inflight.clear()
    _density_limiters.clear()


#: 渲染预算的缺省值：一屏最多画多少格（见 `_clamp_bucket_s`）。
#: 2000 是按"一格至少半个像素、典型视口 1200px 起"取的宽松值 ——
#: 真正的预算是客户端按视口宽算好传上来的（`max_slots`）。
_DENSITY_DEFAULT_MAX_SLOTS = 2000

#: `/api/stats/export` 的格数预算。
#: 导出不像画图那样有视口，但同样不该产出几十万个桶 ——
#: 那些桶会被复制成行矩阵（两次）并整体 JSON 序列化。
#: 与 density 用同一个量级：2000 格足够表达任何可读的时段分布。
_STATS_EXPORT_MAX_SLOTS = 2000

#: 夹档用的档位阶梯。**必须与 `DanmakuStore.estimate_bucket_s` 的阶梯同源**：
#: 否则"自适应挑出来的档"与"夹档夹到的档"会不一致，用户会看到两个都不在档位上的值。
_BUCKET_LADDER = (10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400)


def _clamp_bucket_s(step: float, lo: float | None, hi: float | None, max_slots: int | None) -> float:
    """把步长往上抬到"符合渲染预算的最细档"。

    为什么需要它：轴的口径是"本次采集起点 → 现在"，所以采集越久格数越多。
    实测 1 分钟档在 24 小时跨度下已是 0.83 px/格（糊），7.8 天是 **11,259 格**
    —— 前端会放弃补零、曲线退化成"斜坡"。而 `MAX_DENSE_SLOTS(5000)` 只是
    "别把路径撑爆"的安全阀，**不是视觉预算**。

    三条规矩：

    * 预算缺省 2000 格（客户端按视口宽给更准的值）；
    * **只往粗抬，绝不下调** —— 用户选了更粗的档位就该照办；
    * 数据范围无效（还没采到数据）时不夹：没有跨度可言，
      夹了会把"空库 + 1 分钟档"变成一个莫名其妙的大档。
    """
    budget = int(max_slots or _DENSITY_DEFAULT_MAX_SLOTS)
    if lo is None or hi is None or hi <= lo or budget <= 0:
        return step
    span = hi - lo
    if span / step <= budget:
        return step
    needed = span / budget
    for candidate in _BUCKET_LADDER:
        if candidate >= needed:
            return float(candidate)
    return float(_BUCKET_LADDER[-1])


def _density_cache_key(
    room_id: int | None,
    session_id: str | None,
    types: list[str] | None,
    since: float | None,
    until: float | None,
    bucket_s: float | None,
    max_slots: int | None = None,
) -> str:
    """缓存/合并键：**凡是影响结果的参数都要在里面**（少一个就会返回错数据）。

    `max_slots` 必须在：它决定服务端会不会夹档，
    两个预算不同的客户端不能共用一份结果。
    """
    return json.dumps(
        {
            "room_id": room_id,
            "session_id": session_id,
            "types": sorted(types) if types else None,
            "since": since,
            "until": until,
            "bucket_s": bucket_s,
            "max_slots": max_slots,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _density_limiter() -> Any:
    """取当前事件循环的限流器（没有就建一个）。

    ⚠️ `_density_limiters` 按 `id(loop)` 存，而 `id()` 在对象回收后**会被复用** ——
    生产里只有一个事件循环（进程生命周期内不回收），所以只有测试反复建循环时
    这个表才会长大。加一条上限纯粹是防止测试进程里无限沉淀；淘汰最旧的即可
    （被淘汰的那个循环通常已经不再发请求了）。
    """
    loop_id = id(asyncio.get_running_loop())
    limiter = _density_limiters.get(loop_id)
    if limiter is None:
        limiter = anyio.CapacityLimiter(_DENSITY_MAX_PARALLEL)
        _density_limiters[loop_id] = limiter
        while len(_density_limiters) > _DENSITY_LIMITER_MAXSIZE:
            oldest = next(iter(_density_limiters))
            if oldest == loop_id:  # pragma: no cover - 只有容量为 1 时才可能
                break
            _density_limiters.pop(oldest, None)
    return limiter


async def _watch_disconnect(request: Request, cancel_event: Any) -> None:
    """轮询"客户端还在不在"，走了就置位 `cancel_event`。

    为什么只能轮询：取消信号来自 HTTP 层（`AbortController`），而干活的是
    **工作线程**里的 Python 循环 —— 两者之间唯一的通道就是一个线程安全的标志。
    Starlette 的 `is_disconnected()` 内部用"立刻取消的 cancel scope"读一次
    `receive`，不会吃掉请求体，也不会阻塞，所以这里每 0.25 s 问一次很便宜。
    """
    try:
        while True:
            await asyncio.sleep(0.25)
            if await request.is_disconnected():
                cancel_event.set()
                log.debug("密度查询：客户端已断开，通知折算收手")
                return
    except asyncio.CancelledError:  # pragma: no cover - 正常收尾
        raise


async def _density_response(
    config: Config,
    request: Request,
    *,
    key: str,
    work: Callable[[DanmakuStore, Any], dict[str, Any]],
) -> dict[str, Any]:
    """带**缓存 + 单飞 + 并发上限 + 可取消**地算一次密度（见上面那段说明）。"""
    import threading

    now = time.time()
    cached = _density_cache.get(key)
    if cached is not None and now - cached[0] <= _DENSITY_CACHE_TTL_S:
        return dict(cached[1])

    inflight = _density_inflight.get(key)
    if inflight is not None:
        # 同一组参数已经在算：**共享那一次**，别再起一遍（单飞）
        return dict(await asyncio.shield(inflight))

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    _density_inflight[key] = future
    cancel_event = threading.Event()
    watcher = asyncio.create_task(_watch_disconnect(request, cancel_event))
    started = time.perf_counter()
    try:
        async with _density_limiter():
            payload = await _run_with_store(config, lambda opened: work(opened, cancel_event))
    except BaseException as exc:  # noqa: BLE001 - 失败也要把在飞表清掉，否则同参数永久挂住
        _density_inflight.pop(key, None)
        if not future.done():
            future.set_exception(exc)
        raise
    finally:
        watcher.cancel()
        if _density_inflight.get(key) is future:
            _density_inflight.pop(key, None)

    elapsed_ms = (time.perf_counter() - started) * 1000
    # 被叫停的那份**不写缓存**：它是半成品，谁也别用
    if payload.get("cancelled"):
        log.info("密度查询被客户端取消（%.0f ms，%s）", elapsed_ms, key)
    else:
        _density_cache_put(key, payload)
        log.debug("密度查询 %.0f ms（桶 %d，%s）", elapsed_ms, len(payload.get("buckets") or []), key)
    if not future.done():
        future.set_result(payload)
    return dict(payload)


async def _fill_room_top_senders(
    payload: dict[str, Any],
    *,
    config: Config,
    room_id: int,
    since: float | None,
    scope: str | None,
) -> None:
    """把**某个房间**的发言榜写进 `payload`（查库，在工作线程里跑）。

    取不到库时按既有约定区分两种原因（前端 `statsHint.js` 据此给不同提示）：
      * **配置里没启用 sqlite** → 用户需要去改配置；
      * **还没建库**（全新安装 / 还没采到数据）→ 正常现象，不该报"坏了"。
    """

    def work(store: DanmakuStore) -> dict[str, Any]:
        senders, scope_text = _cumulative_top_senders(
            store, room_id, since=since, limit=10
        )
        return {"top_senders": senders, "top_senders_scope": scope or scope_text}

    try:
        payload.update(await _run_with_store(config, work))
    except HTTPException:
        if "sqlite" not in config.sinks.types:
            payload["top_senders_error"] = "sqlite sink 未启用"
        else:
            payload["top_senders_error"] = "数据库尚未建立"


def _window_top_senders(
    store: DanmakuStore,
    room_id: int | None,
    *,
    since: float | None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """滑动窗口口径的发言榜。

    ⚠️ **不给 `room_id` 时按房间分别查再合并**，而不是全库 `GROUP BY uid`：
    后者用不上 `recv_ts` 索引，1 GB 库上要 2.6~35 秒（见 `hub.cumulative_top_senders`）。
    按房间查走 `idx_event_room_ts_type`，每房间 0.2~7 ms；
    房间列表用 `store.room_ids()`（覆盖索引扫描，实测 16 ms / 244k 行）。

    保持"**库里的全部房间**"这个口径（与同一响应里的 `by_source`/延迟分位一致）——
    只按"当前监控中的房间"合并会让两者自相矛盾。

    每个房间取 `limit * 5` 条再合并，避免"两个房间各排第 11 名、合并后其实该进前 10"
    这种漏人（展示口径够用，且总量有界）。
    """
    if room_id is not None:
        return store.top_senders(limit=limit, room_id=room_id, since=since)
    merged: dict[int, dict[str, Any]] = {}
    per_room = max(int(limit) * 5, int(limit))
    for item in store.room_ids():
        for sender in store.top_senders(limit=per_room, room_id=item, since=since):
            uid = int(sender["uid"])
            entry = merged.setdefault(uid, {"uid": uid, "uname": None, "count": 0})
            entry["count"] += int(sender["count"])
            name = sender.get("uname")
            if name:
                entry["uname"] = name
    ranked = sorted(merged.values(), key=lambda item: (-item["count"], item["uid"]))
    return ranked[:limit]


#: 「全部房间」发言榜的缓存**已随 2026-10-09 的修正删除**。
#:
#: 它当年是为"全库 `top_senders` 101.6 ms、而 `/api/stats` 每 3 秒拉一次"加的。
#: 但那个 101.6 ms 是**小库**上的数：真库（1 GB / 244,036 行）上同一条查询要
#: **2.6~35 秒**，而面板的 SQLite 调用是同步跑在事件循环上的 ⇒ TTL 15 秒根本救不了
#: （实测连续 6 次 3 秒轮询：`20/8/5/5/7/2551` ms，第 6 次又炸，期间整个面板停摆）。
#: 现在"全部房间"这条路**完全不查库**（`hub.cumulative_top_senders()` 读内存计数），
#: 缓存自然没有存在意义 —— 留着只会让人以为那条慢查询还在。
#:
#: ⚠️ 别再"顺手加回来"：任何"缓存全库聚合"的写法都挡不住第一发冷查询，
#: 而第一发就足以让界面卡死几十秒。


def _cumulative_top_senders(
    store: DanmakuStore | None,
    room_id: int | None,
    *,
    since: float | None = None,
    hub: Any = None,
    limit: int = 10,
) -> tuple[list[dict[str, Any]] | None, str]:
    """累计口径的发言榜 —— **只算本次采集开始之后**（需求）。

    为什么必须传 `since`：不传就是在查**该房间的全部历史**。实测某房间库里跨
    60.6 小时，于是面板上"本次采集以来"的榜单里全是往次直播的人
    （`Neuf-Sept 168` 那种），而本次真正在说话的（`翎夜律 10`）根本挤不进去 ——
    与"累计口径 = 自启动以来"的语义自相矛盾。

    顺带**更快**（`tools/bench_top_senders_since.py`）：单房间 42.1 ms → 0.0 ms，
    因为 `idx_event_room_ts_type(room_id, recv_ts, type)` 让 `recv_ts >= ?` 走区间扫描。

    ## "全部房间"这条**不走 SQL**（2026-10-09 实测修正，见 `hub.cumulative_top_senders`）

    不带 `room_id` 的 `GROUP BY uid` 在 1 GB 库上要 **2.6~35 秒**（`recv_ts >= ?`
    没有可用索引），而它跑在事件循环上 ⇒ **整个面板卡死**。累计口径 = 本次运行以来，
    而 hub 手里正好有全量事件 ⇒ 直接读内存计数（O(#uid)、零查询）。
    `store` 允许传 `None`：那时只走内存这条路。
    """
    scope_text = "本次采集以来"

    if room_id is not None:
        if store is None:
            return None, scope_text
        return store.top_senders(limit=limit, room_id=room_id, since=since), scope_text

    if hub is None:
        return None, scope_text
    return hub.cumulative_top_senders(limit=limit), f"全部房间（{scope_text}）"


def _render_batch(buffer: Any, batch: list[Any], format: str, *, header: bool) -> str:
    """把一批事件渲染成一段文本（CSV 只在**第一段**带表头）。

    `_write_records`（CLI 与这里共用）接受 `Event` 或 dict —— 瘦投影路径给的是
    dict（`store._slim_event`），完整路径给的是 `Event`。
    """
    from ..cli import _write_records

    buffer.seek(0)
    buffer.truncate(0)
    _write_records(buffer, batch, format)
    chunk = buffer.getvalue()
    if format == "csv" and not header and "\n" in chunk:
        chunk = chunk.split("\n", 1)[1]  # 去掉重复的表头行
    return chunk


def _content_disposition(filename: str) -> str:
    """给**含中文**的下载文件名生成 `Content-Disposition`（RFC 5987）。

    需求要求原始导出叫
    `<主播昵称>_<日期>_<时间>_弹幕导出.csv` —— 里面有中文，不能只塞进
    `filename="..."`（那是 Latin-1，中文会变乱码或被丢弃）。
    所以两段都给：`filename=` 给一个**纯 ASCII 兜底**，`filename*=UTF-8''…`
    给真名 —— 现代浏览器优先用后者，老浏览器退化成 `danmaku-<时间戳>.csv`。
    """
    from urllib.parse import quote

    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "danmaku-export.csv"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


def _export_filename(anchor_name: str | None, suffix: str) -> str:
    """`<主播昵称>_<日期>_<时间>_弹幕导出.<ext>`（需求 2026-10-09 第七轮）。

    * 昵称取不到时用 `房间弹幕`（不留空段，否则文件名会以 `_` 开头看着像坏的）；
    * 文件名里的非法字符清掉（主播昵称可能带 `/`，那会变成路径）；
    * 日期与时间用**服务端本地时间**（用户看的也是本地时间）。
    """
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", anchor_name or "").strip() or "房间弹幕"
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{safe}_{stamp}_弹幕导出.{suffix}"


def _stream_export(
    format: str,
    events_factory: Any,
    *,
    total: int | None = None,
    range_info: dict[str, Any] | None = None,
    filename: str | None = None,
) -> StreamingResponse:
    """把事件迭代器包成流式下载响应（`/api/export` 与按标记区间导出共用）。

    传的是**工厂**（`Callable[[], Iterator[Event]]`）而不是现成的迭代器：
    Starlette 用线程池**惰性**驱动同步生成器，如果调用方在协程里先开好连接、
    函数一返回就把连接关掉，生成器真正开始跑时数据库已经关了
    （实测 `sqlite3.ProgrammingError: Cannot operate on a closed database`）。
    所以连接的开与关都必须发生在生成器**内部**。

    `format == "csv"` 时首块带 **UTF-8 BOM**：浏览器下载的 CSV 没有 Content-Type 的
    charset 线索，中文版 Excel 会按系统码页（GBK）打开，UTF-8 的中文就全成乱码
    （用户实测反馈）。本地 CLI 导出（`_open_output`）一直带 BOM，这里对齐它。
    """
    from ..cli import _write_records
    import io

    suffix = {"csv": "csv", "jsonl": "jsonl", "txt": "txt"}[format]
    if filename is None:
        filename = f"danmaku-{int(time.time())}.{suffix}"

    def generate() -> Any:
        buffer = io.StringIO()
        events = events_factory()

        def as_payload(event: Any) -> dict[str, Any]:
            """`Event` 或**瘦投影 dict**都能导出。"""
            return event if isinstance(event, dict) else event.to_dict()

        # ── **批量**写，不再一行一次 ──────────────
        #
        # 改前是"每条事件一次 `_write_records` + 一次 yield"，5 万条要 14~16 秒
        # 慢在三处：每条都构造完整 `Event`
        # （含 `raw_json` 的解析 + pydantic 校验）、每条都过一次
        # `csv.writer`、每条都过一次生成器/线程池边界。
        # 现在：一次取 `EXPORT_BATCH_ROWS` 条（瘦投影时更是绕开 pydantic），
        # 一批写一次、一批 yield 一次。对上层完全透明（还是一个流式响应）。
        #
        # ⚠️ 之后 `raw` 是 **JSON 文本**、不再有解析往返（见 `models.Event.raw`），
        # 于是这条路径剩下的唯一大头就是 pydantic 构造 + 序列化。
        chunk_size = 64 * 1024 if format == "jsonl" else 512
        if format == "jsonl":
            for event in events:
                yield json.dumps(as_payload(event), ensure_ascii=False) + "\n"
            return

        written_any = False
        batch: list[Any] = []
        for event in events:
            batch.append(event)
            if len(batch) < chunk_size:
                continue
            first = not written_any
            chunk = _render_batch(buffer, batch, format, header=first)
            batch = []
            if chunk:
                written_any = True
                # ⚠️ BOM **只给 CSV**（Excel 认它）；jsonl/txt 加了 BOM 会污染首行
                yield (CSV_BOM + chunk) if (first and format == "csv") else chunk
        if batch:
            first = not written_any
            chunk = _render_batch(buffer, batch, format, header=first)
            if chunk:
                yield (CSV_BOM + chunk) if (first and format == "csv") else chunk

    # 文件名用 ASCII：Content-Disposition 里塞非 ASCII 需要 RFC 5987 编码，
    # 没必要为"房间/会话"信息冒兼容风险（内容里有全部字段）。
    media = {
        "csv": "text/csv; charset=utf-8",
        "jsonl": "application/x-ndjson; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
    }[format]
    headers = {"Content-Disposition": _content_disposition(filename)}
    if total is not None:
        headers["X-Total-Count"] = str(total)
    if range_info:
        headers["X-Range-Since"] = str(range_info.get("since"))
        headers["X-Range-Until"] = str(range_info.get("until"))
    return StreamingResponse(generate(), media_type=media, headers=headers)


def _iter_range_events(
    config: Config,
    *,
    room_id: int | None,
    types: list[str] | None,
    keyword: str | None,
    since: float | None,
    until: float | None,
    limit: int | None,
    slim: bool = False,
) -> Iterator[Any]:
    """流式产出某区间的事件（连接在生成器内部开关，见 `_stream_export`）。

    `slim=True` 走**瘦投影**：不构造 pydantic `Event`、不解析 `raw_json`，
    直接由行拼 dict —— 导出快 3~5 倍，代价是**没有原始帧**（`raw`）。
    """
    store = _open_store(config)
    if store is None:  # pragma: no cover - 调用方已用 _open_store_or_409 挡过
        return
    try:
        yield from store.iter_events(
            room_id=room_id, types=types, keyword=keyword, since=since, until=until, limit=limit,
            slim=slim,
        )
    finally:
        store.close()


def _memory_events(
    hub: WebHub,
    room_id: int | None,
    types: set[str] | None,
    keyword: str | None,
    limit: int,
) -> dict[str, Any]:
    """没有 SQLite 时的降级路径：只查内存环形缓冲。

    `hub.recent()` 返回的是 `data` 记录（dict）—— 之后环形缓冲直接持有 dict，
    所以这里**不必再 `json.loads`**（既省一次解析，也不用处理脏数据）。
    """
    items: list[dict[str, Any]] = []
    for event in hub.recent(room_id, limit=limit * 2):
        if not isinstance(event, dict):
            continue
        if types and event.get("type") not in types:
            continue
        if keyword and keyword.lower() not in (event.get("text") or "").lower():
            continue
        items.append(event)
    return {"total": len(items), "items": items[-limit:], "source": "memory"}


async def _stats_pusher(panel: PanelService, hub: WebHub) -> None:
    """周期性推送房间与统计状态。"""
    while True:
        await asyncio.sleep(STATS_PUSH_SECONDS)
        try:
            if hub.subscriber_count:
                hub.publish_control("rooms", {"rooms": panel.rooms()})
        except Exception as exc:  # noqa: BLE001
            log.debug("推送状态失败: %s", exc)


async def _warm_store(config: Config) -> None:
    """启动后台预热采集库。

    ## 为什么要有它

    面板起来后的**第一次**开库要付"从磁盘拉页缓存"的钱，实测（真库 281,890 行）：

    | 请求 | 冷（首次开库） | 热 |
    |------|--------------|----|
    | `/api/sessions?limit=50` | 389 ms | 34 ms |
    | `/api/room-names?rooms=…` | **2159 ms** | 360 ms |

    而这两个恰好都在**首屏**路径上 —— 用户"第一次打开面板"的体感就是被它们拖的。
    启动后趁用户还没点，先跑一遍**同样的轻量查询**（会话列表、房间列表、
    时间范围、房间昵称聚合），把索引页与表页都带进页缓存。

    ## 三条约束

    * **不阻塞启动**：由 `lifespan` 用 `create_task` 起，失败也不影响面板可用；
    * **只读、只走轻量查询**：不碰事件表的大范围扫描（那是导出的事）；
    * 没启用 sqlite 时**安静跳过**（`_run_with_store` 会抛 409，这里吞掉）。
    """

    def work(opened: DanmakuStore) -> dict[str, Any]:
        rooms = opened.room_ids()[:8]  # 覆盖索引扫描，顺带把房间页拉热
        return {
            "sessions": len(opened.sessions(limit=1)),
            "rooms": len(rooms),
            "names": len(opened.room_anchor_names(rooms)),
            "range": opened.time_range(),
        }

    started = time.perf_counter()
    try:
        summary = await _run_with_store(config, work)
    except Exception as exc:  # noqa: BLE001 - 预热失败绝不该影响面板
        log.debug("采集库预热跳过：%s", exc)
        return
    log.info("采集库预热完成（%.0f ms，%s）", (time.perf_counter() - started) * 1000, summary)
