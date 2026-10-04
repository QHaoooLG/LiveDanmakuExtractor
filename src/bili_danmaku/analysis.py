"""本地感知分析（V3-b 3A）：把弹幕流变成"看得懂的几行字"。

## 这个模块的边界（**改动前先读**）

* **纯函数 + 有界数据**：输入是"最近 N 条弹幕的快照"，输出是普通 dict。
  没有 IO、没有网络、没有全局状态 —— 所以能完全离线单测（`tests/test_analysis.py`）。
* **不做外发**：本地统计是这个方向能被接受的前提（ROADMAP §3.1）。
  任何"把弹幕发出去"的能力都属于 §3.2 3B（用户自备 key 的 LLM 插件），
  **不要**在本模块里加请求。
* **不放采集热路径**：统计所需的有界聚合由 `server/hub.py` 的 `LiveWindow` 增量维护，
  本模块只在请求时被调用。`hub.publish()` 必须保持 O(1)
  （它在 sink 工作协程里被同步调用，见 `hub.py` docstring）。

## 关于"疑似"的措辞

`spam_groups` / `suspicious_users` 是**启发式**，不是判定。
输出字段一律带 `score` 与 `reasons`，UI 也必须用"疑似/仅供参考"的措辞 ——
把启发式包装成结论会让用户据此误伤真人（ROADMAP §7.3 P-5）。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .text import add_custom_words, clean_text, tokenize

#: 单条弹幕最多参与统计的词数（超长文本不该主导词频）
MAX_WORDS_PER_MESSAGE = 30
#: 词频榜长度
TOP_WORDS = 20
#: 每个词的"说过它的 uid"集合上限。
#:
#: 我们要的是"大概多少人说过"这个量级，不需要精确名单；而弹幕量大时
#: 无界集合会随时间一直涨（这是放在 `LiveWindow.add()` 热路径上的东西）。
#: 到顶后 `users` 的语义变成与 `count` 一起判定的"≥ N 人"，
#: 对"按人数排序"这个用途完全够用。
MAX_USERS_PER_WORD = 50
#: 疑似刷屏：同一文本在窗口内至少出现这么多次才算
SPAM_MIN_COUNT = 4
#: 疑似刷屏：参与判定的文本长度下限（"哈哈"/"6" 这种短句刷屏很常见，别误报成刷屏组）
SPAM_MIN_LENGTH = 4
#: 疑似账号得分达到该值才上报
SUSPICIOUS_MIN_SCORE = 45
#: 疑似账号：至少发过这么多条才纳入评估（发一两条的人不该上榜）
SUSPICIOUS_MIN_MESSAGES = 8
#: 速率类结论所需的最小观测跨度（秒）。见 `_rate_window()`。
MIN_RATE_SPAN_S = 30.0


def _rate_window(stamps: Sequence[float]) -> tuple[float, bool]:
    """速率类指标用的时间窗口，返回 `(窗口秒数, 速率结论是否不可用)`。

    **两类退化都实测踩到过**（都是把正常人报成疑似异常账号）：

    1. **时间戳完全相同**：历史补种（`gethistory`）会把一批弹幕写成同一个
       `recv_ts`，`末条 - 首条 = 0`，`count / max(span, 1)` 算出"600 条/分钟"。
    2. **跨度太短**：房间未开播时那批补种可能落在 1 秒内（实测 10 条 / 0.9 秒
       ⇒ "600 条/分钟"、平均间隔 0.1 秒 ⇒ 直接命中"集中发送"）。
       哪怕时间戳各不相同，**1 秒的样本也不足以推断行为**。

    所以判据是"观测跨度至少 `MIN_RATE_SPAN_S`"，不满足就**放弃速率类结论** ——
    宁可漏报，也不给用户一个假指控（重复率那一项与时间无关，仍然参与）。
    """
    distinct = sorted(set(float(value) for value in stamps))
    if not distinct:
        return 1.0, True
    span = distinct[-1] - distinct[0]
    if span < MIN_RATE_SPAN_S:
        return max(span, 1.0), True
    return span, False


@dataclass
class Message:
    """分析需要的最小事件视图（由 `LiveWindow` 从 `Event` 裁剪而来）。"""

    recv_ts: float
    uid: int | None = None
    uname: str | None = None
    text: str | None = None
    type: str = "danmaku"

    @classmethod
    def from_any(cls, value: Any) -> "Message | None":
        """从 `Event` / dict / tuple 构造；不认识就返回 None（调用方过滤掉）。"""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                recv_ts=float(value.get("recv_ts") or 0.0),
                uid=value.get("uid"),
                uname=value.get("uname"),
                text=value.get("text"),
                type=str(value.get("type") or "danmaku"),
            )
        # Event 模型（pydantic）与其它的对象式输入
        recv_ts = getattr(value, "recv_ts", None)
        if recv_ts is None:
            return None
        return cls(
            recv_ts=float(recv_ts),
            uid=getattr(value, "uid", None),
            uname=getattr(value, "uname", None),
            text=getattr(value, "text", None),
            type=str(getattr(value, "type", "danmaku") or "danmaku"),
        )


@dataclass
class WordBucket:
    """一个时间片里的词频（热点突增要跟历史片比，所以必须分片存）。

    `users` 记录"说过这个词的**不同 uid**"，用于把"一个人刷 20 遍"和
    "20 个人各说一遍"区分开 —— 后者才是真正的热点。
    每个词的 uid 集合**有上限**（`MAX_USERS_PER_WORD`）：我们要的是
    "大概多少人说过"这个量级，不需要精确名单，而弹幕量大时无界集合会一直涨。
    """

    start: float
    counts: Counter[str] = field(default_factory=Counter)
    users: dict[str, set[int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def add_user(self, word: str, uid: int | None) -> None:
        """记一个说过 `word` 的 uid（有界 + 只认正数 uid）。

        `uid` 为 None/0 时**不计入**：uid=0 是"被平台打码的匿名用户"，
        把成千上万个匿名用户并成一个 uid 会让那个词看起来"很多人说"
        （与 `analysis` 里账号画像排除 uid=0 是同一个道理）。
        """
        if not uid:
            return
        bucket = self.users.get(word)
        if bucket is None:
            self.users[word] = {int(uid)}
            return
        if len(bucket) < MAX_USERS_PER_WORD:
            bucket.add(int(uid))


def _as_messages(messages: Iterable[Any]) -> list[Message]:
    out: list[Message] = []
    for raw in messages:
        item = Message.from_any(raw)
        if item is not None and item.recv_ts > 0:
            out.append(item)
    return out


def _words_of(
    text: str | None,
    *,
    tokenizer: str,
    stopwords: Iterable[str] | None = None,
) -> list[str]:
    if not text:
        return []
    cleaned = clean_text(text)
    if not cleaned:
        return []
    return tokenize(
        cleaned, tokenizer=tokenizer, stopwords=stopwords
    ).words[:MAX_WORDS_PER_MESSAGE]


# --------------------------------------------------------------------------- #
# 词频
# --------------------------------------------------------------------------- #


def word_frequency(
    messages: Iterable[Any],
    *,
    top: int = TOP_WORDS,
    tokenizer: str = "auto",
    stopwords: Iterable[str] | None = None,
    custom_words: Iterable[str] | None = None,
) -> dict[str, Any]:
    """窗口内词频榜。

    返回 `{total_words, unique_words, items:[{word,count,share,users}]}`。
    `users` 是说过这个词的**不同 uid 数** —— 一条弹幕重发 20 遍和 20 个人都说，
    在"热点"这件事上含义完全不同，所以两个数都要给。

    `stopwords`：为 None 用 `text.DEFAULT_STOPWORDS`；传集合则**整份替换**
    （这是 `wordcloud --stopwords` 一直以来的语义，保持一致）。
    `custom_words`：提交给 jieba 的用户词典（主播名/房间专名），见
    `text.add_custom_words`。
    """
    if custom_words:
        add_custom_words(custom_words)

    counts: Counter[str] = Counter()
    users: dict[str, set[int]] = defaultdict(set)
    total = 0
    for message in _as_messages(messages):
        if message.type != "danmaku":
            continue
        words = _words_of(message.text, tokenizer=tokenizer, stopwords=stopwords)
        total += len(words)
        for word in words:
            counts[word] += 1
            # 有界：只要"多少人说过"的量级，不需要精确名单（同 WordBucket.add_user）
            bucket = users[word]
            if message.uid and len(bucket) < MAX_USERS_PER_WORD:
                bucket.add(int(message.uid))

    ranked = counts.most_common(max(int(top), 1))
    return {
        "total_words": total,
        "unique_words": len(counts),
        "items": [
            {
                "word": word,
                "count": count,
                "share": round(count / total, 4) if total else 0.0,
                "users": len(users.get(word, ())),
            }
            for word, count in ranked
        ],
    }


def hotspot_words(
    buckets: Sequence[WordBucket],
    *,
    top: int = 10,
    min_count: int = 3,
    ratio: float = 2.5,
) -> dict[str, Any]:
    """热点突增词：最近一片 vs 之前若干片的**篇均速率**。

    为什么用"篇均速率"而不是绝对条数：各片时长可能不同（最后一片常常是半截），
    直接比条数会把"这一片刚好长一点"误判成突增。

    `ratio`：当前速率至少是基线速率的多少倍才算突增。
    `min_count`：当前片至少出现多少次 —— 否则基线是 0 的词全都会变成"无限倍"。
    """
    usable = [bucket for bucket in buckets if bucket.total >= 0]
    if len(usable) < 2:
        return {"items": [], "note": "样本不足（至少需要两个时间片）"}

    current = usable[-1]
    history = usable[:-1]
    if not history:
        return {"items": [], "note": "样本不足"}

    history_rates: dict[str, float] = defaultdict(float)
    for bucket in history:
        for word, count in bucket.counts.items():
            history_rates[word] += count
    history_divisor = max(len(history), 1)
    for word in list(history_rates):
        history_rates[word] /= history_divisor

    items: list[dict[str, Any]] = []
    for word, count in current.counts.items():
        if count < min_count:
            continue
        baseline = history_rates.get(word, 0.0)
        # 基线为 0 的词：给一个"新词"标签，但不编造倍率
        spike = (count / baseline) if baseline > 0 else None
        if spike is not None and spike < ratio:
            continue
        items.append(
            {
                "word": word,
                "count": count,
                "baseline": round(baseline, 2),
                "ratio": round(spike, 2) if spike is not None else None,
                "is_new": baseline <= 0,
            }
        )

    # 新词优先（ratio 为 None），其余按倍率降序
    items.sort(key=lambda item: (item["ratio"] is not None, -(item["ratio"] or 0), -item["count"]))
    return {"items": items[: max(int(top), 1)], "window_buckets": len(usable)}


# --------------------------------------------------------------------------- #
# 疑似刷屏
# --------------------------------------------------------------------------- #


def spam_groups(
    messages: Iterable[Any],
    *,
    min_count: int = SPAM_MIN_COUNT,
    min_length: int = SPAM_MIN_LENGTH,
    top: int = 10,
) -> dict[str, Any]:
    """同一文本被反复发送的分组（疑似刷屏 / 跟风）。

    **刻意不折叠短句**（`min_length`）：`哈哈`、`6`、`？？？` 这类在直播间大量出现
    是正常气氛，把它们全报成"刷屏"只会淹没真正的异常。要短句统计就去看词频榜。
    """
    groups: dict[str, dict[str, Any]] = {}
    for message in _as_messages(messages):
        if message.type != "danmaku":
            continue
        text = clean_text(message.text or "").strip()
        if len(text) < min_length:
            continue
        entry = groups.setdefault(
            text, {"text": text, "count": 0, "uids": set(), "unames": set(), "first_ts": message.recv_ts, "last_ts": message.recv_ts}
        )
        entry["count"] += 1
        entry["first_ts"] = min(entry["first_ts"], message.recv_ts)
        entry["last_ts"] = max(entry["last_ts"], message.recv_ts)
        # `uid is not None`（而不是 `if message.uid`）：uid=0 是"被打码的匿名用户"，
        # 用真值判断会把它当成"没有 uid"而漏掉，`users` 计数就偏小。
        if message.uid is not None:
            entry["uids"].add(int(message.uid))
        if message.uname:
            entry["unames"].add(message.uname)

    items = []
    for entry in groups.values():
        if entry["count"] < min_count:
            continue
        span = max(entry["last_ts"] - entry["first_ts"], 0.0)
        items.append(
            {
                "text": entry["text"][:120],
                "count": entry["count"],
                "users": len(entry["uids"]),
                "span_s": round(span, 1),
                # "同一句话由少数人反复发" = 更可疑；人多则更像跟风
                "single_user": len(entry["uids"]) == 1,
                "sample_users": sorted(entry["unames"])[:3],
            }
        )
    items.sort(key=lambda item: (-item["count"], item["users"]))
    return {"items": items[: max(int(top), 1)]}


# --------------------------------------------------------------------------- #
# 疑似异常账号（启发式）
# --------------------------------------------------------------------------- #


def suspicious_users(
    messages: Iterable[Any],
    *,
    top: int = 10,
    min_messages: int = SUSPICIOUS_MIN_MESSAGES,
    min_score: int = SUSPICIOUS_MIN_SCORE,
) -> dict[str, Any]:
    """按启发式给账号打分（**不是判定**）。

    三个维度，各占一部分分值：

    | 维度 | 含义 | 上限 |
    |------|------|------|
    | 发言密度 | 单位时间条数（消息越密越像机器/节奏号） | 40 |
    | 重复率 | 1 - 去重文本数/总条数 | 40 |
    | 集中度 | 活动时间跨度是否很短（短时间内集中爆发） | 20 |

    总分 ≥ `min_score` 才上报，并且**必须**同时给出 `reasons`，
    让用户能自己判断，而不是只看到一个分数。
    """
    items = _as_messages(messages)
    if not items:
        return {"items": [], "scanned": 0}

    stamps = [message.recv_ts for message in items]
    window, whole_window_degenerate = _rate_window(stamps)

    per_user: dict[int, dict[str, Any]] = {}
    for message in items:
        # `uid is not None and uid != 0`：uid=0 是匿名/打码用户，把它们并成一个
        # "用户 0" 会让那个假账号轻松冲到榜首（那是打码造成的，不是行为异常）。
        if message.type != "danmaku" or not message.uid:
            continue
        entry = per_user.setdefault(
            int(message.uid), {"count": 0, "texts": set(), "uname": message.uname, "stamps": []}
        )
        entry["count"] += 1
        cleaned = clean_text(message.text or "")
        if cleaned:
            entry["texts"].add(cleaned)
        entry["stamps"].append(message.recv_ts)
        if message.uname:
            entry["uname"] = message.uname

    scored: list[dict[str, Any]] = []
    for uid, entry in per_user.items():
        count = entry["count"]
        if count < min_messages:
            continue
        unique = len(entry["texts"])
        # 重复率 = 1 - 有效去重数/总条数。
        # 注意用 `unique - 1` 而不是 `unique`：每条不同的内容本身不算"重复"，
        # 全都不相同时应为 0、全部相同时才为 1 —— 用 `unique` 会得到
        # "10 条完全相同 ⇒ 90%"这种永远够不到 100% 的结果（实测踩到）。
        effective_unique = max(unique - 1, 0)
        repeat_ratio = 1.0 - (effective_unique / count if count else 0.0)
        user_window, degenerate = _rate_window(entry["stamps"])
        degenerate = degenerate or whole_window_degenerate
        rate_per_min = count * 60.0 / user_window

        density_score = min(40.0, rate_per_min / 20.0 * 40.0)  # 20 条/分钟封顶
        repeat_score = min(40.0, repeat_ratio * 40.0)
        # 平均每条间隔 < 2 秒 ⇒ 集中爆发
        burst = user_window / max(count - 1, 1)
        burst_score = 20.0 if (count >= min_messages and burst < 2.0) else 0.0

        # 时间戳退化（历史补种把一批写成同刻）时**放弃速率类结论**：
        # 分不清"密集爆发"与"精度不足"，硬算只会给出假指控。
        # 重复率那一项不受影响 —— 它是文本层面的，与时间精度无关。
        if degenerate:
            density_score = 0.0
            burst_score = 0.0

        score = density_score + repeat_score + burst_score
        if score < min_score:
            continue

        reasons = []
        if density_score >= 20:
            reasons.append(f"发言密度高（约 {rate_per_min:.1f} 条/分钟）")
        if repeat_score >= 20:
            reasons.append(f"重复率高（{repeat_ratio * 100:.0f}% 的发言是重复内容）")
        if burst_score > 0:
            reasons.append(f"短时间内集中发送（平均间隔 {burst:.1f} 秒）")
        if degenerate and repeat_score >= 20:
            reasons.append("（时间戳同刻，已忽略速率类指标）")
        scored.append(
            {
                "uid": uid,
                "uname": entry["uname"],
                "count": count,
                "unique_texts": unique,
                "repeat_ratio": round(repeat_ratio, 3),
                "per_min": round(rate_per_min, 1),
                "rate_available": not degenerate,
                "score": round(score, 1),
                "reasons": reasons,
            }
        )

    scored.sort(key=lambda item: -item["score"])
    return {
        "items": scored[: max(int(top), 1)],
        "scanned": len(per_user),
        "window_s": round(window, 1),
        "rate_available": not whole_window_degenerate,
    }


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def words_from_buckets(
    buckets: Sequence[WordBucket],
    *,
    top: int = TOP_WORDS,
    by: str = "count",
) -> dict[str, Any]:
    """把已维护好的时间片词频合并成词频榜。

    这是 `word_frequency()` 的**省算版本**：词频由 `hub.LiveWindow` 增量维护，
    请求时只需把若干片相加，**不必再对窗口内每条弹幕重跑一遍分词**。

    `users`（"多少人说过这个词"）由各片的 `WordBucket.users` 取**并集**得到 ——
    每个片的集合都是有界的（`MAX_USERS_PER_WORD`），所以并集也是有界的近似值。
    这个数很重要：**一条弹幕刷 20 遍**和**20 个人各说一遍**是完全不同的信号。

    `by`：排序口径。
      * `"count"`（缺省，向后兼容）：按出现次数 —— 会被单人刷屏带偏；
      * `"users"`：按**说过的人数** —— "多少人在聊这个词"，更贴近"热点"的本意。
    面板用 `"users"`，因为需求方反馈"统计的词有一部分不符合日常表达"，
    而按人数排能把"一个人刷出来的词"直接压下去。
    """
    counts: Counter[str] = Counter()
    user_sets: dict[str, set[int]] = defaultdict(set)
    for bucket in buckets:
        counts.update(bucket.counts)
        for word, uids in bucket.users.items():
            if len(user_sets[word]) >= MAX_USERS_PER_WORD:
                continue
            user_sets[word].update(uids)

    total = sum(counts.values())

    if by == "users":
        # 并列时用次数兜底，保证顺序稳定（否则相同人数的词顺序随机，榜单会"抖"）
        ranked = sorted(
            counts.items(),
            key=lambda kv: (-len(user_sets.get(kv[0], ())), -kv[1], kv[0]),
        )[: max(int(top), 1)]
    else:
        ranked = counts.most_common(max(int(top), 1))

    return {
        "total_words": total,
        "unique_words": len(counts),
        "ranked_by": by,
        "items": [
            {
                "word": word,
                "count": count,
                "share": round(count / total, 4) if total else 0.0,
                "users": len(user_sets.get(word, ())),
                "from_buckets": True,
            }
            for word, count in ranked
        ],
    }


def analyze(
    messages: Iterable[Any],
    *,
    buckets: Sequence[WordBucket] | None = None,
    tokenizer: str = "auto",
    top_words: int = TOP_WORDS,
    words: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次算完面板要的全部指标（避免前端多次请求、也避免重复遍历）。

    * `buckets` 缺省时不做热点突增（样本不足会带 `note`）；
    * `words` 可由 `words_from_buckets()` 预先算好并传进来 —— 那样就**跳过**
      这里对每条弹幕的分词（省掉最贵的一步），只算刷屏与账号画像。
    """
    items = _as_messages(messages)
    stamps = [message.recv_ts for message in items]
    span = (max(stamps) - min(stamps)) if stamps else 0.0
    return {
        "messages": len(items),
        "window_s": round(span, 1),
        "words": words if words is not None else word_frequency(items, top=top_words, tokenizer=tokenizer),
        "hotspot": hotspot_words(list(buckets or [])),
        "spam": spam_groups(items),
        "suspicious": suspicious_users(items),
        "note": "本地启发式统计，仅供参考；数据不出本机",
    }


def assess_health(
    rooms: Iterable[Any],
    *,
    reconnect_warn: int = 3,
    reconnect_alert: int = 8,
) -> dict[str, Any]:
    """判断采集是否**需要用户介入**，并给出一句能照做的话。

    需求原话："当凭证失效时，在启动面板用较为明显的信息栏提示用户登录"。
    启动时的登录提示条已经做了；这里补的是**运行中**那一半 ——
    采集中凭据过期/被踢时，用户原来在面板上只看到"重连中"，
    只能自己翻日志（而日志当时还因为异常是空消息而**没写原因**）。

    输入是房间快照的可迭代集合（`PanelService.rooms()` 的元素），
    每个至少要能取到 `conn_state` / `last_error` / `last_error_kind` / `reconnects`。

    判据（按严重程度从高到低）：

    | 级别 | 条件 | 提示语 |
    |------|------|--------|
    | `alert` | 有房间 `last_error_kind == "auth"` | 「登录已失效…请重新扫码登录」 |
    | `warn` | 有房间处于 `PAUSED`（连续认证失败后的暂停） | 「认证反复失败，已暂停…」 |
    | `warn` | 单房间重连次数 ≥ `reconnect_alert` | 「连接反复中断」 |
    | `warn` | 有房间 `last_error_kind == "network"` 且重连 ≥ `reconnect_warn` | 「网络不稳定」 |
    | `ok` | 其余 | 无提示 |

    ⚠️ **不要**看到一次重连就报警：B 站本身会主动断连，偶发一次是正常的。
    只有"反复"或"原因明确是鉴权"才值得占用用户注意力。
    """
    items = [item for item in rooms]
    if not items:
        return {"level": "ok", "title": "", "detail": "", "rooms": []}

    def field(room: Any, name: str, default: Any = None) -> Any:
        if isinstance(room, dict):
            return room.get(name, default)
        return getattr(room, name, default)

    def room_id(room: Any) -> Any:
        return field(room, "room_id")

    auth_rooms = [r for r in items if field(r, "last_error_kind") == "auth"]
    if auth_rooms:
        first = auth_rooms[0]
        # `last_error` 本身已经带"认证失败："前缀（见 collector._remember_auth_error），
        # 这里不再重复一次，否则会读成"认证失败：认证失败：…"
        return {
            "level": "alert",
            "title": "采集的登录已失效",
            "detail": (
                f"房间 {'、'.join(str(room_id(r)) for r in auth_rooms)}："
                f"{field(first, 'last_error', '')}。请重新扫码登录后重启采集。"
            ),
            "rooms": [room_id(r) for r in auth_rooms],
        }

    paused = [r for r in items if str(field(r, "conn_state", "")).upper() == "PAUSED"]
    if paused:
        first = paused[0]
        return {
            "level": "warn",
            "title": "认证反复失败，采集已暂停",
            "detail": (
                f"房间 {'、'.join(str(room_id(r)) for r in paused)} 连续认证失败后暂停恢复中："
                f"{field(first, 'last_error', '')}。若持续如此，请检查登录态。"
            ),
            "rooms": [room_id(r) for r in paused],
        }

    flapping = [
        r for r in items if int(field(r, "reconnects", 0) or 0) >= reconnect_alert
    ]
    if flapping:
        return {
            "level": "warn",
            "title": "连接反复中断",
            "detail": (
                f"房间 {'、'.join(str(room_id(r)) for r in flapping)} 已重连 "
                f"{max(int(field(r, 'reconnects', 0) or 0) for r in flapping)} 次；"
                "常见原因是网络不稳定或房间无人开播。"
            ),
            "rooms": [room_id(r) for r in flapping],
        }

    unstable = [
        r
        for r in items
        if int(field(r, "reconnects", 0) or 0) >= reconnect_warn
        and field(r, "last_error_kind") == "network"
    ]
    if unstable:
        return {
            "level": "warn",
            "title": "网络连接不稳定",
            "detail": f"最近一次失败原因：{field(unstable[0], 'last_error', '')}",
            "rooms": [room_id(r) for r in unstable],
        }

    return {"level": "ok", "title": "", "detail": "", "rooms": []}


def bucket_of(recv_ts: float, *, size_s: float) -> float:
    """把时间戳归到时间片（热点突增按片比较）。"""
    size = max(float(size_s), 1.0)
    return math.floor(float(recv_ts) / size) * size
