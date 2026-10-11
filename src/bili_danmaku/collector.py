"""房间采集器：单房间的连接状态机与自动重连。

状态流转：

    IDLE → CONNECTING → AUTH_SENDING → AUTHED → STREAMING
                                       ↓
                                  RECONNECTING ──(退避)──→ CONNECTING
                                       ↓ 连续失败达上限
                              WAITING_MANUAL ──(用户点「手动重连」)──→ CONNECTING

设计要点：
    * 每次重连都**重新获取 token**（token 会过期，实测必须如此）；
    * 心跳由独立任务发送，读循环超时是常态（低热度房间消息稀疏），
      因此用「多久没收到心跳回应」判假死，而不是「多久没收到消息」；
    * 读循环只做「解析 → 去重 → 投递」，投递是非阻塞的，绝不因落盘慢而卡住心跳；
    * **自动重连有次数上限**（需求 2026-10-09）：连续失败到
      `MAX_RECONNECT_ATTEMPTS` 之后不再自己重试，进入 `WAITING_MANUAL`
      等用户手动点一下 —— 无限重连会对已经出问题的服务端/凭据一直打请求，
      而用户只看到一个永远转不完的"重连中"。
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
from .parser import parse_message, serialize_raw
from .sinks import Bus
from .utils import anonymize_uid, anonymize_uname, backoff_delay, now

log = logging.getLogger("bili_danmaku.collector")

#: **自动重连尝试上限**（需求 2026-10-09："自动重连尝试 5 次后暂时放弃自动重连，
#: 改为等待用户使用手动重连功能"）。
#:
#: 计数口径是"**连续失败**"：拿到一次稳定连接（`MIN_STABLE_SECONDS` 以上）就清零。
#: 否则一个跑了一整晚、中间偶发断过 5 次的房间会被判成"反复失败"而停下来 ——
#: 那不是用户要的（B 站自己会主动断连，偶发重连是正常的）。
MAX_RECONNECT_ATTEMPTS = 5
#: 连接存活多久才算「稳定成功」。低于该值时**不重置退避与重连计数**，
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
    #: 上一次连接失败的原因（**给人看的一句话**）。
    #:
    #: 为什么不直接读日志：面板只能拿到快照，而原来这里连日志都丢了原因 ——
    #: 异常分支写的是 `log.warning("连接失败: %s: %s", type(exc).__name__, exc)`，
    #: 而 `AuthFailed` 之类异常自己是**空消息**，于是 `%s` 打出来是空的，
    #: 只剩一个类型名。用户在面板上永远只看到"重连中"，不知道要重新登录。
    last_error: str = ""
    #: 上次失败的归类：`auth` / `network` / `protocol` / `unknown`（空 = 还没失败过）。
    #: 面板据此决定是"请重新登录"还是"网络问题，请稍候"。
    last_error_kind: str = ""
    #: 用户点过几次「手动重连」（需求 2026-10-09）。给人看的：面板可以显示
    #: "已手动重连 N 次"，用户据此判断"点了到底有没有用"。
    manual_reconnects: int = 0

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
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "manual_reconnects": self.manual_reconnects,
        }
        if self.started_at and self.ended_at:
            data["duration_s"] = round(self.ended_at - self.started_at, 1)
        return data


#: 认证被拒绝时的**错误码 → 人话**。
#:
#: `-101` 是"未登录/登录失效"（实测结论），也是最常见的一种 ——
#: 采集中凭据过期就长这样。不翻译的话面板只能显示 `code=-101`，
#: 用户不知道该去重新扫码（需求："凭据失效要看得见、而且要能照做"）。
AUTH_CODE_HINTS: dict[int, str] = {
    -101: "登录已失效，请重新扫码登录",
    -1: "服务端未返回认证回应（可能是 token 失效或房间不可用）",
}


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
        max_reconnect_attempts: int = MAX_RECONNECT_ATTEMPTS,
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
        #: 自动重连尝试上限（连续失败口径）。默认就是模块常量；
        #: 允许注入是为了让用例能用一个小值快速走到"放弃自动重连"那条分支
        #: （否则每个用例都要等 5 轮退避）。
        self.max_reconnect_attempts = max(int(max_reconnect_attempts), 1)

        self.room: RoomInfo | None = None
        self.state = ConnState.IDLE
        self.stats = CollectorStats()
        self.session_id = ""

        self._dedupe = DedupeCache(maxsize=dedupe_maxsize, ttl=DEDUPE_TTL)
        self._stop = asyncio.Event()
        #: 用户请求手动重连（`request_reconnect()` 置位，`_wait_manual()` 消费）
        self._reconnect_requested = asyncio.Event()
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

    @property
    def manual_reconnect_needed(self) -> bool:
        """是否**正在等用户手动重连**（面板据此显示那个按钮）。

        刻意做成**从状态推导**而不是另存一个布尔：两个真相源一定会有一天不同步
        （本项目已经吃过几次这个亏），而这里"等人工"与"状态是 WAITING_MANUAL"
        本来就是同一件事。
        """
        return self.state == ConnState.WAITING_MANUAL

    def request_reconnect(self) -> bool:
        """**手动重连**：让已放弃自动重连的房间再试一次（需求 2026-10-09）。

        返回 `False` 表示"这一下不需要"——只有停在 `WAITING_MANUAL` 时才受理。
        为什么不在别的状态也受理：`STREAMING` 时点一下就把好连接掐掉重建，
        是纯粹的倒退；`RECONNECTING` 时它本来就在试，再置一次位只会白跑一轮。
        面板据此给出准确反馈（"已请求重连" / "当前不需要重连"）。

        置位是**事件**而不是标志位：等待方消费掉就自动清空，于是不存在
        "两次点击合并成一次"或"清了之后还残留"的问题。
        """
        if self.state != ConnState.WAITING_MANUAL:
            return False
        self.stats.manual_reconnects += 1
        self._reconnect_requested.set()
        log.info("收到手动重连请求（第 %d 次）", self.stats.manual_reconnects)
        return True

    async def _wait_manual(self, reason: str) -> None:
        """进入 `WAITING_MANUAL` 并等待用户手动重连（**不会自己恢复**）。

        等待期间仍然响应"停止面板"（`request_stop`）与截止时间，否则一个已放弃
        重连的房间会把整个采集任务挂在退出路径上。
        """
        self._set_state(ConnState.WAITING_MANUAL)
        # 置位期间的重连请求要认：用户可能比这条日志更早点了按钮
        log.error(
            "%s；自动重连已尝试 %d 次，暂时放弃 —— 等待手动重连"
            "（面板上点「手动重连」，或 POST /api/rooms/%s/reconnect）",
            reason, self.max_reconnect_attempts, getattr(self.room, "room_id", self.room_input),
        )
        stop_task = asyncio.ensure_future(self._stop.wait())
        reconnect_task = asyncio.ensure_future(self._reconnect_requested.wait())
        try:
            # 谁先到就听谁的；另一个在 finally 里取消
            await asyncio.wait(
                {stop_task, reconnect_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if reconnect_task.done() and not stop_task.done():
                self._reconnect_requested.clear()
                self._set_state(ConnState.RECONNECTING)
                log.info("手动重连：立即重试")
        finally:
            for task in (stop_task, reconnect_task):
                task.cancel()
            await asyncio.gather(stop_task, reconnect_task, return_exceptions=True)

    @staticmethod
    def _limit_reached(streak: int, limit: int = MAX_RECONNECT_ATTEMPTS) -> bool:
        """连续失败是否已达上限。抽出来是为了让"上限口径"只有一处定义。"""
        return streak >= max(int(limit), 1)

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
        """连接 → 采集 → 失败 → 退避重连；连续失败到上限就停下等人。

        计数分两个（`attempt` / `reconnect_streak`）：前者只喂退避算法（成功后清零，
        所以每次断线都从短延迟开始），后者是"连续失败了多少次"（只有**稳定连接过**
        才清零），用来判断"该不该放弃自动重连"。
        """
        attempt = 0
        #: 连续失败次数（没有一次稳定连接就不清零）—— 达上限即转人工
        reconnect_streak = 0
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
                # 连上了就把旧的失败原因清掉：不清的话，一次早已恢复的抖动
                # 会永远挂在面板上（`assess_health` 会一直报"网络不稳定"）。
                self.stats.last_error = ""
                self.stats.last_error_kind = ""

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

                # 只有稳定存活过才算真正成功：**两个计数一起清**。
                # 不稳定就保留进度，否则服务端"接受后立刻断开"会形成重连风暴，
                # 而且"连续失败"永远到不了上限（等于没有上限）。
                if asyncio.get_running_loop().time() - connected_at >= MIN_STABLE_SECONDS:
                    attempt = 0
                    reconnect_streak = 0

            except asyncio.CancelledError:
                raise
            except AuthFailed as exc:
                self._consecutive_auth_failures += 1
                self.stats.auth_failures += 1
                # 把 `code=-101` 这类**翻译成人话**再记下来：面板要据此提示"请重新登录"
                self._remember_auth_error(exc, int(getattr(exc, "code", 0) or 0))
                log.error(
                    "认证失败(%d/%d): %s",
                    self._consecutive_auth_failures,
                    MAX_RECONNECT_ATTEMPTS,
                    self.stats.last_error,
                )
            except (BiliApiError, OSError, asyncio.TimeoutError) as exc:
                self._remember_error("network", exc, fallback="连接失败")
                log.warning("连接失败: %s，准备重连", self.stats.last_error)
            except Exception as exc:  # noqa: BLE001 - 任何异常都要走重连而不是让采集整体崩掉
                self._remember_error("unknown", exc, fallback="连接异常")
                log.warning("连接异常: %s，准备重连", self.stats.last_error)
            finally:
                if connection is not None:
                    self._sync_connection_stats(connection)
                    await connection.close()

            if self._stop.is_set() or self._deadline_reached():
                break

            reconnect_streak += 1
            self.stats.reconnects += 1

            # 连续失败到上限 ⇒ **暂时放弃自动重连**，等用户手动点（需求 2026-10-09）。
            # 放在退避之前：已经决定不自动重连了，就不该再多睡一次退避。
            if self._limit_reached(reconnect_streak, self.max_reconnect_attempts):
                self._set_state(ConnState.WAITING_MANUAL)
                await self._wait_manual(f"连续 {reconnect_streak} 次连接失败")
                if self._stop.is_set() or self._deadline_reached():
                    break
                # 人工介入过 ⇒ 计数清零，重新给满 5 次自动重连的机会；
                # 退避也归零（否则手动重连还要等 60 秒，用户会以为按钮没反应）。
                reconnect_streak = 0
                attempt = 0
                continue

            delay = backoff_delay(attempt, cap=self.config.client.reconnect_max_delay)
            attempt += 1
            self._set_state(ConnState.RECONNECTING)
            log.info(
                "%.1f 秒后重连（连续第 %d/%d 次）",
                delay, reconnect_streak, self.max_reconnect_attempts,
            )
            await self._sleep_async(delay)

    def _remember_error(self, kind: str, exc: BaseException, *, fallback: str) -> None:
        """记下"上次为什么失败"，供面板显示（**必须是一句能照做的话**）。

        `fallback` + 类型名是兜底：`BiliApiError("")` 与 `TimeoutError()` 的
        `str(exc)` 都是**空串**（实测），只写 `str(exc)` 会让面板与日志都没有线索。
        """
        detail = str(exc).strip()
        name = type(exc).__name__
        self.stats.last_error_kind = kind
        self.stats.last_error = f"{fallback}（{name}）" + (f"：{detail}" if detail else "")

    def _remember_auth_error(self, exc: BaseException, code: int) -> None:
        """认证失败的专用记录：把**错误码翻译成人话**。

        为什么要翻译：`AuthFailed` 的文案是 `认证失败 code=-101` —— 对用户毫无意义，
        而 `-101` 恰恰是"登录失效"。
        不翻译的话，采集中凭据过期时用户看到的只是一串负数，
        不知道该重新扫码登录（这正是"凭据失效要看得见"这条需求要解决的）。
        """
        reason = AUTH_CODE_HINTS.get(int(code), "认证被服务端拒绝")
        self.stats.last_error_kind = "auth"
        self.stats.last_error = f"认证失败：{reason}（code={code}）"

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
            raw=serialize_raw(record) if self.config.sinks.keep_raw else None,
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
