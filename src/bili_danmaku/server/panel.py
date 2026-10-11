"""面板服务：管理多个房间的采集器，并把事件汇入 WebHub。

与 CLI `grab` 复用同一套 `RoomCollector` / `Bus` / `Sink`，
**面板不是另一套抓取逻辑** —— 它只是多挂了一个 `WebHubSink`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..api import BiliApi, BiliApiError
from ..collector import MAX_RECONNECT_ATTEMPTS, CollectorStats, RoomCollector
from ..config import Config
from ..credentials import Credentials
from ..filters import build_filter
from ..models import Event, RoomInfo, SessionMeta
from ..rooms import parse_room_input
from ..sinks import Bus, Sink, build_sinks
from .hub import WebHub

log = logging.getLogger("bili_danmaku.server.panel")

#: 房间信息缓存的条数上限（见 `PanelService._room_cache`）。
#: 单条约 1 KB ⇒ 500 条约 0.5 MB，足够覆盖用户会翻到的房间。
ROOM_CACHE_CAP = 500


class WebHubSink(Sink):
    """把事件转发给 WebHub。永不因订阅者问题影响采集。"""

    name = "web"

    def __init__(self, config: Config, hub: WebHub) -> None:
        super().__init__(config)
        self.hub = hub

    def write(self, events: list[Event]) -> None:
        for event in events:
            self.hub.publish(event)
        self.record(len(events))


@dataclass
class RoomRuntime:
    """一个正在采集的房间。"""

    room: RoomInfo
    request: str
    collector: RoomCollector
    task: asyncio.Task[CollectorStats]
    bus: Bus
    started_at: float = field(default_factory=time.time)
    error: str | None = None

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> str:
        if self.task.done():
            if self.task.cancelled():
                return "STOPPED"
            exception = self.task.exception()
            return f"FAILED: {exception}" if exception else "STOPPED"
        return self.collector.state.value

    def snapshot(self) -> dict[str, Any]:
        stats = self.collector.stats
        state = self.state
        return {
            "room_id": self.room.room_id,
            "short_id": self.room.short_id,
            "anchor_uid": self.room.anchor_uid,
            "anchor_name": self.room.anchor_name,
            "anchor_face": self.room.anchor_face,
            "live_status": self.room.live_status,
            "conn_state": state,
            "token_source": self.collector._meta.token_source if self.collector._meta else None,
            "session_id": self.collector.session_id,
            "uptime_s": round(time.time() - self.started_at, 1),
            "received": stats.received,
            "stored": stats.parsed,
            "duplicates": stats.duplicates,
            "filtered": stats.filtered,
            "dropped": stats.dropped,
            "reconnects": stats.reconnects,
            "popularity": stats.last_popularity,
            "last_message_at": stats.last_message_at,
            "by_type": dict(stats.by_type),
            "error": self.error,
            # 上次连接失败的原因与归类 —— 面板据此区分"请重新登录"与"网络问题"。
            # 缺了这两个字段时，**运行中**的凭证失配对用户完全不可见（只能翻日志，
            # 而日志里当时连原因都没写，见 collector._remember_error 的说明）。
            "last_error": stats.last_error,
            "last_error_kind": stats.last_error_kind,
            # 手动重连（需求 2026-10-09）：`manual_reconnect_needed` 是**推导值**，
            # 与连接状态同源（见 collector.manual_reconnect_needed 的说明），
            # 前端据此决定"要不要显示那个按钮"。
            "manual_reconnect_needed": self.collector.manual_reconnect_needed,
            "manual_reconnect_limit": MAX_RECONNECT_ATTEMPTS,
            "manual_reconnects": stats.manual_reconnects,
        }


class PanelService:
    """面板的生命周期与房间管理。"""

    def __init__(self, config: Config, credentials: Credentials, hub: WebHub) -> None:
        self.config = config
        self.credentials = credentials
        self.hub = hub
        self.api: BiliApi | None = None
        self._rooms: dict[int, RoomRuntime] = {}
        self._stopping = False
        #: room_id → 房间/主播信息（含已停止监控的房间）。进程内缓存：
        #: 房间号→真实房间号→主播昵称这条链路要走两次网络接口，
        #: 而昵称几乎不会变，重复查只是在给风控送请求。
        #:
        #: **有上限**：`/api/room-info?room=<任意号>` 可以让用户
        #: 塞进任意多个房间号，原实现"进程内永久缓存"于是随输入无界增长。
        #: 房间信息很小（~1 KB），500 条约 0.5 MB，够用且封顶。
        self._room_cache: OrderedDict[int, RoomInfo] = OrderedDict()
        self._room_cache_cap = ROOM_CACHE_CAP

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self.api = await BiliApi(self.config, self.credentials).__aenter__()
        # 设备指纹缺失时自动补齐（提升连接稳定性；失败不阻断启动）
        if not self.credentials.buvid3:
            try:
                buvid3, buvid4 = await self.api.fingerprint()
                if buvid3:
                    self.credentials = self.credentials.with_buvid(buvid3, buvid4)
                    log.debug("已补齐设备指纹 buvid3")
            except Exception as exc:  # noqa: BLE001
                log.warning("获取 buvid 失败（不影响启动）: %s", exc)

    async def stop(self) -> None:
        self._stopping = True
        for room_id in list(self._rooms):
            await self.remove_room(room_id)
        if self.api is not None:
            await self.api.__aexit__(None, None, None)
            self.api = None

    # ------------------------------------------------------------------ #
    def _build_bus(self) -> Bus:
        sinks: list[Sink] = build_sinks(list(self.config.sinks.types), self.config)
        sinks.append(WebHubSink(self.config, self.hub))
        return Bus(sinks, self.config)

    async def _with_anchor(self, room: RoomInfo) -> RoomInfo:
        """给房间补上主播昵称。取不到就原样返回（不抛异常）。"""
        if room.anchor_name:
            return room
        lookup = getattr(self.api, "anchor_info", None)
        if lookup is None:  # 假 API（测试桩）没有这一步
            return room
        anchor = await lookup(room.room_id)
        if not anchor:
            return room
        return room.model_copy(
            update={
                "anchor_name": anchor.get("uname") or None,
                "anchor_face": anchor.get("face") or None,
            }
        )

    def _resolve_timeout(self) -> float:
        """整条「房间号 → 房间信息」链路的兜底超时（秒）。见 `ApiConfig.resolve_timeout`。"""
        value = getattr(getattr(self.config, "api", None), "resolve_timeout", None)
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return 12.0
        return timeout if timeout > 0 else 12.0

    async def _resolve_room(self, room_input: int | str) -> RoomInfo:
        """房间号 → `RoomInfo`（含主播昵称），**进程内缓存优先**。

        主播昵称要走两次网络接口，而它几乎不变 —— 所以命中缓存就不再打接口：
        这既省往返，也少给风控送请求（`_room_cache` 在 `PanelService` 里）。

        ⚠️ 整条链路外面套了 `asyncio.wait_for`（`resolve_timeout`，缺省 12 s）：
        系统代理不通时 `room_init`（重试 3 次）+ `anchor_info` 能叠到 **30 s**，
        而 `/api/room-info`、`/api/rooms` 是**用户交互路径** —— 前端 `RoomPanel`
        在这些请求挂起期间会把输入框和按钮一直禁着，用户看到的就是"面板卡死"
        （实测）。超时后抛 `TimeoutError`，
        由 API 层转成 504，前端立刻恢复可交互并提示重试。
        """
        cached = self._cached_room(room_input)
        if cached is not None:
            return cached

        timeout = self._resolve_timeout()
        resolver = getattr(self.api, "room_info", None)

        async def _lookup() -> RoomInfo:
            # ⚠️ 这里必须是**内层协程**，不能写成 `wait_for(self._with_anchor(await ...))`：
            # 那样 `await room_init` 会在 `wait_for` **调用之前**就展开执行，超时形同虚设。
            if resolver is not None:
                return await resolver(room_input)
            # 假 API / 旧实现只有 room_init 时退回单步查询
            return await self._with_anchor(await self.api.room_init(room_input))  # type: ignore[union-attr]

        try:
            room: RoomInfo = await asyncio.wait_for(_lookup(), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(
                f"查询房间 {room_input} 超时（>{timeout:.0f}s）："
                "网络或系统代理不可达，请检查网络后重试"
            ) from exc
        self._remember_room(room)
        return room

    async def lookup_room(self, room_input: int | str) -> dict[str, Any]:
        """把一个房间号（含短号）解析成面板展示用的房间信息。

        不启动采集 —— 右侧栏要在用户**输入房间号时**就能显示
        「XXX 的直播间」，而不是等他点了添加之后。已监控的房间直接
        复用运行时的缓存，避免重复打网络接口。

        `room_input` 也可以是**直播间链接**（`https://live.bilibili.com/8432038?...`）：
        这里的解析是必需的而不只是图方便 —— 否则一个粘进来的链接会被
        `int()` 拒掉，用户只能看到一句"添加失败"。
        """
        if self.api is None:
            raise RuntimeError("PanelService 尚未 start()")

        room_input = parse_room_input(room_input)

        room = await self._resolve_room(room_input)
        return self._room_payload(room, monitored=room.room_id in self._rooms)

    def _remember_room(self, room: RoomInfo) -> None:
        """写入房间缓存，并维持 LRU 顺序与容量上限。"""
        self._room_cache[room.room_id] = room
        self._room_cache.move_to_end(room.room_id)
        while len(self._room_cache) > self._room_cache_cap:
            self._room_cache.popitem(last=False)

    def _cached_room(self, room_input: int | str) -> RoomInfo | None:
        try:
            numeric = int(str(room_input).strip())
        except (TypeError, ValueError):
            return None
        room = self._room_cache.get(numeric)
        if room is None:
            # 短号也要能命中缓存。原实现这里是 O(n) 线性扫全部缓存值，
            # 现在是 O(n) 但只在真实房间号未命中时才走（且缓存有上限）。
            room = next(
                (item for item in self._room_cache.values() if item.short_id == numeric),
                None,
            )
        if room is not None:
            self._room_cache.move_to_end(room.room_id)  # LRU：命中即视为最近使用
        return room

    def _room_payload(self, room: RoomInfo, *, monitored: bool) -> dict[str, Any]:
        return {
            "room_id": room.room_id,
            "short_id": room.short_id,
            "anchor_uid": room.anchor_uid,
            "anchor_name": room.anchor_name,
            "anchor_face": room.anchor_face,
            "live_status": room.live_status,
            "title": f"{room.anchor_name}的直播间" if room.anchor_name else f"房间 {room.room_id}",
            "monitored": monitored,
        }

    async def add_room(self, room_input: int | str) -> RoomRuntime:
        """解析房间号、启动采集，并返回运行时信息。"""
        if self.api is None:
            raise RuntimeError("PanelService 尚未 start()")

        # 上限检查必须在 room_init **之前**：否则已达上限时，一个不存在的房间号
        # 会先打一次网络接口再报"房间不存在"，把真正的原因（已达上限）盖掉。
        if len(self._rooms) >= self.config.general.max_rooms:
            raise ValueError(
                f"已达并发上限 {self.config.general.max_rooms}"
                "（可通过 general.max_rooms 调整）"
            )

        # 用带缓存的解析：用户往往先输入房间号看一眼「XXX 的直播间」再点添加，
        # 那次已经把 room_init + 主播昵称查过一遍了，这里不该再打一次接口。
        # （主播昵称取不到时 `_with_anchor` 会退化成 None，不阻断添加。）
        # 也接受直播间链接：`parse_room_input` 会把链接里的房间号取出来。
        room = await self._resolve_room(parse_room_input(room_input))
        if room.room_id in self._rooms:
            raise ValueError(f"房间 {room.room_id} 已在监控中")
        self._remember_room(room)

        bus = self._build_bus()
        # 与 CLI 一致：默认只采集有用户文字内容的弹幕与 SC
        filters = build_filter(content_only=self.config.general.content_only)
        collector = RoomCollector(
            room.room_id,
            self.config,
            self.credentials,
            self.api,
            bus,
            filters=filters,
            seed_history=True,
            anonymize=self.config.general.anonymize,
        )
        task: asyncio.Task[CollectorStats] = asyncio.create_task(
            collector.run(), name=f"collector-{room.room_id}"
        )
        runtime = RoomRuntime(room=room, request=str(room_input), collector=collector, task=task, bus=bus)
        task.add_done_callback(lambda finished: self._on_room_done(runtime, finished))
        self._rooms[room.room_id] = runtime

        self.hub.publish_control("state", {"room_id": room.room_id, "conn_state": "STARTING"})
        log.info("面板开始监控房间 %s（当前 %d 个）", room.room_id, len(self._rooms))
        return runtime

    def _on_room_done(self, runtime: RoomRuntime, task: asyncio.Task[CollectorStats]) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            runtime.error = f"{type(exception).__name__}: {exception}"
            log.error("房间 %s 采集任务异常结束: %s", runtime.room.room_id, runtime.error)
        self.hub.publish_control(
            "state",
            {
                "room_id": runtime.room.room_id,
                "conn_state": "STOPPED",
                "reason": runtime.error,
            },
        )

    def reconnect_room(self, room_id: int) -> dict[str, Any]:
        """**手动重连**一个房间（需求 2026-10-09）。

        语义（刻意做窄）：
        * 房间不在监控中 → `ValueError`（面板显示 404，不猜用户想干嘛）；
        * 采集任务已经结束 → `ValueError`（重连是"再连一次"，不是"重新开始采集"，
          后者该走 `add_room`）；
        * 只有在 `WAITING_MANUAL`（自动重连已放弃）时才真正受理，其余状态返回
          `accepted: False` + 原因 —— **不报错**，因为"现在不需要重连"是好消息，
          不该在界面上显示成一次失败。

        `accepted` 与 HTTP 状态码分开表达：200 表示"请求被理解了"，
        `accepted` 表示"是否真的排了一次重连"。前端据此给准确反馈。
        """
        runtime = self._rooms.get(room_id)
        if runtime is None:
            raise ValueError(f"房间 {room_id} 不在监控中")
        if runtime.task.done():
            raise ValueError(f"房间 {room_id} 的采集任务已结束，请重新添加房间")

        accepted = runtime.collector.request_reconnect()
        reason = "" if accepted else (
            "当前连接正常，不需要重连"
            if runtime.collector.state.value in ("STREAMING", "AUTHED")
            else "正在自动重连中，请稍候"
        )
        log.info("房间 %s 手动重连：accepted=%s（state=%s）",
                 room_id, accepted, runtime.collector.state.value)
        return {
            "room_id": room_id,
            "accepted": accepted,
            "reason": reason,
            "room": runtime.snapshot(),
        }

    async def remove_room(self, room_id: int) -> bool:
        runtime = self._rooms.pop(room_id, None)
        if runtime is None:
            return False

        runtime.collector.request_stop()
        try:
            await asyncio.wait_for(runtime.task, timeout=20.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001 - 停止失败不应阻断其他房间
            log.warning("停止房间 %s 时出错: %s", room_id, exc)

        # 清掉该房间在 hub 里的内存态（分析窗口 + 广播环形缓冲）。
        # 不清的话，用户每换一个房间就沉淀一份（1200 条弹幕 + 2000 条 JSON payload），
        # 长时间运行只增不减。
        self.hub.drop_room(room_id)

        log.info("面板停止监控房间 %s（剩余 %d 个）", room_id, len(self._rooms))
        return True

    # ------------------------------------------------------------------ #
    def rooms(self) -> list[dict[str, Any]]:
        return [runtime.snapshot() for runtime in self._rooms.values()]

    def anchor_name(self, room_id: int | None) -> str:
        """取主播昵称（**只读缓存，绝不发网络请求**），取不到返回空串。

        用途是导出文件名 `<主播名字>_...`：这是**取名字**，不该为了它去调
        B 站接口（那会给风控多送一次请求，而且导出本来就不依赖网络）。
        所以只认已经在缓存/正在监控的房间；缓存里没有就老老实实返回空串，
        由调用方决定用什么占位。

        `_room_cache` 按真实房间号与短号都可能命中，所以这里复用同一套查找规则。
        """
        if room_id is None:
            return ""
        numeric = int(room_id)
        for runtime in self._rooms.values():
            if runtime.room.room_id == numeric:
                return runtime.room.anchor_name or ""
        cached = self._cached_room(numeric)
        return (cached.anchor_name or "") if cached is not None else ""

    def room_ids(self) -> list[int]:
        return sorted(self._rooms)

    @property
    def room_count(self) -> int:
        return len(self._rooms)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if not self._stopping else "stopping",
            "rooms": len(self._rooms),
            "sinks": list(self.config.sinks.types),
            "keep_raw": self.config.sinks.keep_raw,
            "anonymize": self.config.general.anonymize,
            "content_only": self.config.general.content_only,
            "hub": self.hub.stats,
        }

    def session_meta_for(self, room_id: int) -> SessionMeta | None:
        runtime = self._rooms.get(room_id)
        if runtime is None or runtime.collector._meta is None:
            return None
        return runtime.collector._meta
