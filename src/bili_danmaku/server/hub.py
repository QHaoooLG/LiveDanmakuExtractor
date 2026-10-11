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
from collections import OrderedDict, deque
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

#: 累计发言榜最多记多少个不同的 uid（见 `WebHub._prune_cumulative_uids`）。
#: 实测 230 B/uid ⇒ 20000 个约 4.6 MB。**这是面板里唯一真正无界的结构的上限**。
CUMULATIVE_UID_CAP = 20_000

#: 累计 uid 表的裁剪间隔（秒）。限频是必需的：裁剪要排一次序，
#: 若每条事件都判断"超没超"，超限后每条都会排一次（那是 O(n log n)／条）。
CUMULATIVE_PRUNE_INTERVAL_S = 30.0

#: 保留多少个房间的分析窗口（LRU）。面板同时最多监控 `general.max_rooms`（默认 3）个，
#: 留 8 个是为了"停掉再重新添加"时还能用上旧基线（见 `_windows` 的说明）。
HUB_WINDOW_ROOM_CAP = 8

#: 实时订阅者（WS 客户端）数量上限。
#:
#: 每个订阅者一个 `DEFAULT_QUEUE_SIZE=500` 的有界队列，单条 payload 约 0.7 KB
#: ⇒ 最坏约 1 MB/客户端。256 个 ≈ 256 MB，对"本机面板"这个定位已经绰绰有余
#: （正常人只开一两个标签页）。**没有上限的话它是无界的**。
DEFAULT_MAX_SUBSCRIBERS = 256


class TooManySubscribers(RuntimeError):
    """订阅者数量超过上限（见 `WebHub.subscribe`）。"""


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
    而每条都调分词器是可接受的（有界窗口 + 增量聚合，实测在热路径可承受）。
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
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
    ) -> None:
        self._queue_size = queue_size
        self._buffer_size = buffer_size
        self._slow_threshold = slow_threshold
        #: 订阅者数量上限（见 `subscribe`）
        self.max_subscribers = max(int(max_subscribers), 1)
        self._subscribers: set[Subscriber] = set()
        #: room_id → 最近的 `data` 记录（**dict，不是 JSON 串**）。
        #: 存 dict 是为了让零订阅者时不必编码；
        #: 发给客户端时用 `encode_event()`。
        self._buffers: dict[int, deque[dict[str, Any]]] = {}
        self.published = 0
        #: 自 hub 创建（= 面板启动）以来的累计统计
        self.started_at = time.time()
        self.cumulative_total = 0
        self.cumulative_by_type: dict[str, int] = {}
        self.cumulative_by_room: dict[int, int] = {}
        self.cumulative_by_room_type: dict[int, dict[str, int]] = {}
        #: 「发言榜」的内存累计（2026-10-09 新增，见 `cumulative_top_senders`）。
        #:
        #: 为什么要有：不带 `room_id` 的 `GROUP BY uid` 查询在 1 GB 库上要 2.6~35 秒
        #: （`recv_ts >= ?` 没有可用索引，退化成走 uid 索引 + 每行回表），而它跑在
        #: 事件循环上 ⇒ **整个面板卡死**（实测 `/api/health` 从 21 ms 被拖到 2412 ms）。
        #: 累计口径本来就是"本次运行以来"，hub 手里正好有全量事件 ⇒ 直接计数。
        #: 代价：每个 uid 一条计数 + 一个昵称（实测万级 uid 约几 MB，可接受）。
        self.cumulative_by_uid: dict[int, int] = {}
        self.cumulative_uname: dict[int, str] = {}
        #: 累计发言榜的 uid 上限（见 `_prune_cumulative_uids`）。
        #: 实测 230 B/uid ⇒ 20000 个约 4.6 MB，是有界的常驻开销。
        self.cumulative_uid_cap = CUMULATIVE_UID_CAP
        #: 上次裁剪的时刻（限频：每 `CUMULATIVE_PRUNE_INTERVAL_S` 秒最多裁一次，
        #: 否则每条事件都可能在超限时排一次序）
        self._last_uid_prune = 0.0
        #: 感知分析（V3-b 3A）的有界窗口：room_id → LiveWindow。
        #: 只覆盖**已监控过的房间**（房间移除后保留，重新添加时还能用旧基线）。
        #:
        #: **但有上限**：原实现是"永不删除"，于是"监控过 N 个房间"
        #: 就沉淀 N 份窗口（每份含 1200 条弹幕 + 20 个词表），长期运行只增不减。
        #: 这里改成 LRU，容量 `HUB_WINDOW_ROOM_CAP`（8，远大于默认 max_rooms=3）：
        #: 既能保住"停掉再添加还能用旧基线"，又不会无限沉淀。
        self._windows: dict[int, LiveWindow] = OrderedDict()

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
            self._evict_stale_windows()
        else:
            self._windows.move_to_end(room_id)
        return window

    def _evict_stale_windows(self) -> list[int]:
        """把 `_windows` 压回容量上限，返回被淘汰的房间号。"""
        evicted: list[int] = []
        while len(self._windows) > HUB_WINDOW_ROOM_CAP:
            room_id, _ = self._windows.popitem(last=False)
            evicted.append(room_id)
        if evicted:
            log.debug("分析窗口超出上限，淘汰最久未用的房间: %s", evicted)
        return evicted

    def drop_room(self, room_id: int) -> bool:
        """房间被移除时清掉它的内存态（分析窗口 + 广播环形缓冲）。

        **为什么必须清**：不监控了却还留着 1200 条弹幕原文 + 2000 条 JSON payload，
        用户"换个房间看看"时就一路沉淀下去。返回是否真的清了东西。
        """
        dropped = self._windows.pop(room_id, None) is not None
        if self._buffers.pop(room_id, None) is not None:
            dropped = True
        return dropped

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
        self.cumulative_by_uid = {}
        self.cumulative_uname = {}
        self._last_uid_prune = 0.0
        for window in self._windows.values():
            window.reset()

    def _prune_cumulative_uids(self) -> int:
        """把累计 uid 表**压回上限**，返回淘汰了多少个 uid。

        ## 为什么必须压

        `cumulative_by_uid` / `cumulative_uname` 每个**不同观众**占一条，
        实测 **230 B/uid** ⇒ 100 万 uid 约 **219 MiB**，
        而且面板开得越久、房间越热，它就越大。原注释猜"万级 uid 约几 MB"，
        万级确实只有几 MB，但**高热房间跑一整天是百万级**，差了 100 倍。
        这是整个面板唯一真正**无界**的结构。

        ## 口径：保留 **发言最多的前 N 个 uid**

        面板只用它做"发言 TOP N"（`cumulative_top_senders`，默认 `limit=10`），
        所以给重发言者留位、把长尾淘汰掉，正好符合用途。

        ## 代价（如实记录）

        被淘汰的 uid 若之后再发言，计数会**从头开始**（显示为偏小）。
        这是"内存有界"必须付的代价，而且只影响长尾里那些本来也进不了 TOP 的账号。
        排行榜的头部不会受影响 —— 头部发言多，裁剪时按计数排序必然保得住。
        """
        kept = self.cumulative_uid_cap
        if len(self.cumulative_by_uid) <= kept:
            return 0
        # 按"发言数降序、uid 升序"取前 kept 个，与 `cumulative_top_senders` 同口径
        survivors = sorted(
            self.cumulative_by_uid.items(), key=lambda item: (-item[1], item[0])
        )[:kept]
        removed = len(self.cumulative_by_uid) - kept
        self.cumulative_by_uid = dict(survivors)
        self.cumulative_uname = {
            uid: self.cumulative_uname[uid]
            for uid, _ in survivors
            if uid in self.cumulative_uname
        }
        log.debug("累计发言榜已裁剪：淘汰 %d 个长尾 uid，保留 %d 个", removed, kept)
        return removed

    def cumulative_top_senders(self, limit: int = 10) -> list[dict[str, Any]]:
        """**累计口径**的发言榜（"自本次运行以来"）—— 完全来自内存计数，零查询。

        ## 为什么不用 SQL（2026-10-09 实测，这是"面板卡死"的元凶）

        不带 `room_id` 时 `store.top_senders()` 的 SQL 是
        `WHERE recv_ts >= ? ... GROUP BY uid ORDER BY COUNT(*) DESC`：

        * `recv_ts >= ?` **用不上**任何索引（`idx_event_room_ts*` 的最左列是 `room_id`）；
        * 规划器于是退回 `SEARCH event USING INDEX idx_event_uid` + 每行回表
          + `TEMP B-TREE`（实测：库里 244,036 行、1 GB ⇒ **2.6 s（热）/ 35 s（冷）**；
          带 `room_id` 的同一条只要 **0.2 ms**）。

        而它跑在**事件循环**上（面板里所有 SQLite 调用都是同步的）⇒ 查询期间
        整个面板停摆（实测 `/api/health` 从 21 ms 被拖到 2412 ms），
        再叠加"前端每 3 秒拉一次 + 默认不选房间"⇒ 界面持续卡死。

        ⚠️ **给 `event(recv_ts)` 加单列索引解决不了**：实测规划器仍然选
        `idx_event_uid`（建索引前后 188 ms vs 184 ms，计划一模一样）。
        所以这里换**数据来源**（内存），而不是继续调 SQL。

        `uname` 取"最后一次非空"的昵称（SQL 那边是 `MAX(uname)`；展示口径等价）。
        """
        top = sorted(
            self.cumulative_by_uid.items(),
            key=lambda item: (-item[1], item[0]),
        )[: max(int(limit), 0)]
        return [
            {"uid": uid, "uname": self.cumulative_uname.get(uid), "count": count}
            for uid, count in top
        ]

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
        """登记一个订阅者（WS 客户端）。

        ## 为什么要上限

        每个订阅者持有一个有界队列（`DEFAULT_QUEUE_SIZE=500` 条 payload，
        单条约 0.7 KB ⇒ 每个订阅者最坏约 **1 MB**）。队列本身是有界的，
        但**订阅者数量此前没有任何上限** —— 1000 个客户端就是约 1 GB。

        超限时**抛 `TooManySubscribers`** 而不是静默挤掉一个老客户端：
        调用方（WS 端点）据此回一条明确的错误并关闭连接，
        比"莫名其妙把自己或别人踢下线"好排障得多。
        """
        if len(self._subscribers) >= self.max_subscribers:
            raise TooManySubscribers(
                f"实时订阅已达上限 {self.max_subscribers} 个（每个客户端约占用 1 MB 缓冲）"
            )
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
        subscribers = list(self._subscribers)

        # **没有任何订阅者时不付 `json.dumps` 的代价**。
        #
        # 面板"在采集、但没人打开实时视图"是常见状态（用户关掉标签页、或只看
        # 历史/统计）。`json.dumps` 是这条路径上最贵的一步（每条要序列化一份
        # 完整 `data` 并转义中文）。
        #
        # 但 `model_dump` **仍然要做**，因为环形缓冲必须照常维护：两个读它的人
        # 都与"当前有没有订阅者"无关 ——
        #   * WS 新连接的**历史回填**（先 publish 再连接是常见顺序）；
        #   * 没有 SQLite 时 `/api/events` 的**内存降级路径**（`_memory_events`）。
        # 所以这里只把**编码**推迟：缓冲直接存 `record`（dict），
        # 只有真的要扇出给订阅者时才 `encode_event()`。
        #
        # 形状没有任何变化：`record` 与原来喂给 `json.dumps` 的是同一个对象
        # （仍排除 `raw`），`recent()` 返回 dict、
        # WS 回填改用 `encode_event()` —— 帧内容逐字相同。
        try:
            # **广播不带 `raw`**。
            #
            # `raw` 是原始协议帧，只用于**事后取证**（见 `models.Event.raw`），
            # `web/src` 里没有任何一处读它（已 grep 确认）。而这条记录会被
            # 存进每个房间的环形缓冲（`maxlen=2000`）并扇出给所有订阅者 ——
            # 不排除的话，每个房间白白驻留 2000 份原始帧。
            #
            # 过滤只看顶层字段（`accepts()` 读 `room_id`/`type`/`text`），
            # 所以排除 `raw` 不影响任何订阅过滤语义。
            record = event.to_dict(exclude_raw=True)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 模型应始终可序列化
            log.warning("事件序列化失败: %s", exc)
            return

        buffer = self._buffers.setdefault(event.room_id, deque(maxlen=self._buffer_size))
        buffer.append(record)
        self.published += 1

        # 分析窗口（V3-b 3A）：`add()` 是 O(1) 且有界，可以放在热路径上。
        self.warm_window(event)

        # 累计计数（面板"自启动以来"的统计口径）
        self.cumulative_total += 1
        self.cumulative_by_type[event.type] = self.cumulative_by_type.get(event.type, 0) + 1
        self.cumulative_by_room[event.room_id] = self.cumulative_by_room.get(event.room_id, 0) + 1
        room_counts = self.cumulative_by_room_type.setdefault(event.room_id, {})
        room_counts[event.type] = room_counts.get(event.type, 0) + 1
        # 发言榜的**内存累计**（见 `cumulative_top_senders`）：只认有 uid 的发言，
        # `uid=0` 是"被打码的匿名用户"，与 `store.top_senders(uid != 0)` 口径一致。
        uid = event.uid
        if uid:
            self.cumulative_by_uid[uid] = self.cumulative_by_uid.get(uid, 0) + 1
            name = event.uname
            if name:
                self.cumulative_uname[uid] = name
            # 有界化：超过上限就压回前 N 个（限频，见 `_prune_cumulative_uids`）
            if len(self.cumulative_by_uid) > self.cumulative_uid_cap:
                moment = time.monotonic()
                if moment - self._last_uid_prune >= CUMULATIVE_PRUNE_INTERVAL_S:
                    self._last_uid_prune = moment
                    self._prune_cumulative_uids()

        # 零订阅者：上面该做的都做完了（缓冲已维护），**不必编码**。
        # 这正是零订阅者时省下的那一步。
        if not subscribers:
            return

        payload = self.encode_event(record)
        for subscriber in subscribers:
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
    @staticmethod
    def encode_event(record: dict[str, Any]) -> str:
        """把一条 `data` 记录编成发给前端的 WS 帧。

        `publish()` 与 `recent()` 共用一个出口，保证**实时推送与历史回填的帧
        形状完全一致**（否则新连接与老连接会看到两种 `data`）。
        """
        return json.dumps({"t": "event", "data": record}, ensure_ascii=False)

    def recent(
        self,
        room_id: int | None = None,
        limit: int = 200,
        accept: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """取最近事件的 **`data` 记录**（供新连接回填）。

        返回 dict 而不是 JSON 串：环形缓冲现在**直接持有 dict**，
        于是没有订阅者时热路径不必为"没人读的缓冲"付一次 `json.dumps`
        （见 `publish` 的说明）。需要发给客户端时用 `encode_event()`，
        调用方（WS 回填）本来就要把它写进 socket，所以这里只挪了一次编码的位置，
        并没有增加总工作量。

        `accept` 用于把订阅者的过滤条件也作用到回填上 —— 否则新连上的客户端
        会先收到一批它并不想看的类型。
        """
        if room_id is not None:
            buffer = self._buffers.get(room_id)
            items = list(buffer) if buffer else []
        else:
            merged: list[dict[str, Any]] = []
            for buffer in self._buffers.values():
                merged.extend(buffer)
            items = merged

        if accept is None:
            return items[-limit:]

        picked: list[dict[str, Any]] = []
        for record in reversed(items):
            if not isinstance(record, dict):
                continue
            if accept(record):
                picked.append(record)
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
