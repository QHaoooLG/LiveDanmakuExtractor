"""WebSocket 广播中枢。

职责：
    * 把采集到的事件同步投递给所有 WS 订阅者（**同步**接口，
      因为它在 sink 工作协程里被调用，绝不能阻塞）；
    * 为每个房间保留一小段最近事件，新连上的客户端立刻有内容可看；
    * 识别慢客户端并剔除，避免一个卡住的浏览器把采集拖慢。

慢客户端的处理是刻意的：直接断开并告知原因，而不是无限缓冲 ——
面板是采集的**消费者**，它不该反向影响数据完整性。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..analysis import MAX_WORDS_PER_MESSAGE, WordBucket, bucket_of
from ..models import Event
from ..text import clean_text, tokenize

log = logging.getLogger("bili_danmaku.server.hub")

#: 单个订阅者的待发队列上限（条）
DEFAULT_QUEUE_SIZE = 500
#: 每房间保留的最近事件数（供新连接回填）
DEFAULT_BUFFER_SIZE = 2000
#: 累计丢弃超过该值即判定为慢客户端
SLOW_CONSUMER_DROPS = 200

#: 感知分析（V3-b 3A）的有界窗口参数。
#: 这些数字是**内存上限**的旋钮：每个值都直接决定面板常驻时的驻留内存，
#: 调大之前先想清楚"面板要开一整天"这件事。
ANALYSIS_WINDOW_MESSAGES = 1200  # 参与分析的最近弹幕条数（按房间）
ANALYSIS_BUCKET_S = 60.0  # 时间片长度（热点突增按片比较）
ANALYSIS_BUCKET_COUNT = 20  # 保留多少个时间片
ANALYSIS_WORDS_PER_BUCKET = 2000  # 每片最多记多少个不同的词
ANALYSIS_MESSAGES_PER_BUCKET = 400  # 每片最多留多少条弹幕原文


@dataclass(eq=False)
class Subscriber:
    """一个 WS 客户端的发送队列。

    `eq=False` 是必需的：dataclass 默认会生成 `__eq__` 并把 `__hash__` 置为 None，
    那样 Subscriber 就无法放进 set（实测直接抛 `unhashable type`）。
    这里需要的是**按身份**区分订阅者，正是 `eq=False` 的语义。
    """

    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_QUEUE_SIZE))
    connected_at: float = field(default_factory=time.time)
    dropped: int = 0
    sent: int = 0
    want_types: frozenset[str] | None = None
    want_rooms: frozenset[int] | None = None
    keyword: str | None = None
    #: 累计丢弃达到该值即判定为慢客户端
    slow_threshold: int = SLOW_CONSUMER_DROPS

    @property
    def is_slow(self) -> bool:
        return self.dropped >= self.slow_threshold


class LiveWindow:
    """按房间增量维护的**有界**分析窗口（V3-b 3A 的数据源）。

    设计约束（改动前先读）：

    1. **`add()` 必须 O(1)** —— 它由 `WebHub.publish()` 调用，而 publish 在 sink
       工作协程里被**同步**调用。任何"顺手重算一遍"都会拖慢采集链路。
    2. **一切有界**。每个房间的弹幕原文、词表、时间片都有硬上限，
       超了按最旧淘汰。面板要开着跑一整天，这里漏一个无界容器就是内存泄漏。
    3. **只做词频那种"能增量维护"的统计**。像"热点突增""疑似账号"这类
       需要跨时间片比较或全局排序的，留给请求时在快照上算
       （`analysis.py`）—— 那些是一天几次的计算，不是每条一次。

    词频的增量维护在这里做，是因为它天然可加（`Counter` 累加），
    而每条都调分词器是可接受的：实测口径见 `TECH_DESIGN` 的 V3-b 一节。
    """

    def __init__(
        self,
        *,
        bucket_s: float = ANALYSIS_BUCKET_S,
        bucket_count: int = ANALYSIS_BUCKET_COUNT,
        window_messages: int = ANALYSIS_WINDOW_MESSAGES,
        words_per_bucket: int = ANALYSIS_WORDS_PER_BUCKET,
        messages_per_bucket: int = ANALYSIS_MESSAGES_PER_BUCKET,
    ) -> None:
        self.bucket_s = max(float(bucket_s), 1.0)
        self.bucket_count = max(int(bucket_count), 2)
        self.window_messages = max(int(window_messages), 1)
        self.words_per_bucket = max(int(words_per_bucket), 10)
        self.messages_per_bucket = max(int(messages_per_bucket), 10)
        #: 当前时间片的起点；0 表示还没收到任何消息
        self.current_start = 0.0
        #: 时间片序列（旧 → 新），每项是 `analysis.WordBucket`
        self._buckets: deque[Any] = deque(maxlen=self.bucket_count)
        #: 用于"疑似刷屏 / 账号画像"的弹幕原文（全窗口，条数有上限）
        self._messages: deque[Any] = deque(maxlen=self.window_messages)

    # ------------------------------------------------------------------ #
    def _rotate(self, recv_ts: float) -> None:
        """把窗口推进到 `recv_ts` 所在的时间片。

        跨片时新建一个空片；如果窗口已经老化到"当前片比保留范围还新"，
        直接整体重置 —— 中间的空档没有必要补空片（补了反而让基线失真：
        一个空片会把所有词的基线拉低，制造假突增）。
        """
        start = bucket_of(recv_ts, size_s=self.bucket_s)
        if self.current_start == 0.0:
            self.current_start = start
            self._buckets.append(WordBucket(start=start))
            return
        if start == self.current_start:
            return
        if start - self.current_start > self.bucket_s * (self.bucket_count - 1):
            # 空档太大（面板卡了很久 / 房间刚恢复）：从零开始，别拿旧数据当基线
            self._buckets.clear()
            self._messages.clear()
        self.current_start = start
        self._buckets.append(WordBucket(start=start))

    def _current(self) -> Any:
        if not self._buckets or self._buckets[-1].start != self.current_start:
            self._buckets.append(WordBucket(start=self.current_start))
        return self._buckets[-1]

    def add(self, event: Any) -> None:
        """把一条事件纳入窗口。**O(1)**，不抛异常。"""
        try:
            recv_ts = float(getattr(event, "recv_ts", 0.0) or 0.0)
            if recv_ts <= 0:
                recv_ts = time.time()
            event_type = str(getattr(event, "type", "") or "")
            text = getattr(event, "text", None)

            self._rotate(recv_ts)
            cleaned = clean_text(text or "")
            if not cleaned:
                return

            # 刷屏检测与账号画像都只看正文，所以"有内容"才留
            self._messages.append(
                {
                    "recv_ts": recv_ts,
                    "uid": getattr(event, "uid", None),
                    "uname": getattr(event, "uname", None),
                    "text": cleaned,
                    "type": event_type or "danmaku",
                }
            )

            if event_type != "danmaku":
                return
            bucket = self._current()
            counts = bucket.counts
            uid = getattr(event, "uid", None)
            for word in tokenize(cleaned, tokenizer="auto").words[:MAX_WORDS_PER_MESSAGE]:
                counts[word] += 1
                # 顺带记"谁说过这个词"（有界，见 WordBucket.add_user）——
                # 词频榜要按**人数**排序才能不被单人刷屏带偏，而这个数
                # 只有在这里（逐条）才拿得到；事后从计数里推不出来。
                bucket.add_user(word, uid)
            # 词表上限：超了淘汰"当前最少"的词。这是**近似**，但对"现在最热的词"
            # 这个问题足够 —— 冷词被淘汰不影响热词计数，而精确去重需要额外索引。
            if len(counts) > self.words_per_bucket:
                overflow = len(counts) - self.words_per_bucket
                for word, _ in sorted(counts.items(), key=lambda kv: kv[1])[:overflow]:
                    counts.pop(word, None)
                    bucket.users.pop(word, None)  # 词被淘汰，它的发言人也一起丢
        except Exception as exc:  # noqa: BLE001 - 分析绝不允许影响采集
            log.debug("LiveWindow.add 忽略异常: %s", exc)

    # ------------------------------------------------------------------ #
    @property
    def buckets(self) -> list[Any]:
        """时间片快照（旧 → 新）。"""
        return list(self._buckets)

    @property
    def messages(self) -> list[dict[str, Any]]:
        """窗口内弹幕快照（旧 → 新）。"""
        return list(self._messages)

    @property
    def size(self) -> int:
        return len(self._messages)

    def reset(self) -> None:
        self.current_start = 0.0
        self._buckets.clear()
        self._messages.clear()


class WebHub:
    """事件广播 + 最近事件环形缓冲 + **自启动以来的累计计数**。

    累计计数是面板"统计默认看全程"的数据源（`/api/stats?window=0`）：
    数据库里的行包含历史补种与往次运行的数据，而面板要的是
    「这个面板从启动到现在一共收了多少」，所以必须自己数。
    """

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        slow_threshold: int = SLOW_CONSUMER_DROPS,
    ) -> None:
        self._queue_size = queue_size
        self._buffer_size = buffer_size
        self._slow_threshold = slow_threshold
        self._subscribers: set[Subscriber] = set()
        self._buffers: dict[int, deque[str]] = {}
        self.published = 0
        #: 自 hub 创建（= 面板启动）以来的累计统计
        self.started_at = time.time()
        self.cumulative_total = 0
        self.cumulative_by_type: dict[str, int] = {}
        self.cumulative_by_room: dict[int, int] = {}
        self.cumulative_by_room_type: dict[int, dict[str, int]] = {}
        #: 感知分析（V3-b 3A）的有界窗口：room_id → LiveWindow。
        #: 只覆盖**已监控过的房间**（房间移除后保留，重新添加时还能用旧基线）。
        self._windows: dict[int, LiveWindow] = {}

    def window(self, room_id: int) -> LiveWindow:
        """取某个房间的分析窗口（不存在则建一个空的）。

        只按 room_id 取 —— 要"全部房间"的合并视图请用 `window_snapshot()`，
        不要把 `None` 当成一个特殊的房间号塞进来（那会让 `_windows` 里多一个
        语义不明的键）。
        """
        window = self._windows.get(room_id)
        if window is None:
            window = LiveWindow()
            self._windows[room_id] = window
        return window

    def window_snapshot(self, room_id: int | None = None) -> tuple[list[dict[str, Any]], list[Any]]:
        """取分析快照 `(messages, buckets)`；`room_id=None` 时合并全部房间。

        合并多房间时**弹幕按时间归并、时间片按起点合并** —— 两个房间的片起点
        可能不同（各自第一条弹幕的时刻决定），所以不能简单拼接。
        """
        if room_id is not None:
            window = self._windows.get(room_id)
            if window is None:
                return [], []
            return window.messages, window.buckets

        messages: list[dict[str, Any]] = []
        merged: dict[float, WordBucket] = {}
        for window in self._windows.values():
            messages.extend(window.messages)
            for bucket in window.buckets:
                target = merged.get(bucket.start)
                if target is None:
                    target = WordBucket(start=bucket.start)
                    merged[bucket.start] = target
                target.counts.update(bucket.counts)
        messages.sort(key=lambda item: item["recv_ts"])
        # 合并后可能超过单房间窗口上限，按时间裁到同一量级（保持"有界"这条约束）
        if len(messages) > ANALYSIS_WINDOW_MESSAGES:
            messages = messages[-ANALYSIS_WINDOW_MESSAGES:]
        return messages, [merged[key] for key in sorted(merged)]

    def warm_window(self, event: Event) -> None:
        """把事件喂给对应房间的分析窗口（`publish()` 的第一件事）。"""
        self.window(event.room_id).add(event)

    def mark_reset(self) -> None:
        """清零累计计数（保留订阅者与缓冲）。"""
        self.started_at = time.time()
        self.cumulative_total = 0
        self.cumulative_by_type = {}
        self.cumulative_by_room = {}
        self.cumulative_by_room_type = {}
        for window in self._windows.values():
            window.reset()

    @property
    def cumulative(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "total": self.cumulative_total,
            "by_type": dict(sorted(self.cumulative_by_type.items(), key=lambda kv: -kv[1])),
            "by_room": {str(room): count for room, count in sorted(self.cumulative_by_room.items())},
        }

    def by_room_type(self, room_id: int | None = None) -> dict[str, int]:
        """某个房间（或全部房间合并）的累计类型分布。"""
        if room_id is not None:
            return dict(
                sorted(
                    (self.cumulative_by_room_type.get(room_id) or {}).items(),
                    key=lambda kv: -kv[1],
                )
            )
        merged: dict[str, int] = {}
        for counts in self.cumulative_by_room_type.values():
            for key, value in counts.items():
                merged[key] = merged.get(key, 0) + value
        return dict(sorted(merged.items(), key=lambda kv: -kv[1]))

    # ------------------------------------------------------------------ #
    # 订阅管理
    # ------------------------------------------------------------------ #

    def subscribe(
        self,
        *,
        types: set[str] | None = None,
        rooms: set[int] | None = None,
        keyword: str | None = None,
    ) -> Subscriber:
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=self._queue_size),
            want_types=frozenset(types) if types else None,
            want_rooms=frozenset(rooms) if rooms else None,
            keyword=keyword.strip() if keyword and keyword.strip() else None,
            slow_threshold=self._slow_threshold,
        )
        self._subscribers.add(subscriber)
        log.debug("新订阅者，当前 %d 个", len(self._subscribers))
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)
        log.debug("订阅者离开，当前 %d 个", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ------------------------------------------------------------------ #
    # 投递
    # ------------------------------------------------------------------ #

    def accepts(self, subscriber: Subscriber, record: dict[str, Any]) -> bool:
        """判断某条**事件字典**是否符合订阅者的过滤条件。

        `publish()` 与 `recent()` 共用这一处判断 —— 否则历史回填会绕过过滤
        （实测：订阅 `types=danmaku` 仍会收到互动/点赞等其它类型）。
        """
        if subscriber.want_rooms is not None and record.get("room_id") not in subscriber.want_rooms:
            return False
        if subscriber.want_types is not None and record.get("type") not in subscriber.want_types:
            return False
        if subscriber.keyword:
            text = (record.get("text") or "").lower()
            if subscriber.keyword.lower() not in text:
                return False
        return True

    def publish(self, event: Event) -> None:
        """同步投递，绝不阻塞、绝不抛异常。"""
        try:
            record = event.model_dump(mode="json")
            payload = json.dumps({"t": "event", "data": record}, ensure_ascii=False)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 模型应始终可序列化
            log.warning("事件序列化失败: %s", exc)
            return

        buffer = self._buffers.setdefault(event.room_id, deque(maxlen=self._buffer_size))
        buffer.append(payload)
        self.published += 1

        # 分析窗口（V3-b 3A）：`add()` 是 O(1) 且有界，可以放在热路径上。
        self.warm_window(event)

        # 累计计数（面板"自启动以来"的统计口径）
        self.cumulative_total += 1
        self.cumulative_by_type[event.type] = self.cumulative_by_type.get(event.type, 0) + 1
        self.cumulative_by_room[event.room_id] = self.cumulative_by_room.get(event.room_id, 0) + 1
        room_counts = self.cumulative_by_room_type.setdefault(event.room_id, {})
        room_counts[event.type] = room_counts.get(event.type, 0) + 1

        for subscriber in list(self._subscribers):
            if not self.accepts(subscriber, record):
                continue
            try:
                subscriber.queue.put_nowait(payload)
                continue
            except asyncio.QueueFull:
                pass
            # 丢最旧，为新事件腾位置
            try:
                subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            try:
                subscriber.queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover
                pass
            subscriber.dropped += 1

    def publish_control(self, kind: str, data: Any) -> None:
        """广播控制类消息（state / stats / error），不受订阅过滤影响。"""
        payload = json.dumps({"t": kind, "data": data}, ensure_ascii=False, default=str)
        for subscriber in list(self._subscribers):
            try:
                subscriber.queue.put_nowait(payload)
            except asyncio.QueueFull:
                subscriber.dropped += 1

    # ------------------------------------------------------------------ #
    def recent(
        self,
        room_id: int | None = None,
        limit: int = 200,
        accept: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[str]:
        """取最近事件的原始 JSON 串（供新连接回填）。

        `accept` 用于把订阅者的过滤条件也作用到回填上 —— 否则新连上的客户端
        会先收到一批它并不想看的类型。
        """
        if room_id is not None:
            buffer = self._buffers.get(room_id)
            items = list(buffer) if buffer else []
        else:
            merged: list[str] = []
            for buffer in self._buffers.values():
                merged.extend(buffer)
            items = merged

        if accept is None:
            return items[-limit:]

        picked: list[str] = []
        for raw in reversed(items):
            try:
                record = json.loads(raw).get("data") or {}
            except ValueError:
                continue
            if accept(record):
                picked.append(raw)
                if len(picked) >= limit:
                    break
        picked.reverse()
        return picked

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "subscribers": len(self._subscribers),
            "rooms": sorted(self._buffers),
            "dropped": sum(sub.dropped for sub in self._subscribers),
            "cumulative_total": self.cumulative_total,
            "uptime_s": round(time.time() - self.started_at, 1),
        }
