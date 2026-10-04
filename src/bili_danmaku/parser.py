"""cmd → Event 归一化。

字段映射策略（实测驱动）：
    1. **优先**使用 `info[0][15].user` 等结构化对象 —— 实测其值与位置下标一致但更稳定。
    2. 结构化缺失时回退位置下标，并用 `field_source` 标注实际来源，
       以便通过 `field_quality` 指标监控协议漂移、并验证登录态是否改善数据质量。
    3. 所有取值容错：缺字段返回 None 而非抛异常；未知 cmd 走 other 兜底，绝不丢消息。

注意 `info[0][15].extra` 是一个 **JSON 字符串**，必须二次解析。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .models import Event, EventType, FieldSource, Medal, RoomInfo
from .protobuf_lite import decode_base64, extract_strings

log = logging.getLogger("bili_danmaku.parser")


# --------------------------------------------------------------------------- #
# 容错取值
# --------------------------------------------------------------------------- #


def safe_get(obj: Any, *path: Any, default: Any = None) -> Any:
    """沿路径安全取值，任一层缺失/类型不符则返回 default。"""
    current = obj
    for key in path:
        if isinstance(current, dict):
            if key not in current:
                return default
            current = current[key]
        elif isinstance(current, (list, tuple)):
            if not isinstance(key, int) or key < 0 or key >= len(current):
                return default
            current = current[key]
        else:
            return default
    return current


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_int(*values: Any) -> int | None:
    """返回第一个能转成 int 的值。

    与 `a or b` 的区别：**保留 0**。实测 `is_lighted=0` 这类合法零值
    会被 `or` 穿透而误判为"没有值"。
    """
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _first_positive(*values: Any) -> int | None:
    """返回第一个**大于 0** 的值；若都不大于 0，则返回第一个非 None 的值。

    用于礼物数量：实测 COMBO_SEND 的 `gift_num=0` 是未使用字段，
    真实数量在 `total_num` / `batch_combo_num`。
    """
    fallback: int | None = None
    for value in values:
        parsed = _as_int(value)
        if parsed is None:
            continue
        if fallback is None:
            fallback = parsed
        if parsed > 0:
            return parsed
    return fallback


class _FieldPicker:
    """记录字段取自结构化对象还是位置下标。"""

    def __init__(self) -> None:
        self.used_structured = False
        self.used_index = False

    def pick(self, structured: Any, indexed: Any) -> Any:
        """结构化优先；否则退回下标。

        注意 `0` / `""` 视为「无有效值」，不标记来源 —— 否则 guard_level=0
        这类合法零值会让几乎所有事件都被误标为 mixed，使 field_source 指标失效。
        """
        if structured not in (None, "", 0, [], {}):
            self.used_structured = True
            return structured
        if indexed not in (None, "", 0, [], {}):
            self.used_index = True
            return indexed
        return structured if structured is not None else indexed

    @property
    def source(self) -> str | None:
        if self.used_structured and self.used_index:
            return FieldSource.MIXED.value
        if self.used_structured:
            return FieldSource.STRUCTURED.value
        if self.used_index:
            return FieldSource.INDEX.value
        return None


# --------------------------------------------------------------------------- #
# DANMU_MSG
# --------------------------------------------------------------------------- #


def _extract_danmaku_parts(msg: dict[str, Any]) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    info = msg.get("info")
    if not isinstance(info, list) or len(info) < 2:
        return [], {}, {}

    info0 = info[0] if isinstance(info[0], list) else []
    structured = info0[15] if len(info0) > 15 and isinstance(info0[15], dict) else {}

    user = structured.get("user")
    user = user if isinstance(user, dict) else {}

    raw_extra = structured.get("extra")
    extra: dict[str, Any] = {}
    if isinstance(raw_extra, str) and raw_extra.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw_extra)
            extra = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            extra = {}
    elif isinstance(raw_extra, dict):
        extra = raw_extra

    return info, user, extra


def parse_danmaku(
    msg: dict[str, Any],
    cmd: str,
    room: RoomInfo,
    recv_ts: float,
    *,
    seq: int = 0,
    session_id: str = "",
    keep_raw: bool = True,
) -> Event | None:
    info, user, extra = _extract_danmaku_parts(msg)
    if not info:
        return None

    picker = _FieldPicker()

    text = picker.pick(
        _as_str(extra.get("content")),
        _as_str(info[1]) if len(info) > 1 else None,
    )

    msg_id = picker.pick(
        _as_str(extra.get("id_str")),
        _as_str(safe_get(info, 0, 9, "ct")),
    )

    uid = picker.pick(
        _as_int(safe_get(user, "uid")),
        _as_int(safe_get(info, 2, 0)),
    )

    uname = picker.pick(
        _as_str(safe_get(user, "base", "name")),
        _as_str(safe_get(info, 2, 1)),
    )

    user_level = picker.pick(
        _as_int(safe_get(user, "wealth", "level")),
        _as_int(safe_get(info, 16, 0)) or _as_int(safe_get(info, 4, 0)),
    )

    guard_level = picker.pick(
        _as_int(safe_get(user, "guard", "guard_level")),
        _as_int(safe_get(info, 7)),
    )
    if guard_level is None:
        guard_level = 0

    server_ts = _as_int(safe_get(info, 9, "ts"))
    if server_ts is None:
        ts_ms = _as_int(safe_get(info, 0, 4))
        if ts_ms:
            server_ts = ts_ms // 1000

    medal = _parse_medal(user, info, picker)

    is_admin = bool(_as_int(safe_get(info, 2, 2)) or 0)
    is_anchor = bool(room.anchor_uid and uid and uid == room.anchor_uid)

    event_extra: dict[str, Any] = {}
    for key in (
        "mode",
        "font_size",
        "color",
        "dm_type",
        "emoticon_unique",
        "reply_uname",
        "emots",
    ):
        if extra.get(key):
            event_extra[key] = extra[key]
    if event_extra.get("emoticon_unique") or event_extra.get("emots"):
        # 标记「本条含表情」，便于统计与 V2 词云把 [dog] 这类占位符单独处理
        event_extra["has_emoticon"] = True
    if not event_extra.get("color"):
        color = _as_int(safe_get(info, 0, 3))
        if color is not None:
            event_extra["color"] = color
    if not event_extra.get("font_size"):
        size = _as_int(safe_get(info, 0, 2))
        if size is not None:
            event_extra["font_size"] = size

    return Event(
        seq=seq,
        session_id=session_id,
        room_id=room.room_id,
        short_id=room.short_id,
        type=EventType.DANMAKU.value,
        cmd=cmd,
        source="live",
        recv_ts=recv_ts,
        server_ts=server_ts,
        latency_ms=_latency(recv_ts, server_ts),
        msg_id=msg_id,
        uid=uid,
        uname=uname,
        text=text,
        guard_level=guard_level,
        user_level=user_level,
        medal=medal,
        is_anchor=is_anchor,
        is_admin=is_admin,
        field_source=picker.source,
        extra=event_extra,
        raw=msg if keep_raw else None,
    )


def _parse_medal(user: dict[str, Any], info: list[Any], picker: _FieldPicker) -> Medal | None:
    medal_obj = user.get("medal")
    medal_obj = medal_obj if isinstance(medal_obj, dict) else {}
    idx = safe_get(info, 3)
    idx = idx if isinstance(idx, list) else []

    level = picker.pick(_as_int(medal_obj.get("level")), _as_int(safe_get(idx, 0)))
    name = picker.pick(_as_str(medal_obj.get("name")), _as_str(safe_get(idx, 1)))
    if level in (None, 0) and not name:
        return None

    return Medal(
        name=name,
        level=level,
        anchor_uname=_as_str(safe_get(idx, 2)),
        anchor_roomid=_as_int(safe_get(idx, 3)),
        anchor_uid=picker.pick(_as_int(medal_obj.get("ruid")), _as_int(safe_get(idx, 12))),
        is_light=picker.pick(_as_int(medal_obj.get("is_light")), _as_int(safe_get(idx, 11))),
        color=picker.pick(_as_int(medal_obj.get("color")), _as_int(safe_get(idx, 4))),
    )


def _latency(recv_ts: float, server_ts: int | None) -> int | None:
    if not server_ts:
        return None
    return max(int((recv_ts - server_ts) * 1000), 0)


# --------------------------------------------------------------------------- #
# protobuf 载荷（INTERACT_WORD_V2 等）
# --------------------------------------------------------------------------- #

#: 实测 INTERACT_WORD_V2 的 pb 字段 23.3 取值 → 语义（1 与 3 已由实抓文案交叉验证）
PB_INTERACT_MSG_TYPE = {1: "进入直播间", 3: "关注"}


def _pb_field(fields: list[Any] | None, number: int) -> Any:
    for field in fields or []:
        if field.number == number:
            return field
    return None


def _pb_message(fields: list[Any] | None, number: int) -> list[Any] | None:
    field = _pb_field(fields, number)
    return field.value if field is not None and field.is_message else None


def _pb_int(fields: list[Any] | None, number: int) -> int | None:
    field = _pb_field(fields, number)
    return field.value if field is not None and isinstance(field.value, int) else None


def _pb_str(fields: list[Any] | None, number: int) -> str | None:
    field = _pb_field(fields, number)
    if field is None or not isinstance(field.value, bytes):
        return None
    try:
        return field.value.decode("utf-8") or None
    except UnicodeDecodeError:
        return None


def decode_pb_interact(pb_raw: str) -> dict[str, Any]:
    """解析 INTERACT_WORD_V2 的 protobuf 载荷。

    实测结构（字段号 → 含义）：
        2   str    用户名（登录态下应为完整昵称）
        6   int    房间号
        7   int    时间戳（秒）
        8   int    时间戳（毫秒）
        15  int    时间戳（纳秒）
        22.2.1 str 用户名      22.2.2 str 头像 URL
        23.1 str   互动图标    23.2 str 互动文案    23.3 int 互动类型
    """
    fields = decode_base64(pb_raw)
    if not fields:
        return {}

    user_block = _pb_message(fields, 22)
    user_base = _pb_message(user_block, 2) if user_block else None
    action = _pb_message(fields, 23)

    msg_type = _pb_int(action, 3)
    result: dict[str, Any] = {
        "uname": _pb_str(user_base, 1) or _pb_str(fields, 2),
        "face": _pb_str(user_base, 2),
        "msg_type": msg_type,
        "interact_label": _pb_str(action, 2),
        "interact_icon": _pb_str(action, 1),
        "room_id": _pb_int(fields, 6),
        "ts": _pb_int(fields, 7),
        "ts_ms": _pb_int(fields, 8),
        "msg_type_label": PB_INTERACT_MSG_TYPE.get(msg_type or 0),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


# --------------------------------------------------------------------------- #
# data 型命令（礼物 / SC / 上舰 / 互动 / 点赞等）
# --------------------------------------------------------------------------- #

#: 归一化类型 ← cmd 前缀。同一事件多形态（如 INTERACT_WORD_V2）在此登记。
CMD_TYPE_MAP: dict[str, str] = {
    "DANMU_MSG": EventType.DANMAKU.value,
    "SUPER_CHAT_MESSAGE": EventType.SUPER_CHAT.value,
    "SUPER_CHAT_MESSAGE_JPN": EventType.SUPER_CHAT.value,
    "SEND_GIFT": EventType.GIFT.value,
    "COMBO_SEND": EventType.GIFT.value,
    "GUARD_BUY": EventType.GUARD_BUY.value,
    "USER_TOAST_MSG": EventType.GUARD_BUY.value,
    "INTERACT_WORD": EventType.INTERACT.value,
    "INTERACT_WORD_V2": EventType.INTERACT.value,
    "ENTRY_EFFECT": EventType.INTERACT.value,
    "LIKE_INFO_V3_CLICK": EventType.LIKE.value,
    "LIKE_INFO_V3_UPDATE": EventType.LIKE.value,
    "WATCHED_CHANGE": EventType.ROOM_STATS.value,
    "ONLINE_RANK_COUNT": EventType.ROOM_STATS.value,
    "ONLINE_RANK_V3": EventType.ROOM_STATS.value,
    "ROOM_REAL_TIME_MESSAGE_UPDATE": EventType.ROOM_STATS.value,
    "LIVE": EventType.LIVE_STATUS.value,
    "PREPARING": EventType.LIVE_STATUS.value,
    "STOP_LIVE_ROOM": EventType.LIVE_STATUS.value,
    "NOTICE_MSG": EventType.NOTICE.value,
    "SYS_MSG": EventType.NOTICE.value,
    "LOG_IN_NOTICE": EventType.NOTICE.value,
    "DM_INTERACTION": EventType.DM_INTERACTION.value,
}


#: 承载用户信息的嵌套对象键名。实测 LIKE_INFO_V3_CLICK 用 `uinfo`、
#: 礼物（COMBO_SEND）用 `sender_uinfo`，SC 用 `user_info`。
_USER_BLOCK_KEYS = ("uinfo", "sender_uinfo", "user_info", "receiver_uinfo", "sender_info")

#: 承载粉丝牌的嵌套对象键名。实测互动/点赞用 `fans_medal`、礼物用 `medal_info`。
_MEDAL_BLOCK_KEYS = ("fans_medal", "medal_info", "medal")


def _extract_identity(data: dict[str, Any]) -> tuple[int | None, str | None, int | None]:
    """从多种嵌套用户对象里取 (uid, uname, user_level)。

    实测各命令的嵌套层级不一致（点赞用 `uinfo`、礼物用 `sender_uinfo`、SC 用
    `user_info`），且**各字段的来源可能不同** —— 因此每个字段独立回退，
    而不是"某个块有 uid 就整体采用它"。
    """
    uid: int | None = None
    name: str | None = None
    level: int | None = None

    for key in _USER_BLOCK_KEYS:
        block = data.get(key)
        if not isinstance(block, dict) or not block:
            continue
        if uid is None:
            uid = _as_int(block.get("uid"))
        if name is None:
            name = (
                _as_str(safe_get(block, "base", "name"))
                or _as_str(block.get("name"))
                or _as_str(block.get("uname"))
            )
        if level is None:
            level = _as_int(safe_get(block, "wealth", "level"))

    if uid is None:
        uid = _as_int(data.get("uid"))
    if name is None:
        name = _as_str(data.get("uname")) or _as_str(data.get("username"))
    if level is None:
        level = _as_int(data.get("wealth_level"))

    return uid, name, level


def _extract_medal(data: dict[str, Any]) -> Medal | None:
    """从 `fans_medal` / `medal_info` 等嵌套对象里取粉丝牌。"""
    for key in _MEDAL_BLOCK_KEYS:
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        name = _as_str(block.get("medal_name")) or _as_str(block.get("name"))
        level = _as_int(block.get("medal_level")) or _as_int(block.get("level"))
        if not name and not level:
            continue
        return Medal(
            name=name,
            level=level,
            anchor_uname=_as_str(block.get("anchor_uname")),
            anchor_roomid=_first_int(block.get("anchor_roomid")),
            anchor_uid=_first_int(block.get("target_id")),
            is_light=_first_int(block.get("is_lighted"), block.get("is_light")),
            color=_first_int(block.get("medal_color"), block.get("color")),
        )
    return None


def parse_data_cmd(
    msg: dict[str, Any],
    cmd: str,
    base: str,
    room: RoomInfo,
    recv_ts: float,
    *,
    seq: int = 0,
    session_id: str = "",
    keep_raw: bool = True,
) -> Event:
    """处理以 `data` 承载内容的命令族。字段全部容错，取不到就留在 extra 里。"""
    data = msg.get("data")
    data = data if isinstance(data, dict) else {}

    event_type = CMD_TYPE_MAP.get(base, EventType.OTHER.value)

    extra: dict[str, Any] = {"base_cmd": base}

    # INTERACT_WORD_V2 等新命令把数据放在 base64 编码的 protobuf 里（实测 data.pb），
    # 旧版 INTERACT_WORD 的 uid/uname/msg_type 等 JSON 字段已不复存在。
    pb_info: dict[str, Any] = {}
    pb_raw = data.get("pb")
    if isinstance(pb_raw, str) and pb_raw:
        extra["pb_encoded"] = True
        if base in ("INTERACT_WORD", "INTERACT_WORD_V2"):
            pb_info = decode_pb_interact(pb_raw)
            extra.update(pb_info)
        else:
            fields = decode_base64(pb_raw)
            if fields:
                # 未识别命令：至少把可读字符串留下来，原始 pb 仍在 raw 中
                extra["pb_strings"] = extract_strings(fields)[:10]

    uid, uname, nested_level = _extract_identity(data)
    if uid is None:
        uid = _as_int(pb_info.get("uid"))
    if uname is None:
        uname = _as_str(pb_info.get("uname"))

    text = (
        _as_str(data.get("message"))
        or _as_str(data.get("gift_name"))
        or _as_str(data.get("giftName"))
        or _as_str(data.get("like_text"))
        or _as_str(data.get("toast_msg"))
        or _as_str(data.get("notice_msg"))  # LOG_IN_NOTICE 的提示文案
        or _as_str(data.get("copy_writing"))  # ENTRY_EFFECT 的进场文案
    )
    server_ts = (
        _as_int(data.get("timestamp"))
        or _as_int(data.get("time"))
        or _as_int(pb_info.get("ts"))
    )

    if base in ("INTERACT_WORD", "INTERACT_WORD_V2"):
        if not pb_info:
            # 旧版 JSON 形态
            extra["msg_type"] = _as_int(data.get("msg_type"))
            extra["msg_type_label"] = {1: "进入直播间", 2: "关注", 3: "分享"}.get(
                extra["msg_type"] or 0, "未知"
            )
    elif base in ("LIKE_INFO_V3_CLICK", "LIKE_INFO_V3_UPDATE"):
        extra["click_count"] = _as_int(data.get("click_count"))
        extra["like_text"] = _as_str(data.get("like_text"))
    elif base in ("WATCHED_CHANGE", "ONLINE_RANK_COUNT", "ROOM_REAL_TIME_MESSAGE_UPDATE"):
        extra["watched_num"] = _as_int(data.get("num"))
        extra["watched_text"] = _as_str(data.get("text_large")) or _as_str(data.get("text_small"))
        extra["online_count"] = _as_int(data.get("count")) or _as_int(data.get("online_count"))
        extra["fans"] = _as_int(data.get("fans"))
        extra["fans_club"] = _as_int(data.get("fans_club"))
    elif base == "STOP_LIVE_ROOM_LIST":
        # 全局广播，携带数百个房间号；只记数量，避免脏数据淹没统计
        room_list = data.get("room_id_list")
        extra["room_count"] = len(room_list) if isinstance(room_list, list) else None
        extra["global_broadcast"] = True
    elif base == "DM_INTERACTION":
        # 点赞聚合卡片：data.data 又是一个嵌套的 JSON 字符串
        inner = data.get("data")
        payload: dict[str, Any] = {}
        if isinstance(inner, str) and inner.lstrip().startswith("{"):
            try:
                parsed = json.loads(inner)
                payload = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                payload = {}
        elif isinstance(inner, dict):
            payload = inner

        count = _as_int(payload.get("cnt"))
        suffix = _as_str(payload.get("suffix_text"))
        extra["like_count"] = count
        extra["interaction_type"] = _as_int(data.get("type"))
        extra["count_type"] = _as_int(payload.get("count_type"))
        if count is not None and suffix:
            text = f"{count}{suffix}"
        else:
            text = _as_str(payload.get("pv_prefix_text"))
    elif base in ("LIVE", "PREPARING", "STOP_LIVE_ROOM"):
        extra["live_status"] = {
            "LIVE": "开播",
            "PREPARING": "准备中",
            "STOP_LIVE_ROOM": "下播",
        }.get(base, base)

    price: int | None = None
    if event_type == EventType.SUPER_CHAT.value:
        price = _as_int(data.get("price"))
        extra["price_unit"] = "CNY"  # SC 的 price 单位为元
        extra["sc_time"] = _as_int(data.get("time"))
        extra["sc_id"] = _as_int(data.get("id"))
    elif event_type == EventType.GIFT.value:
        # 实测（房间 804456 抓到的 COMBO_SEND）：当前字段是 snake_case，
        # 金额在 combo_total_coin，且用户信息在嵌套的 sender_uinfo 里。
        price = (
            _as_int(data.get("combo_total_coin"))
            or _as_int(data.get("total_coin"))
            or _as_int(data.get("discount_price"))
        )
        extra["price_unit"] = "gold_bean"  # 金瓜子，1000 = 1 元
        extra["gift_name"] = _as_str(data.get("gift_name")) or _as_str(data.get("giftName"))
        extra["gift_id"] = _as_int(data.get("gift_id"))
        extra["num"] = _first_positive(
            data.get("gift_num"), data.get("num"), data.get("total_num"), data.get("batch_combo_num")
        )
        extra["total_num"] = _as_int(data.get("total_num"))
        extra["batch_combo_num"] = _as_int(data.get("batch_combo_num"))
        extra["coin_type"] = _as_str(data.get("coin_type"))
        extra["discount_price"] = _as_int(data.get("discount_price"))
    elif event_type == EventType.GUARD_BUY.value:
        price = _as_int(data.get("price"))
        extra["price_unit"] = "gold_bean"
        extra["guard_level"] = _as_int(data.get("guard_level"))
        extra["num"] = _as_int(data.get("num")) or _as_int(data.get("gift_num"))
        extra["gift_name"] = _as_str(data.get("gift_name"))

    guard_level = _as_int(data.get("guard_level"))
    if guard_level is None:
        guard_level = _as_int(safe_get(data, "fans_medal", "guard_level"))
    if guard_level is None:
        guard_level = _as_int(safe_get(data, "medal_info", "guard_level"))

    medal = _extract_medal(data)

    return Event(
        seq=seq,
        session_id=session_id,
        room_id=room.room_id,
        short_id=room.short_id,
        type=event_type,
        cmd=cmd,
        source="live",
        recv_ts=recv_ts,
        server_ts=server_ts,
        latency_ms=_latency(recv_ts, server_ts),
        msg_id=None,
        uid=uid,
        uname=uname,
        text=text,
        price=price,
        guard_level=guard_level,
        user_level=(
            _as_int(data.get("user_level"))
            or _as_int(data.get("wealth_level"))
            or nested_level
            or _as_int(safe_get(data, "wealth", "level"))
        ),
        medal=medal,
        is_anchor=bool(room.anchor_uid and uid and uid == room.anchor_uid),
        field_source=FieldSource.STRUCTURED.value,
        extra=extra,
        raw=msg if keep_raw else None,
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def parse_message(
    msg: dict[str, Any],
    room: RoomInfo,
    recv_ts: float,
    *,
    seq: int = 0,
    session_id: str = "",
    keep_raw: bool = True,
) -> Event | None:
    """把一条原始消息归一化为 Event。返回 None 表示消息无法解析（应计入 bad_msg）。"""
    if not isinstance(msg, dict):
        return None

    raw_cmd = str(msg.get("cmd") or "")
    if not raw_cmd:
        return None
    # 归一：DANMU_MSG:4:0:2:2:2:0 → DANMU_MSG
    base = raw_cmd.split(":", 1)[0]

    try:
        if base == "DANMU_MSG":
            return parse_danmaku(
                msg, raw_cmd, room, recv_ts, seq=seq, session_id=session_id, keep_raw=keep_raw
            )
        return parse_data_cmd(
            msg,
            raw_cmd,
            base,
            room,
            recv_ts,
            seq=seq,
            session_id=session_id,
            keep_raw=keep_raw,
        )
    except Exception as exc:  # noqa: BLE001 - 单条消息解析失败绝不能影响整体采集
        log.warning("解析消息失败 cmd=%s: %s: %s", raw_cmd, type(exc).__name__, exc)
        return None
