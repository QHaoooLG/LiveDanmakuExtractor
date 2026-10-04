"""FastAPI 应用：REST + WebSocket。

前端是构建产物（`web/dist`）；**若前端未构建，服务仍会启动**并给出一个
内建提示页，而不是直接报错 —— 面板不该因为缺 Node 而让整个工具不可用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

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
from ..rooms import parse_room_input
from ..store import DanmakuStore, StoreError
from ..text import add_custom_words, jieba_available, load_stopwords
from .hub import ANALYSIS_BUCKET_S, ANALYSIS_WINDOW_MESSAGES, WebHub
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
        log.info("面板就绪：前端 %s", dist or "未构建（使用内建提示页）")
        try:
            yield
        finally:
            push_task.cancel()
            await asyncio.gather(push_task, return_exceptions=True)
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
    # 只有一个端点，而且只做"停止"。安全边界（见 HANDOVER §6.4 DEC-3）：
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
        except Exception as exc:  # noqa: BLE001 - 房间不存在/接口失败统一 400
            raise HTTPException(status_code=400, detail=f"添加房间失败: {exc}") from exc
        self_snapshot = runtime.snapshot()
        hub.publish_control("rooms", {"rooms": panel.rooms()})
        return {"room": self_snapshot}

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
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        types = [item.strip() for item in type.split(",") if item.strip()] if type else None
        store = _open_store(config)
        if store is None:
            return _memory_events(hub, room_id, types, q, limit)

        try:
            total = store.count(room_id=room_id, types=types, keyword=q, since=since, until=until)
            events = list(
                store.iter_events(
                    room_id=room_id,
                    types=types,
                    keyword=q,
                    since=since,
                    until=until,
                    limit=limit + offset,
                )
            )
        finally:
            store.close()

        window = events[offset : offset + limit]
        return {
            "total": total,
            "items": [event.model_dump(mode="json") for event in window],
            "source": "sqlite",
        }

    # ------------------------------------------------------------------ #
    # 历史浏览（V3-a）：游标分页 / 时间轴密度 / 会话列表
    #
    # 三个端点都**只读**、都不启动采集。它们的存在理由见 ROADMAP §2：
    # 数据一直是全的，缺的是"能翻页、能定位"的通路。
    #
    # 与 `/api/events` 的分工（故意不合并）：
    #   * `/api/events` 面向脚本/小结果集：完整 Event、支持 offset；
    #   * `/api/history` 面向面板翻页：**瘦投影** + `(recv_ts, id)` 复合游标。
    # 不要把 `/api/history` 的投影逻辑塞回 `/api/events` —— 那会悄悄改窄既有契约。
    # ------------------------------------------------------------------ #

    def _history_filters(
        room_id: int | None, session_id: str | None, type: str | None, q: str | None
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if room_id is not None:
            filters["room_id"] = room_id
        if session_id:
            filters["session_id"] = session_id
        if type:
            filters["types"] = [item.strip() for item in type.split(",") if item.strip()]
        if q:
            filters["keyword"] = q
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
        """
        opened = _open_store_or_409(config)

        before = (before_ts, before_id) if before_ts is not None and before_id is not None else None
        after = (after_ts, after_id) if after_ts is not None and after_id is not None else None
        if before is not None and after is not None:
            raise HTTPException(status_code=400, detail="before_* 与 after_* 不能同时使用")

        filters = _history_filters(room_id, session_id, type, q)
        try:
            lo, hi = _anchor_ts(opened, filters)
            pane = HISTORY_PAGE_WINDOW_S if window_s is None else float(window_s)
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
        finally:
            opened.close()

        oldest = (items[0]["recv_ts"], items[0]["id"]) if items else None
        newest = (items[-1]["recv_ts"], items[-1]["id"]) if items else None
        return {
            "items": items,
            "has_older": has_older,
            "has_newer": has_newer,
            "oldest": oldest,
            "newest": newest,
            "source": "sqlite",
            "room_id": room_id,
            "session_id": session_id,
            "window_s": 0.0 if room_id is not None else pane,
            "range": {"lo": lo, "hi": hi},
        }

    @app.get("/api/density")
    async def density(
        room_id: int | None = None,
        session_id: str | None = None,
        type: str | None = None,
        since: float | None = None,
        until: float | None = None,
        bucket_s: float | None = Query(
            None, ge=1, le=86400, description="桶宽（秒）；缺省按数据跨度自适应"
        ),
    ) -> dict[str, Any]:
        """时间轴密度：按桶聚合的条数分布（时间轴/密度条的数据源）。

        聚合在 SQL 里完成（实测 ~1 ms / 104 桶）。**不要**退回"把事件搬到前端再算" ——
        那正是旧的 `iter_events(limit=20100)` 路径在干的事（约 0.9 s）。
        """
        opened = _open_store_or_409(config)

        try:
            filters: dict[str, Any] = {}
            if room_id is not None:
                filters["room_id"] = room_id
            if session_id:
                filters["session_id"] = session_id
            lo, hi = opened.time_range(**filters)
            step = bucket_s if bucket_s is not None else opened.estimate_bucket_s(lo, hi)
            payload = opened.density(
                bucket_s=step,
                since=since,
                until=until,
                include_types=[item.strip() for item in type.split(",") if item.strip()]
                if type
                else None,
                **filters,
            )
        finally:
            opened.close()
        return payload

    @app.get("/api/sessions")
    async def sessions(
        room_id: int | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        """抓取会话列表（含起止时间与事件数），供"按某次抓取浏览历史"用。"""
        opened = _open_store_or_409(config)
        try:
            items = opened.sessions(limit=limit, room_id=room_id)
        finally:
            opened.close()
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
    ) -> dict[str, Any]:
        """导出**统计 CSV**：弹幕统计 + 每时段弹幕量，**同一个文件多张表**。

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
        """
        from ..export_stats import DEFAULT_BUCKET_S, build_stats_csv

        opened = _open_store_or_409(config)
        try:
            types = [item.strip() for item in type.split(",") if item.strip()] if type else None
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
            density = opened.density(bucket_s=step, since=start, until=end, **filters)
            senders = opened.top_senders(limit=10, since=start, until=end, **filters)

            # 选了具体房间就用它；否则从桶里挑**事件最多**的那个房间当"这份报告的主房间"
            # （文件名需要一个房间号，而为空会让文件名退化得没有信息量）。
            effective_room = room_id
            if effective_room is None:
                effective_room = _busiest_room(
                    opened, since=start, until=end, types=types, session_id=session_id
                )

            payload: dict[str, Any] = {
                "exported_at": time.time(),
                "since": start,
                "until": end,
                "bucket_s": step,
                "room_id": effective_room,
                "anchor_name": panel.anchor_name(effective_room),
                "total": sum(int(v or 0) for v in (density.get("by_type") or {}).values()),
                "by_type": density.get("by_type") or {},
                "buckets": density.get("buckets") or [],
                "top_senders": senders,
            }
        finally:
            opened.close()

        return {"filename_hint": "", "csv": build_stats_csv(payload), **payload}

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

        # 先把"能不能导"判掉（409/错误码必须在开始流式输出**之前**发出）
        probe = _open_store_or_409(config)
        types = [item.strip() for item in type.split(",") if item.strip()] if type else None
        try:
            total = probe.count(room_id=scope_room, types=types, keyword=q, since=since, until=until)
        finally:
            probe.close()

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
    # 这个端点**不做任何外发**：LLM 那类"把弹幕发出去"的能力属于 ROADMAP §3.2 3B，
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
            # 发言 TOP：**累计口径下也默认给**（数据来自 SQLite，不是 hub 内存计数）。
            #
            # 代价必须控住：实测本机库 `top_senders` 单房间 6.4 ms、**全库 101.6 ms**
            # （按 uid 分组排序没有合适索引），而 `/api/stats` 每 3 秒拉一次 ——
            # 直接查全库等于每 3 秒白扫一次全表。
            # 所以两条路：
            #   * 选了具体房间 → 每次都查（6 ms，可以忽略）；
            #   * "全部房间" → 走 `_TOP_SENDERS_CACHE`（TTL 内复用上一次结果）。
            opened = _open_store(config)
            if opened is not None:
                try:
                    senders, scope = _cumulative_top_senders(opened, room_id)
                    if senders is not None:
                        payload["top_senders"] = senders
                        payload["top_senders_scope"] = scope
                finally:
                    opened.close()
            else:
                # 打开失败最常见的两种原因要分开说，否则前端只能猜：
                #   * **还没建库**（全新安装 / 还没采到数据）→ 正常现象，不该报"坏了"；
                #   * **配置里没启用 sqlite** → 用户需要去改配置。
                # 前端据此给出**不同**的提示（见 web/src/components/statsHint.js）。
                if "sqlite" not in config.sinks.types:
                    payload["top_senders_error"] = "sqlite sink 未启用"
                else:
                    payload["top_senders_error"] = "数据库尚未建立"
            return payload

        # 滑动窗口：以 hub 内存计数为主，SQLite 只用来补延迟分位与发言榜
        elapsed = max(window, 1)
        payload["msgs_per_min"] = round(payload["msgs_total"] * 60.0 / elapsed, 1)

        store = _open_store(config)
        if store is None:
            payload["recent"] = len(hub.recent(room_id, limit=1000))
            return payload

        try:
            scope = {"room_id": room_id}
            p50, p95 = store.latency_percentiles(since=since, **scope)
            payload.update(
                {
                    "source": "sqlite",
                    "db": str(store.path),
                    "by_source": store.by_source(since=since, **scope),
                    "latency_p50_ms": p50,
                    "latency_p95_ms": p95,
                    "top_senders": store.top_senders(limit=10, since=since, **scope),
                }
            )
        finally:
            store.close()
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
    ) -> Any:
        # 可用性检查必须发生在**返回响应之前**：一旦开始流式输出，状态码就已经发出去了，
        # 那时再抛 HTTPException 只会变成一个断掉的 200（实测由 test_export_requires_sqlite 抓到）。
        if _open_store(config) is None:
            raise HTTPException(status_code=409, detail="未启用 sqlite sink，无法导出")

        types = [item.strip() for item in type.split(",") if item.strip()] if type else None
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
        subscriber = hub.subscribe(types=types, rooms=rooms or None, keyword=keyword)
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
            for payload in hub.recent(
                next(iter(rooms)) if len(rooms) == 1 else None,
                limit=200,
                accept=lambda record: hub.accepts(subscriber, record),
            ):
                await websocket.send_text(payload)

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


#: "全部房间"发言榜的缓存：`{scope_key: (expire_at, senders)}`。
#: 全库 `top_senders` 实测 101.6 ms，而 `/api/stats` 每 3 秒拉一次 ——
#: 不缓存就是每 3 秒白扫一次全表。TTL 内复用上一次结果（榜单本来变化就慢）。
_TOP_SENDERS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
#: 缓存有效期（秒）
TOP_SENDERS_TTL_S = 15.0


def _cumulative_top_senders(
    store: DanmakuStore, room_id: int | None
) -> tuple[list[dict[str, Any]] | None, str]:
    """累计口径的发言榜。

    选具体房间时直接查（6 ms 量级）；"全部房间"走 TTL 缓存（101 ms 量级）。
    榜单范围带在返回值里，前端必须把它显示出来 —— 否则"该房间全部时间"
    会被误读成"自面板启动以来"（两者差着往次运行与历史补种的数据）。
    """
    if room_id is not None:
        return store.top_senders(limit=10, room_id=room_id), "该房间全部时间"

    key = "all"
    hit = _TOP_SENDERS_CACHE.get(key)
    now = time.time()
    if hit is not None and hit[0] > now:
        return hit[1], "全部房间（缓存）"
    senders = store.top_senders(limit=10)
    _TOP_SENDERS_CACHE[key] = (now + TOP_SENDERS_TTL_S, senders)
    return senders, "全部房间"


def reset_top_senders_cache() -> None:
    """清空发言榜缓存。

    存在的理由：缓存是**模块级**的（应该如此，它要跨请求复用），
    于是它会跨测试用例存活 —— 测试需要能把它清干净，否则"第一次查询"
    与"命中缓存"两条路径没法分别断言。
    """
    _TOP_SENDERS_CACHE.clear()


def _stream_export(
    format: str,
    events_factory: Any,
    *,
    total: int | None = None,
    range_info: dict[str, Any] | None = None,
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
    filename = f"danmaku-{int(time.time())}.{suffix}"

    def generate() -> Any:
        buffer = io.StringIO()
        events = events_factory()
        if format == "jsonl":
            for event in events:
                yield json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n"
            return
        written_any = False
        for event in events:
            buffer.seek(0)
            buffer.truncate(0)
            _write_records(buffer, [event], format)
            chunk = buffer.getvalue()
            # csv 首行是表头，只在第一条时输出
            if format == "csv" and written_any:
                chunk = chunk.split("\n", 1)[1] if "\n" in chunk else ""
            if not chunk:
                continue
            if format == "csv" and not written_any:
                chunk = CSV_BOM + chunk
            written_any = True
            yield chunk

    # 文件名用 ASCII：Content-Disposition 里塞非 ASCII 需要 RFC 5987 编码，
    # 没必要为"房间/会话"信息冒兼容风险（内容里有全部字段）。
    media = {
        "csv": "text/csv; charset=utf-8",
        "jsonl": "application/x-ndjson; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
    }[format]
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
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
) -> Iterator[Any]:
    """流式产出某区间的事件（连接在生成器内部开关，见 `_stream_export`）。"""
    store = _open_store(config)
    if store is None:  # pragma: no cover - 调用方已用 _open_store_or_409 挡过
        return
    try:
        yield from store.iter_events(
            room_id=room_id, types=types, keyword=keyword, since=since, until=until, limit=limit
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
    """没有 SQLite 时的降级路径：只查内存环形缓冲。"""
    items: list[dict[str, Any]] = []
    for raw in hub.recent(room_id, limit=limit * 2):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        event = payload.get("data") or {}
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
