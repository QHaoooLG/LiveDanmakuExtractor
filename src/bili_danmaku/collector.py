"""房间采集器：单房间的连接状态机与自动重连。

状态流转（见 TECH_DESIGN §9.1）：

    IDLE → CONNECTING → AUTH_SENDING → AUTHED → STREAMING
                                       ↓
                                  RECONNECTING ──(退避)──→ CONNECTING
                                       ↓ 连续认证失败
                                    PAUSED ──(300s)──→ CONNECTING

设计要点：
    * 每次重连都**重新获取 token**（token 会过期，实测必须如此）；
    * 心跳由独立任务发送，读循环超时是常态（低热度房间消息稀疏），
      因此用「多久没收到心跳回应」判假死，而不是「多久没收到消息」；
    * 读循环只做「解析 → 去重 → 投递」，投递是非阻塞的，绝不因落盘慢而卡住心跳。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .api import BiliApi, BiliApiError
from .client import AuthFailed, ConnectionStale, DanmakuConnection
from .config import Config
from .credentials import Credentials
from .dedupe import DedupeCache
from .filters import FilterSpec
from .models import ConnState, Event, EventType, Medal, RoomInfo, SessionMeta
from .parser import parse_message
from .sinks import Bus
from .utils import anonymize_uid, anonymize_uname, backoff_delay, now

log = logging.getLogger("bili_danmaku.collector")

#: 连续认证失败多少次后暂停该房间
MAX_CONSECUTIVE_AUTH_FAILURES = 5
#: 暂停时长（秒）
PAUSE_SECONDS = 300.0
#: 连接存活多久才算「稳定成功」。低于该值时**不重置退避**，
#: 避免服务端「接受后立刻断开」时退避被反复清零而形成重连风暴。
MIN_STABLE_SECONDS = 5.0
#: 去重缓存 TTL（秒）—— 需覆盖历史补种与实时流的重叠窗口
DEDUPE_TTL = 300.0


@dataclass
class CollectorStats:
    received: int = 0
    parsed: int = 0
    duplicates: int = 0
    bad_messages: int = 0
    filtered: int = 0
    dropped: int = 0
    reconnects: int = 0
    auth_failures: int = 0
    seeded: int = 0
    parse_errors: int = 0
    unknown_protover: int = 0
    bad_json: int = 0
    protocol_errors: int = 0
    last_popularity: int | None = None
    last_message_at: float | None = None
    started_at: float = 0.0
    ended_at: float | None = None
    by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "received": self.received,
            "parsed": self.parsed,
            "duplicates": self.duplicates,
            "bad_messages": self.bad_messages,
            "filtered": self.filtered,
            "dropped": self.dropped,
            "reconnects": self.reconnects,
            "auth_failures": self.auth_failures,
            "seeded": self.seeded,
            "parse_errors": self.parse_errors,
            "unknown_protover": self.unknown_protover,
            "bad_json": self.bad_json,
            "protocol_errors": self.protocol_errors,
            "last_popularity": self.last_popularity,
            "by_type": dict(self.by_type),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }
        if self.started_at and self.ended_at:
            data["duration_s"] = round(self.ended_at - self.started_at, 1)
        return data


class RoomCollector:
    """采集单个直播间，直到被要求停止或到达截止时间。"""

    def __init__(
        self,
        room_input: int | str,
        config: Config,
        credentials: Credentials,
        api: BiliApi,
        bus: Bus,
        *,
        filters: Any = None,
        seed_history: bool = False,
        anonymize: bool = False,
        dedupe_maxsize: int = 100_000,
    ) -> None:
        self.room_input = room_input
        self.config = config
        self.credentials = credentials
        self.api = api
        self.bus = bus
        # 未显式传入规则时，默认只采集「有用户文字内容」的弹幕与 SC；
        # 进房 / 上舰 / 点赞 / 礼物 / 房间数据等不产生用户文字，按需求排除。
        self.filters = filters if filters is not None else FilterSpec(content_only=True)
        self.seed_history = seed_history
        self.anonymize = anonymize

        self.room: RoomInfo | None = None
        self.state = ConnState.IDLE
        self.stats = CollectorStats()
        self.session_id = ""

        self._dedupe = DedupeCache(maxsize=dedupe_maxsize, ttl=DEDUPE_TTL)
        self._stop = asyncio.Event()
        self._deadline: float | None = None
        self._seq = 0
        self._consecutive_auth_failures = 0
        self._meta: SessionMeta | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def run(
        self,
        *,
        duration: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> CollectorStats:
        """开始采集。`duration` 为秒数；`deadline_monotonic` 优先。"""
        if deadline_monotonic is not None:
            self._deadline = deadline_monotonic
        elif duration is not None:
            self._deadline = time.monotonic() + max(duration, 0.0)

        self.stats.started_at = now()
        self.room = await self.api.room_init(self.room_input)
        self.session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{self.room.room_id}"
        self._meta = SessionMeta(
            session_id=self.session_id,
            room_id=self.room.room_id,
            short_id=self.room.short_id,
            anchor_uid=self.room.anchor_uid,
            started_at=self.stats.started_at,
        )

        log.info(
            "开始采集房间 %s（主播 uid=%s，live_status=%s，session=%s）",
            self.room.room_id,
            self.room.anchor_uid,
            self.room.live_status,
            self.session_id,
        )
        if not self.room.is_living:
            log.warning("该房间当前未开播（live_status=%s），仍会保持连接等待开播", self.room.live_status)

        await self.bus.start(self._meta)

        try:
            if self.seed_history:
                await self._seed_from_history()
            await self._connection_loop()
        finally:
            self.stats.ended_at = now()
            await self.bus.stop()
            self._set_state(ConnState.STOPPED)

        return self.stats

    def request_stop(self) -> None:
        self._stop.set()

    async def _sleep_async(self, seconds: float) -> None:
        """可被 request_stop 提前打断的等待。"""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _set_state(self, state: ConnState) -> None:
        if state != self.state:
            log.debug("连接状态: %s → %s", self.state.value, state.value)
        self.state = state

    def _deadline_reached(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _time_left(self) -> float | None:
        if self._deadline is None:
            return None
        return max(self._deadline - time.monotonic(), 0.0)

    # ------------------------------------------------------------------ #
    # 连接循环
    # ------------------------------------------------------------------ #

    async def _connection_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set() and not self._deadline_reached():
            connection: DanmakuConnection | None = None
            try:
                self._set_state(ConnState.CONNECTING)
                bundle = await self.api.acquire_token(self.room.room_id)  # type: ignore[union-attr]
                if self._meta is not None:
                    self._meta.token_source = bundle.source

                connection = DanmakuConnection(
                    bundle, self.room, self.credentials, self.config  # type: ignore[arg-type]
                )
                await connection.open()

                self._set_state(ConnState.AUTH_SENDING)
                await connection.authenticate()
                self._set_state(ConnState.AUTHED)

                self._consecutive_auth_failures = 0

                await connection.send_heartbeat()
                self._set_state(ConnState.STREAMING)
                connected_at = asyncio.get_running_loop().time()
                log.info(
                    "已连接房间 %s（token 来源=%s，protover=%s，节点=%s）",
                    self.room.room_id,  # type: ignore[union-attr]
                    bundle.source,
                    connection.protover,
                    connection.uri,
                )

                await self._pump(connection)

                # 只有稳定存活过才算真正成功，否则保留退避进度
                if asyncio.get_running_loop().time() - connected_at >= MIN_STABLE_SECONDS:
                    attempt = 0

            except asyncio.CancelledError:
                raise
            except AuthFailed as exc:
                self._consecutive_auth_failures += 1
                self.stats.auth_failures += 1
                log.error(
                    "认证失败(%d/%d): %s",
                    self._consecutive_auth_failures,
                    MAX_CONSECUTIVE_AUTH_FAILURES,
                    exc,
                )
                if self._consecutive_auth_failures >= MAX_CONSECUTIVE_AUTH_FAILURES:
                    self._set_state(ConnState.PAUSED)
                    log.error("连续认证失败，暂停该房间 %.0f 秒", PAUSE_SECONDS)
                    await self._sleep_async(PAUSE_SECONDS)
                    self._consecutive_auth_failures = 0
                    continue
            except (BiliApiError, OSError, asyncio.TimeoutError) as exc:
                log.warning("连接失败: %s: %s，准备重连", type(exc).__name__, exc)
            except Exception as exc:  # noqa: BLE001 - 任何异常都要走重连而不是让采集整体崩掉
                log.warning("连接异常: %s: %s，准备重连", type(exc).__name__, exc)
            finally:
                if connection is not None:
                    self._sync_connection_stats(connection)
                    await connection.close()

            if self._stop.is_set() or self._deadline_reached():
                break

            delay = backoff_delay(attempt, cap=self.config.client.reconnect_max_delay)
            attempt += 1
            self.stats.reconnects += 1
            self._set_state(ConnState.RECONNECTING)
            log.info("%.1f 秒后重连（第 %d 次）", delay, attempt)
            await self._sleep_async(delay)

    async def _pump(self, connection: DanmakuConnection) -> None:
        """读循环：拉取消息并交给处理管道。

        读超时由 `DanmakuConnection.messages()` 内部消化，这里只需要处理
        「心跳假死」与「迭代结束」两种终止条件。
        """
        heartbeat = asyncio.create_task(self._heartbeat_loop(connection), name="heartbeat")
        stream = connection.messages(
            should_stop=lambda: self._stop.is_set() or self._deadline_reached()
        )

        try:
            while True:
                try:
                    message = await stream.__anext__()
                except ConnectionStale as exc:
                    log.warning("连接假死（%s），主动重连", exc)
                    return
                except StopAsyncIteration:
                    return

                if connection.stats.last_popularity is not None:
                    self.stats.last_popularity = connection.stats.last_popularity
                self._handle_message(message)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await stream.aclose()

    async def _heartbeat_loop(self, connection: DanmakuConnection) -> None:
        interval = self.config.client.heartbeat_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await connection.send_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 发送失败交给读循环的超时判定
                log.warning("心跳发送失败: %s: %s", type(exc).__name__, exc)
                return

    def _sync_connection_stats(self, connection: DanmakuConnection) -> None:
        self.stats.unknown_protover = connection.stats.unknown_protover
        self.stats.bad_json = connection.stats.bad_json
        self.stats.protocol_errors = connection.stats.protocol_errors
        if connection.stats.last_popularity is not None:
            self.stats.last_popularity = connection.stats.last_popularity

    # ------------------------------------------------------------------ #
    # 消息处理管道
    # ------------------------------------------------------------------ #

    def _handle_message(self, message: dict[str, Any]) -> None:
        self.stats.received += 1
        try:
            event = parse_message(
                message,
                self.room,  # type: ignore[arg-type]
                now(),
                keep_raw=self.config.sinks.keep_raw,
            )
        except Exception as exc:  # noqa: BLE001 - 解析层已兜底，这里是双保险
            self.stats.parse_errors += 1
            log.warning("解析异常: %s: %s", type(exc).__name__, exc)
            return

        if event is None:
            self.stats.bad_messages += 1
            return

        event.session_id = self.session_id
        self._seq += 1
        event.seq = self._seq

        self._dispatch(event)

    def _dispatch(self, event: Event) -> None:
        if self.filters is not None and not self.filters.match(event):
            self.stats.filtered += 1
            return

        if self._dedupe.check_and_add(event.dedupe_key()):
            self.stats.duplicates += 1
            return

        if self.anonymize:
            self._apply_anonymize(event)

        if not self.bus.publish(event):
            self.stats.dropped += 1

        self.stats.parsed += 1
        self.stats.last_message_at = event.recv_ts
        self.stats.by_type[event.type] = self.stats.by_type.get(event.type, 0) + 1

    def _apply_anonymize(self, event: Event) -> None:
        """脱敏：uid 转加盐哈希放入 extra，昵称哈希化。

        `Event.uid` 保持为 int|None，哈希值另存 `extra.uid_hash`，
        避免破坏数据模型的类型约束。
        """
        salt = self.session_id or "bili-danmaku"
        uid_hash = anonymize_uid(event.uid, salt)
        if uid_hash is not None:
            event.extra["uid_hash"] = uid_hash
        event.uid = None
        if event.uname:
            event.uname = anonymize_uname(event.uname, salt)

    # ------------------------------------------------------------------ #
    # 历史补种
    # ------------------------------------------------------------------ #

    async def _seed_from_history(self) -> None:
        """抓取最近历史弹幕并写入 sink，用于启动即有内容 / 离线夹具。

        注意：该接口的字段结构尚未用真实样本验证（实测匿名返回 0 条），
        因此这里全部走 `.get()` 容错，取不到的字段留空而不是编造。
        """
        assert self.room is not None
        try:
            records = await self.api.history(self.room.room_id)
        except BiliApiError as exc:
            log.warning("历史补种失败: %s", exc)
            return

        if not records:
            log.info("历史补种: 没有可用的历史弹幕（接口返回 0 条）")
            return

        seeded = 0
        for record in reversed(records):  # 接口按时间倒序，反转成正序
            event = self._history_to_event(record)
            if event is None:
                continue
            event.session_id = self.session_id
            self._seq += 1
            event.seq = self._seq
            before = self.stats.duplicates
            self._dispatch(event)
            if self.stats.duplicates == before:
                seeded += 1

        self.stats.seeded = seeded
        log.info("历史补种: 写入 %d 条（原始 %d 条）", seeded, len(records))

    def _history_to_event(self, record: dict[str, Any]) -> Event | None:
        if not isinstance(record, dict):
            return None
        text = record.get("text")
        if not text:
            return None

        server_ts = self._parse_timeline(record.get("timeline"))
        medal = None
        medal_raw = record.get("medal")
        if isinstance(medal_raw, list) and len(medal_raw) >= 2:
            medal = Medal(
                level=medal_raw[0] if isinstance(medal_raw[0], int) else None,
                name=medal_raw[1] if isinstance(medal_raw[1], str) else None,
            )

        user_level = None
        level_raw = record.get("user_level")
        if isinstance(level_raw, list) and level_raw and isinstance(level_raw[0], int):
            user_level = level_raw[0]

        return Event(
            room_id=self.room.room_id if self.room else 0,
            type=EventType.DANMAKU.value,
            cmd="DANMU_MSG",
            source="history",
            recv_ts=now(),
            server_ts=server_ts,
            uid=record.get("uid") if isinstance(record.get("uid"), int) else None,
            uname=record.get("nickname") or None,
            text=str(text),
            user_level=user_level,
            medal=medal,
            is_admin=bool(record.get("isadmin")),
            field_source="history_api",
            extra={"history": True},
            raw=record if self.config.sinks.keep_raw else None,
        )

    @staticmethod
    def _parse_timeline(value: Any) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return int(time.mktime(time.strptime(value, fmt)))
            except ValueError:
                continue
        return None
