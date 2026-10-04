"""数据模型：事件、房间、连接状态、统计。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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


class Medal(BaseModel):
    name: str | None = None
    level: int | None = None
    anchor_uname: str | None = None
    anchor_roomid: int | None = None
    anchor_uid: int | None = None
    is_light: int | None = None
    color: int | None = None


class Event(BaseModel):
    """一条归一化弹幕事件。"""

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
    extra: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None

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
