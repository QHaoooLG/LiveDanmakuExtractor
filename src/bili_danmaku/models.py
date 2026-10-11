"""数据模型：事件、房间、连接状态、统计。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 转换辅助（`Event.from_dict` / `Medal.from_dict` 用）
#
# 这些正是原来 pydantic **隐式**帮我们做的事（JSON 里的 `"12"` → `12`）。
# 换成 dataclass 之后，把它写在明处。
#
# ## 两种语义，必须分清
#
#   * **字段缺席**（`payload` 里没有这个键）⇒ 用默认值。这是正常情况：
#     历史数据、前端回传的部分 payload 都不带全部字段。
#   * **字段在、但值转换不了**（`"seq": "错的"`）⇒ **抛异常**。
#     那是真的坏数据，静默变成 0 会让一条错行看起来像一条合法弹幕 ——
#     比直接跳过它危险得多（CLI 的 `replay` 就是靠这个异常在报"跳过第 N 行"）。
# --------------------------------------------------------------------------- #


class EventFieldError(ValueError):
    """`Event.from_dict` 遇到"字段在、但值不合法"时抛这个。"""


def _int_or_raise(value: Any, field_name: str) -> int | None:
    """缺席/`None` ⇒ `None`；能转就转；**转不了就抛**。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise EventFieldError(f"{field_name} 期望整数，得到布尔值")
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise EventFieldError(f"{field_name} 期望整数，得到 {value!r}") from exc


def _float_or_raise(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise EventFieldError(f"{field_name} 期望数字，得到布尔值")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise EventFieldError(f"{field_name} 期望数字，得到 {value!r}") from exc


def _str_or_raise(value: Any, field_name: str) -> str | None:
    """字符串字段：数字也接受（`str()` 一下），但 dict/list 视为坏数据。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    raise EventFieldError(f"{field_name} 期望字符串，得到 {type(value).__name__}")


def _as_dict(value: Any) -> dict[str, Any]:
    """`extra` 容错：不是 dict 就当空（它只是补充信息，不值得为此丢整条）。"""
    return dict(value) if isinstance(value, dict) else {}


def _as_bool(value: Any) -> bool:
    """布尔字段容错：只有明确的真值才算真（不做 `bool("false") == True` 那种蠢事）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


class EventType(str, Enum):
    """归一化事件类型。"""

    DANMAKU = "danmaku"
    SUPER_CHAT = "super_chat"
    GIFT = "gift"
    GUARD_BUY = "guard_buy"
    INTERACT = "interact"
    LIKE = "like"
    ROOM_STATS = "room_stats"
    LIVE_STATUS = "live_status"
    NOTICE = "notice"
    DM_INTERACTION = "dm_interaction"
    OTHER = "other"


class ConnState(str, Enum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    AUTH_SENDING = "AUTH_SENDING"
    AUTHED = "AUTHED"
    STREAMING = "STREAMING"
    RECONNECTING = "RECONNECTING"
    PAUSED = "PAUSED"
    #: **自动重连已放弃，等用户手动重连**（需求 2026-10-09）。
    #:
    #: 与 `PAUSED` 的区别：`PAUSED` 是"连续认证失败，等一会儿自己再试"（已不再使用），
    #: 而这个状态**不会自己恢复** —— 必须有人（面板上的「手动重连」按钮，或
    #: `RoomCollector.request_reconnect()`）明确要求一次重试。
    #: 之所以要单独一个状态而不是复用 `PAUSED`：面板要据此**显示一个按钮**，
    #: 而"暂停中，请稍候"与"已放弃，请你点一下"是两句完全不同的话。
    WAITING_MANUAL = "WAITING_MANUAL"
    STOPPED = "STOPPED"


class TokenSource(str, Enum):
    GET_DANMU_INFO = "getDanmuInfo"
    GET_DANMU_INFO_WBI = "getDanmuInfo+wbi"
    GET_CONF = "getConf"


class FieldSource(str, Enum):
    """字段取自何处 —— 用于监控协议漂移与验证登录态是否改善数据质量。"""

    STRUCTURED = "structured"  # info[0][15].user 等结构化对象
    INDEX = "index"  # 位置下标兜底
    MIXED = "mixed"


#: 采集范围：只有产生**用户文字内容**的事件才入库。
#: 进房 / 上舰 / 点赞 / 礼物 / 房间数据等事件不产生用户文字，按需求排除在外。
CONTENT_TYPES = frozenset({EventType.DANMAKU.value, EventType.SUPER_CHAT.value})


@dataclass(slots=True)
class Medal:
    """粉丝牌。

    ⚠️ 与 `Event` 一样是 `slots` dataclass，**不是** pydantic 模型。
    它是每条带牌子弹幕的子对象，用 pydantic 会为它单独付一份模型开销。
    """

    name: str | None = None
    level: int | None = None
    anchor_uname: str | None = None
    anchor_roomid: int | None = None
    anchor_uid: int | None = None
    is_light: int | None = None
    color: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好的 dict（字段名与顺序与改造前 `model_dump(mode="json")` 一致）。"""
        return {
            "name": self.name,
            "level": self.level,
            "anchor_uname": self.anchor_uname,
            "anchor_roomid": self.anchor_roomid,
            "anchor_uid": self.anchor_uid,
            "is_light": self.is_light,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Medal | None":
        """从 dict 还原；给的不是 dict 就返回 `None`（容错，不抛）。

        数字字段同样做显式转换 —— 原来这是 pydantic 隐式做的（`"3"` → `3`）。
        """
        if isinstance(payload, Medal):
            return payload
        if not isinstance(payload, dict):
            return None
        return cls(
            name=_str_or_raise(payload.get("name"), "medal.name"),
            level=_int_or_raise(payload.get("level"), "medal.level"),
            anchor_uname=_str_or_raise(payload.get("anchor_uname"), "medal.anchor_uname"),
            anchor_roomid=_int_or_raise(payload.get("anchor_roomid"), "medal.anchor_roomid"),
            anchor_uid=_int_or_raise(payload.get("anchor_uid"), "medal.anchor_uid"),
            is_light=_int_or_raise(payload.get("is_light"), "medal.is_light"),
            color=_int_or_raise(payload.get("color"), "medal.color"),
        )


@dataclass(slots=True)
class Event:
    """一条归一化弹幕事件。

    ## 为什么是 `slots` dataclass 而不是 pydantic 模型

    `Event` 是**采集热路径**上每秒数千次构造的对象，它要穿过 4 个 sink 队列、
    WS 广播与 hub 环形缓冲 —— 单条大小直接乘上"队列容量 × 房间数"就是常驻内存。

    pydantic 的实例每个都带一份 **832 B 的 `__dict__`**（23 个字段全走它）
    外加 `__pydantic_fields_set__` 等私有槽，实测单条 **1,713 B**；
    换成 `@dataclass(slots=True)` 后字段内联进对象头、**没有 `__dict__`**，
    实测约 **225 B**。

    ## 代价（如实记录）

    * 失去 pydantic 的**自动类型转换与校验**（JSONL 里 `"1"` 不会再被变成 `1`）。
      CLI 的 `convert` 走 `from_dict()`，那里做了显式的容错转换；
      其余构造点（parser / store / collector）本来就是按正确类型传的。
    * 序列化改为显式的 `to_dict()` / `from_dict()`，字段清单要**手工维护**。
      所以改字段必须是有意的 —— 它是对外形状。
    """

    seq: int = 0
    session_id: str = ""
    room_id: int = 0
    short_id: int | None = None

    type: str = EventType.OTHER.value
    cmd: str = ""
    source: str = "live"  # live | history

    recv_ts: float = 0.0
    server_ts: int | None = None
    latency_ms: int | None = None

    msg_id: str | None = None
    uid: int | None = None
    uname: str | None = None
    text: str | None = None
    price: int | None = None
    guard_level: int | None = None
    user_level: int | None = None
    medal: Medal | None = None

    is_anchor: bool = False
    is_admin: bool = False

    field_source: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    #: 原始协议帧，**以 JSON 文本形式保存**（不是解析后的 dict）。
    #:
    #: 为什么要存文本而不是 dict：`json.loads` 之后的一帧是一棵 ~60 个 Python 对象的树
    #: （list/dict/str/int），实测**单条 ~3.0 KB**，而同一份内容的 JSON 文本只有 ~0.9 KB。
    #: 事件对象要经过 4 个 sink 队列 + WS 广播 + hub 环形缓冲，这个差价会被放大好几倍。
    #: 落盘层（sqlite 的 `raw_json` 列、
    #: jsonl 的一行）本来要的就是文本，存文本还能**省掉一次 `json.dumps`**。
    #:
    #: 用途只有一个：**事后取证**（解析器写错/新增字段时可以拿它重新解析）。
    #: 没有任何界面消费它 —— 所以它也**不应该**进入 WS 广播（见 `server/hub.py`）。
    raw: str | None = None

    # ------------------------------------------------------------------ #
    # 序列化（替代 pydantic 的 model_dump / model_validate）
    # ------------------------------------------------------------------ #
    def to_dict(self, *, exclude_raw: bool = False) -> dict[str, Any]:
        """JSON 友好的 dict，**字段名与顺序与改造前 `model_dump(mode="json")` 逐字一致**。

        手工维护（不走 `dataclasses.asdict`）是有意的：
        `asdict()` 会**深拷贝** `extra` / 每一层嵌套，而这里只做一层浅转换；
        而且它递归处理 `medal` 时也要保持形状一致。

        `exclude_raw=True` 用于 WS 广播（见 `server/hub.py`）——
        实测 `json.dumps` 是那条路径上最贵的一步，直接不放进 dict 比放进去再过滤划算。
        """
        payload: dict[str, Any] = {
            "seq": self.seq,
            "session_id": self.session_id,
            "room_id": self.room_id,
            "short_id": self.short_id,
            "type": self.type,
            "cmd": self.cmd,
            "source": self.source,
            "recv_ts": self.recv_ts,
            "server_ts": self.server_ts,
            "latency_ms": self.latency_ms,
            "msg_id": self.msg_id,
            "uid": self.uid,
            "uname": self.uname,
            "text": self.text,
            "price": self.price,
            "guard_level": self.guard_level,
            "user_level": self.user_level,
            "medal": self.medal.to_dict() if self.medal is not None else None,
            "is_anchor": self.is_anchor,
            "is_admin": self.is_admin,
            "field_source": self.field_source,
            "extra": self.extra,
            "raw": None if exclude_raw else self.raw,
        }
        if exclude_raw:
            payload.pop("raw")
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "Event":
        """从 dict 还原一条事件（替代 `model_validate`）。

        **容错边界**（与 pydantic 的默认行为对齐，见上面两个辅助函数的说明）：

        * 未知字段**忽略** —— 旧/新版本互读不会炸；
        * 缺失字段用**默认值** —— 历史数据、部分 payload 都能读；
        * 关键字段"在、但值非法"（例如 `"seq": "错的"`）**抛 `EventFieldError`**
          —— 静默变成 0 会让坏行看起来像合法弹幕，比跳过它更危险；
        * `extra` / `is_anchor` / `is_admin` / `medal` 这几个**补充性**字段容错到底
          （它们缺失或格式怪都不该让整条事件读不出来）。

        数字字段做**显式转换**（JSON 里可能是字符串）—— 这是原来 pydantic
        隐式帮我们做的事，现在写在明处。
        """
        if isinstance(payload, Event):
            return payload
        if not isinstance(payload, dict):
            raise TypeError(f"Event.from_dict 需要 dict，得到 {type(payload).__name__}")

        seq = _int_or_raise(payload.get("seq"), "seq")
        room_id = _int_or_raise(payload.get("room_id"), "room_id")
        recv_ts = _float_or_raise(payload.get("recv_ts"), "recv_ts")
        type_ = _str_or_raise(payload.get("type"), "type")

        return cls(
            seq=seq if seq is not None else 0,
            session_id=_str_or_raise(payload.get("session_id"), "session_id") or "",
            room_id=room_id if room_id is not None else 0,
            short_id=_int_or_raise(payload.get("short_id"), "short_id"),
            type=type_ if type_ is not None else EventType.OTHER.value,
            cmd=_str_or_raise(payload.get("cmd"), "cmd") or "",
            source=_str_or_raise(payload.get("source"), "source") or "live",
            recv_ts=recv_ts if recv_ts is not None else 0.0,
            server_ts=_int_or_raise(payload.get("server_ts"), "server_ts"),
            latency_ms=_int_or_raise(payload.get("latency_ms"), "latency_ms"),
            msg_id=_str_or_raise(payload.get("msg_id"), "msg_id"),
            uid=_int_or_raise(payload.get("uid"), "uid"),
            uname=_str_or_raise(payload.get("uname"), "uname"),
            text=_str_or_raise(payload.get("text"), "text"),
            price=_int_or_raise(payload.get("price"), "price"),
            guard_level=_int_or_raise(payload.get("guard_level"), "guard_level"),
            user_level=_int_or_raise(payload.get("user_level"), "user_level"),
            medal=Medal.from_dict(payload.get("medal")),
            is_anchor=_as_bool(payload.get("is_anchor")),
            is_admin=_as_bool(payload.get("is_admin")),
            field_source=_str_or_raise(payload.get("field_source"), "field_source"),
            extra=_as_dict(payload.get("extra")),
            raw=_str_or_raise(payload.get("raw"), "raw"),
        )

    def dedupe_key(self) -> str:
        """去重键。优先使用实测中全局唯一的 msg_id。"""
        if self.msg_id:
            return f"{self.type}|{self.room_id}|{self.msg_id}"
        import hashlib

        text_hash = hashlib.sha1((self.text or "").encode("utf-8")).hexdigest()[:16]
        if self.server_ts is not None:
            return f"{self.type}|{self.uid}|{self.server_ts}|{text_hash}"
        return f"{self.type}|{self.uid}|{text_hash}"

    def has_content(self) -> bool:
        """是否带有明确内容（文字或表情）。

        实测两类表情都落在 text 上：普通表情在正文里内嵌占位符（如 `哈哈[dog]`），
        并在 `extra.emots` 给出「占位符 → 图片元数据」映射；
        主播自定义表情的 text 就是表情名（如 `大拇哥`）。
        因此 text 非空即可判为有内容；另外保留 `emoticon_unique` / `emots`
        兜底，以防出现「纯表情、text 为空」的形态。
        """
        if (self.text or "").strip():
            return True
        extra = self.extra or {}
        return bool(extra.get("emoticon_unique") or extra.get("emots"))

    def is_content_event(self) -> bool:
        """类型与内容都落在采集范围内（文字弹幕 / SC）。"""
        return self.type in CONTENT_TYPES and self.has_content()


class RoomInfo(BaseModel):
    room_id: int
    short_id: int | None = None
    anchor_uid: int | None = None
    live_status: int = 0  # 0 未开播 / 1 直播中 / 2 轮播
    live_time: int | None = None
    #: 主播昵称 / 头像（可选）：由 `get_anchor_in_room` 补齐，取不到就是 None。
    #: 面板右侧栏用它显示「XXX 的直播间」；采集链路完全不依赖这两个字段。
    anchor_name: str | None = None
    anchor_face: str | None = None

    @property
    def is_living(self) -> bool:
        return self.live_status == 1


class HostInfo(BaseModel):
    host: str
    wss_port: int | None = None
    ws_port: int | None = None
    port: int | None = None

    def url(self, use_wss: bool | None = None) -> str:
        """构造 WS 地址。

        默认按「是否声明了 wss_port」自动选择 —— 实测 B 站节点都带 wss_port(443/2245)，
        而测试用假服务器只有 ws_port，因此这里不做硬编码。
        """
        secure = (self.wss_port is not None) if use_wss is None else use_wss
        if secure:
            return f"wss://{self.host}:{self.wss_port or 443}/sub"
        return f"ws://{self.host}:{self.ws_port or 2244}/sub"


class TokenBundle(BaseModel):
    token: str
    hosts: list[HostInfo]
    source: str = TokenSource.GET_CONF.value
    max_delay: int | None = None
    refresh_rate: int | None = None


class SessionMeta(BaseModel):
    session_id: str
    room_id: int
    short_id: int | None = None
    anchor_uid: int | None = None
    token_source: str | None = None
    started_at: float = 0.0
    note: str | None = None


class SinkStats(BaseModel):
    written: int = 0
    dropped: int = 0
    failed: int = 0
    bytes: int = 0
    ok: bool = True
    error: str | None = None
