"""解析层单测。金样本来自房间 804456 的真实抓取。"""

from __future__ import annotations

import base64
import json

from bili_danmaku.models import EventType, FieldSource, RoomInfo
from bili_danmaku.parser import parse_message, safe_get
from bili_danmaku.utils import is_masked_identity

ROOM = RoomInfo(room_id=804456, short_id=None, anchor_uid=8431064, live_status=1)


def _danmaku(
    *,
    text: str = "测试弹幕",
    uid: int = 12345,
    uname: str = "观众甲",
    user_level: int = 5,
    medal: dict | None = None,
    extra_override: object = None,
    medal_index: object = None,
    guard_level_index: int = 0,
) -> dict:
    """构造一条结构接近实测样本的 DANMU_MSG。"""
    extra: object = (
        json.dumps({"content": text, "id_str": "mid-0001", "color": 16777215, "font_size": 25})
        if extra_override is None
        else extra_override
    )
    user: dict = {"uid": uid, "base": {"name": uname}, "wealth": {"level": user_level}}
    if medal is not None:
        user["medal"] = medal

    info = [
        [
            0, 1, 25, 16777215, 1700000000000, -1164395626, 0, "d750f8e6",
            0, 0, 0, "", 0, "{}", "{}",
            {"extra": extra, "mode": 0, "show_player_type": 0, "user": user},
            {"activity_identity": "", "activity_source": 0, "not_show": 0},
            0,
        ],
        text,
        [uid, uname, 0, 0, 0, 10000, 1, ""],
        medal_index if medal_index is not None else [],
        [11, 0, 6406234, ">50000", 0],
        ["", ""],
        0,
        guard_level_index,
        None,
        {"ct": "33C0E5DC", "ts": 1789707654},
        0, 0, None, None, 0, 390, [27], None,
    ]
    return {"cmd": "DANMU_MSG", "dm_v2": "", "info": info}


# --------------------------------------------------------------------------- #
# 安全取值
# --------------------------------------------------------------------------- #


def test_safe_get_handles_missing_and_bad_types() -> None:
    assert safe_get({"a": {"b": [1, 2]}}, "a", "b", 1) == 2
    assert safe_get({"a": 1}, "a", "b") is None
    assert safe_get({"a": [1]}, "a", 5) is None
    assert safe_get(None, "a") is None
    assert safe_get({"a": [1]}, "a", "not-an-int") is None


# --------------------------------------------------------------------------- #
# 金样本：字段映射必须与实测一致
# --------------------------------------------------------------------------- #


def test_golden_sample_field_mapping(danmu_samples: list[dict]) -> None:
    event = parse_message(danmu_samples[0], ROOM, recv_ts=1789707656.4)
    assert event is not None

    assert event.type == EventType.DANMAKU.value
    assert event.cmd == "DANMU_MSG"
    assert event.room_id == 804456
    assert event.text == "集市run博物馆了"
    assert event.msg_id == "5fd7e66727d03c9b40851feb116aacc58384"

    # 实测该用户 UID 与昵称被平台打码
    assert event.uid == 0
    assert event.uname == "逸***"
    assert is_masked_identity(event.uid, event.uname) is True

    assert event.user_level == 27
    assert event.guard_level == 0
    assert event.server_ts == 1789707654
    assert event.latency_ms == int((1789707656.4 - 1789707654) * 1000)

    assert event.medal is not None
    assert event.medal.level == 15
    assert event.medal.name == "六式"
    assert event.medal.anchor_uid == 8431064
    assert event.medal.anchor_roomid == 804456
    assert event.medal.is_light == 1
    assert event.medal.color == 12478086

    # 结构化对象与位置下标都参与过取值
    assert event.field_source in (FieldSource.STRUCTURED.value, FieldSource.MIXED.value)
    assert event.extra.get("color") == 16777215
    assert event.extra.get("font_size") == 25


def test_golden_sample_keeps_raw(danmu_samples: list[dict]) -> None:
    assert parse_message(danmu_samples[0], ROOM, 0.0, keep_raw=True).raw is not None
    assert parse_message(danmu_samples[0], ROOM, 0.0, keep_raw=False).raw is None


def test_golden_sample_marks_anchor(danmu_samples: list[dict]) -> None:
    """样本 uid=0，主播 uid=8431064，不应被误判为主播本人。"""
    event = parse_message(danmu_samples[0], ROOM, 0.0)
    assert event is not None
    assert event.is_anchor is False


# --------------------------------------------------------------------------- #
# 结构化优先 / 下标兜底
# --------------------------------------------------------------------------- #


def test_structured_object_preferred() -> None:
    event = parse_message(_danmaku(text="结构化文本", user_level=42), ROOM, 0.0)
    assert event is not None
    assert event.text == "结构化文本"
    assert event.user_level == 42
    assert event.field_source == FieldSource.STRUCTURED.value


def test_falls_back_to_index_when_extra_is_broken() -> None:
    """info[0][15].extra 是坏 JSON 时必须退回 info[1]，而不是报错。"""
    event = parse_message(_danmaku(text="下标文本", extra_override="{ 不是合法 json"), ROOM, 0.0)
    assert event is not None
    assert event.text == "下标文本"
    assert event.field_source == FieldSource.MIXED.value


def test_handles_missing_structured_block() -> None:
    """info[0] 长度不足、没有结构化对象时仍应解析出文本。"""
    msg = _danmaku(text="只有下标")
    msg["info"][0] = [0, 1, 25, 16777215, 1700000000000]
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.text == "只有下标"
    assert event.field_source == FieldSource.INDEX.value


# --------------------------------------------------------------------------- #
# 粉丝牌的各种形态
# --------------------------------------------------------------------------- #


def test_medal_from_index_when_structured_absent() -> None:
    msg = _danmaku(
        medal_index=[21, "観測者", "某主播", 422915, 6809855, "", 0, 1, 2, 3, 0, 1, 1850091]
    )
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.medal is not None
    assert event.medal.level == 21
    assert event.medal.name == "観測者"
    assert event.medal.anchor_uid == 1850091
    assert event.medal.is_light == 1


def test_medal_empty_list_yields_none() -> None:
    event = parse_message(_danmaku(medal_index=[]), ROOM, 0.0)
    assert event is not None
    assert event.medal is None


def test_medal_short_list_with_nulls_is_safe() -> None:
    event = parse_message(_danmaku(medal_index=[0, "", "", 0, 0, None]), ROOM, 0.0)
    assert event is not None
    assert event.medal is None


def test_medal_zero_level_is_treated_as_no_medal() -> None:
    event = parse_message(_danmaku(medal={"level": 0, "name": ""}), ROOM, 0.0)
    assert event is not None
    assert event.medal is None


# --------------------------------------------------------------------------- #
# 守卫与兜底
# --------------------------------------------------------------------------- #


def test_malformed_messages_return_none() -> None:
    assert parse_message({"cmd": "DANMU_MSG"}, ROOM, 0.0) is None  # 无 info
    assert parse_message({"cmd": "DANMU_MSG", "info": []}, ROOM, 0.0) is None
    assert parse_message({"info": []}, ROOM, 0.0) is None  # 无 cmd
    assert parse_message("not-a-dict", ROOM, 0.0) is None  # type: ignore[arg-type]


def test_unknown_cmd_falls_back_to_other_and_keeps_raw() -> None:
    msg = {"cmd": "SOME_BRAND_NEW_CMD", "data": {"foo": 1}}
    event = parse_message(msg, ROOM, 123.0)
    assert event is not None
    assert event.type == EventType.OTHER.value
    assert event.cmd == "SOME_BRAND_NEW_CMD"
    assert event.raw == msg


def test_cmd_with_suffix_is_normalized() -> None:
    msg = _danmaku()
    msg["cmd"] = "DANMU_MSG:4:0:2:2:2:0"
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.DANMAKU.value
    assert event.cmd == "DANMU_MSG:4:0:2:2:2:0"


# --------------------------------------------------------------------------- #
# data 型命令
# --------------------------------------------------------------------------- #


def test_interact_word_v2_legacy_json_payload() -> None:
    """旧版 JSON 形态的 payload 仍要支持（协议回退兼容）。"""
    msg = {
        "cmd": "INTERACT_WORD_V2",
        "data": {
            "uid": 335979315,
            "uname": "TIM_Init",
            "msg_type": 1,
            "timestamp": 1789707654,
            "fans_medal": {"medal_name": "小豆皮", "medal_level": 1, "is_lighted": 0},
        },
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.INTERACT.value
    assert event.uid == 335979315
    assert event.uname == "TIM_Init"
    assert event.extra["msg_type"] == 1
    assert event.extra["msg_type_label"] == "进入直播间"
    assert event.server_ts == 1789707654
    assert event.medal is not None
    assert event.medal.name == "小豆皮"


# --------------------------------------------------------------------------- #
# INTERACT_WORD_V2 的真实 protobuf 载荷（房间 804456 实抓）
# --------------------------------------------------------------------------- #


def test_interact_word_v2_protobuf_payload(interact_samples: list[dict]) -> None:
    """V2 把数据放在 base64 protobuf 里，旧 JSON 字段已不存在。"""
    interact = [m for m in interact_samples if m.get("cmd") == "INTERACT_WORD_V2"]
    assert interact, "夹具中应包含 INTERACT_WORD_V2"

    event = parse_message(interact[0], ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.INTERACT.value
    assert event.cmd == "INTERACT_WORD_V2"

    # 这些字段只能从 protobuf 里解出来
    assert event.uname == "宅***"
    assert event.server_ts == 1789710200
    assert event.extra["pb_encoded"] is True
    assert event.extra["room_id"] == 804456
    assert event.extra["msg_type"] == 1
    assert event.extra["msg_type_label"] == "进入直播间"
    assert "还没有关注" in event.extra["interact_label"]
    assert event.extra["face"].startswith("https://")


def test_interact_word_v2_distinguishes_follow(interact_samples: list[dict]) -> None:
    """进入直播间（msg_type=1）与关注（msg_type=3）必须区分开。"""
    events = [
        parse_message(message, ROOM, 0.0)
        for message in interact_samples
        if message.get("cmd") == "INTERACT_WORD_V2"
    ]
    follows = [e for e in events if e is not None and e.extra.get("msg_type") == 3]
    assert follows, "夹具中应包含一条关注事件"
    assert follows[0].extra["msg_type_label"] == "关注"
    assert "关注了你" in follows[0].extra["interact_label"]


def test_interact_word_v2_keeps_raw_for_reparsing(interact_samples: list[dict]) -> None:
    interact = [m for m in interact_samples if m.get("cmd") == "INTERACT_WORD_V2"]
    event = parse_message(interact[0], ROOM, 0.0, keep_raw=True)
    assert event is not None
    assert event.raw is not None
    assert "pb" in (event.raw.get("data") or {})


def test_decode_pb_interact_handles_garbage() -> None:
    from bili_danmaku.parser import decode_pb_interact

    assert decode_pb_interact("not-base64!!!") == {}


def test_log_in_notice_explains_anonymous_masking(interact_samples: list[dict]) -> None:
    """实测 LOG_IN_NOTICE 明说「未登录无法查看他人昵称」——这是打码的根因。"""
    notices = [m for m in interact_samples if m.get("cmd") == "LOG_IN_NOTICE"]
    assert notices, "夹具中应包含 LOG_IN_NOTICE"

    event = parse_message(notices[0], ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.NOTICE.value
    assert event.text is not None
    assert "未登录" in event.text


def test_legacy_interact_word_still_supported() -> None:
    msg = {"cmd": "INTERACT_WORD", "data": {"uid": 1, "uname": "a", "msg_type": 2}}
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.INTERACT.value
    assert event.extra["msg_type_label"] == "关注"


def test_new_cmds_seen_in_live_capture() -> None:
    """实测出现的新 cmd 不应丢消息。"""
    for cmd, expected in (
        ("DM_INTERACTION", EventType.DM_INTERACTION.value),
        ("LOG_IN_NOTICE", EventType.NOTICE.value),
        ("ONLINE_RANK_COUNT", EventType.ROOM_STATS.value),
        ("ONLINE_RANK_V3", EventType.ROOM_STATS.value),
        ("WATCHED_CHANGE", EventType.ROOM_STATS.value),
        ("LIKE_INFO_V3_CLICK", EventType.LIKE.value),
        ("LIKE_INFO_V3_UPDATE", EventType.LIKE.value),
        ("ENTRY_EFFECT", EventType.INTERACT.value),
        ("STOP_LIVE_ROOM_LIST", EventType.OTHER.value),
    ):
        event = parse_message({"cmd": cmd, "data": {}}, ROOM, 0.0)
        assert event is not None, cmd
        assert event.type == expected, cmd


def test_dm_interaction_parses_nested_json_string() -> None:
    """DM_INTERACTION 的 data.data 是嵌套 JSON 字符串（实测）。"""
    msg = {
        "cmd": "DM_INTERACTION",
        "data": {
            "data": (
                '{"fade_duration":10000,"cnt":7,"suffix_text":"人正在点赞",'
                '"pv_cnt":0,"count_type":0,"pv_prefix_text":"大家正在点赞",'
                '"pv_suffix_text":"次"}'
            ),
            "dmscore": 36,
            "id": 170132676667904,
            "status": 5,
            "type": 106,
        },
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.DM_INTERACTION.value
    assert event.text == "7人正在点赞"
    assert event.extra["like_count"] == 7
    assert event.extra["interaction_type"] == 106


def test_dm_interaction_tolerates_broken_inner_json() -> None:
    msg = {"cmd": "DM_INTERACTION", "data": {"data": "{ 坏 JSON", "type": 106}}
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.DM_INTERACTION.value
    assert event.extra["like_count"] is None


def test_online_rank_v3_extracts_strings_from_protobuf() -> None:
    """ONLINE_RANK_V3 同样把数据放在 protobuf 里（实测 data.pb）。"""
    pb = base64.b64encode(b"\x12\x06" + "榜一".encode("utf-8")).decode()
    event = parse_message({"cmd": "ONLINE_RANK_V3", "data": {"pb": pb}}, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.ROOM_STATS.value
    assert event.extra["pb_encoded"] is True
    assert "榜一" in event.extra["pb_strings"]


def test_stop_live_room_list_records_count_only() -> None:
    """全局广播携带数百个房间号，只记数量避免脏数据淹没统计。"""
    msg = {"cmd": "STOP_LIVE_ROOM_LIST", "data": {"room_id_list": [1, 2, 3, 4]}}
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.OTHER.value
    assert event.extra["room_count"] == 4
    assert event.extra["global_broadcast"] is True


def test_entry_effect_maps_to_interact() -> None:
    msg = {
        "cmd": "ENTRY_EFFECT",
        "data": {"uid": 123456, "copy_writing": "<%某人%> 进入直播间"},
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.INTERACT.value
    assert event.uid == 123456
    assert "进入直播间" in (event.text or "")


def test_online_rank_count_falls_back_to_online_count() -> None:
    msg = {"cmd": "ONLINE_RANK_COUNT", "data": {"count": 2061, "count_text": "2061"}}
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.extra["online_count"] == 2061


def test_watched_change_falls_back_to_text_small() -> None:
    msg = {"cmd": "WATCHED_CHANGE", "data": {"num": 19984, "text_small": "1.9万"}}
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.extra["watched_num"] == 19984
    assert event.extra["watched_text"] == "1.9万"


def test_like_click_uses_uinfo_block() -> None:
    """LIKE_INFO_V3_CLICK 带完整 uid 与 uinfo.base.name。"""
    msg = {
        "cmd": "LIKE_INFO_V3_CLICK",
        "data": {
            "uid": 445594178,
            "like_text": "为主播点赞了",
            "uinfo": {"base": {"name": "欲心yvxin"}, "uid": 445594178},
        },
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.LIKE.value
    assert event.uid == 445594178
    assert event.extra["like_text"] == "为主播点赞了"


def test_room_stats_fields() -> None:
    msg = {"cmd": "WATCHED_CHANGE", "data": {"num": 1234, "text_large": "1234人看过"}}
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.extra["watched_num"] == 1234
    assert event.extra["watched_text"] == "1234人看过"


def test_super_chat_price_unit_documented() -> None:
    msg = {
        "cmd": "SUPER_CHAT_MESSAGE",
        "data": {
            "price": 30,
            "message": "加油",
            "uid": 999,
            "time": 60,
            "user_info": {"uname": "老板"},
        },
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.SUPER_CHAT.value
    assert event.price == 30
    assert event.text == "加油"
    assert event.uname == "老板"
    assert event.extra["price_unit"] == "CNY"


def test_gift_price_unit_documented() -> None:
    msg = {
        "cmd": "SEND_GIFT",
        "data": {"giftName": "辣条", "num": 5, "total_coin": 500, "uid": 1, "uname": "b"},
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.type == EventType.GIFT.value
    assert event.price == 500
    assert event.extra["price_unit"] == "gold_bean"
    assert event.extra["num"] == 5


def test_parser_preserves_emoticon_mapping() -> None:
    """表情占位符与映射表要保留下来（V2 词云需要把 [dog] 还原成图片）。"""
    extra = json.dumps(
        {
            "content": "哈哈[dog]",
            "id_str": "emote-1",
            "emots": {"[dog]": {"emoticon_unique": "emoji_208", "url": "http://x/y.png"}},
        },
        ensure_ascii=False,
    )
    event = parse_message(_danmaku(text="哈哈[dog]", extra_override=extra), ROOM, 0.0)

    assert event is not None
    assert event.extra["has_emoticon"] is True
    assert "[dog]" in event.extra["emots"]
    assert event.extra["emots"]["[dog]"]["emoticon_unique"] == "emoji_208"
    assert event.text == "哈哈[dog]"
    assert event.has_content() is True


def test_room_emoticon_uses_name_as_text() -> None:
    """主播自定义表情：emoticon_unique 有值，text 就是表情名。"""
    extra = json.dumps({"content": "大拇哥", "id_str": "e2", "emoticon_unique": "room_804456_37238"})
    event = parse_message(_danmaku(text="大拇哥", extra_override=extra), ROOM, 0.0)
    assert event is not None
    assert event.extra["emoticon_unique"] == "room_804456_37238"
    assert event.has_content() is True


def test_real_emote_samples_map_placeholder_to_image(emote_samples: list[dict]) -> None:
    """金样本：真实抓到的表情弹幕必须能把占位符映射到图片（V2 词云/面板依赖它）。

    这几条是从真实 `raw` 里筛出来的（`tests/fixtures/emote_samples.json`），
    覆盖中英文两种占位符形态（`[dog]` 与 `[捂脸]` / `[花]`）。
    """
    assert emote_samples, "夹具不应为空"

    seen: set[str] = set()
    for message in emote_samples:
        event = parse_message(message, ROOM, 1_789_967_000.0, keep_raw=False)
        assert event is not None
        assert event.text, "正文应保留（含占位符）"

        emots = event.extra.get("emots")
        assert isinstance(emots, dict) and emots, f"应保留表情映射: {event.text}"
        assert event.extra.get("has_emoticon") is True

        for placeholder, meta in emots.items():
            # 占位符必须真的出现在正文里，否则说明映射与文本对不上
            assert placeholder in (event.text or ""), f"{placeholder} 不在正文中: {event.text}"
            assert meta.get("url", "").startswith("http"), "应带图片地址"
            assert meta.get("emoticon_unique"), "应带 emoticon_unique"
            assert int(meta.get("width") or 0) > 0 and int(meta.get("height") or 0) > 0
            seen.add(placeholder.strip("[]"))
    assert len(seen) >= 2, f"样本应覆盖多种表情，实得 {seen}"


def test_real_emote_samples_feed_wordcloud(emote_samples: list[dict]) -> None:
    """真实表情样本喂进词云：占位符单独成表情榜，且**不进**词语榜。"""
    from bili_danmaku.text import count_words

    records = []
    for message in emote_samples:
        event = parse_message(message, ROOM, 1_789_967_000.0, keep_raw=False)
        assert event is not None
        records.append((event.type, event.text or "", event.extra))

    report = count_words(records, top=50)
    assert report.emotes, "应统计出表情"
    assert all("[" not in word for word, _ in report.words), "占位符不能进词语榜"
    assert all(item.url for item in report.emotes), "表情榜应带图片链接"

    with_emotes = count_words(records, top=50, include_emotes=True)
    assert any(word.startswith("[") for word, _ in with_emotes.words)


def test_has_content_rules() -> None:
    """has_content 的判定与采集范围一致。"""
    from bili_danmaku.models import Event

    def event(**overrides) -> Event:
        data = {"type": "danmaku", "text": "你好", "extra": {}}
        data.update(overrides)
        return Event(**data)

    assert event().has_content() is True
    assert event(text="   ").has_content() is False
    assert event(text=None).has_content() is False
    assert event(text=None, extra={"emots": {"[dog]": {}}}).has_content() is True
    assert event(text=None, extra={"emoticon_unique": "room_1_2"}).has_content() is True
    # 类型也必须在采集范围内
    assert event(type="danmaku").is_content_event() is True
    assert event(type="super_chat", text="加油").is_content_event() is True
    assert event(type="interact", text="").is_content_event() is False
    assert event(type="gift", text="辣条").is_content_event() is False


# --------------------------------------------------------------------------- #
# 付费事件的真实结构（房间 804456 实抓 COMBO_SEND）
# --------------------------------------------------------------------------- #


def test_real_combo_send_is_parsed_from_snake_case(priced_samples: list[dict]) -> None:
    """实测字段是 snake_case，金额在 combo_total_coin，用户信息在 sender_uinfo。"""
    message = next(m for m in priced_samples if m.get("cmd") == "COMBO_SEND")
    event = parse_message(message, ROOM, 0.0)

    assert event is not None
    assert event.type == EventType.GIFT.value
    assert event.text == "传音符"
    assert event.price == 200, "金额应取自 combo_total_coin"
    assert event.extra["price_unit"] == "gold_bean"
    assert event.extra["gift_name"] == "传音符"
    assert event.extra["gift_id"] == 35888
    assert event.extra["coin_type"] == "gold"

    # 用户信息来自嵌套的 sender_uinfo
    assert event.uid == 3546570133866786
    assert event.uname == "sjsnjjjj"
    assert event.user_level == 2

    # 粉丝牌来自 medal_info（不是 fans_medal）
    assert event.medal is not None
    assert event.medal.name == "粉丝团"
    assert event.medal.level == 3
    assert event.medal.anchor_uid == 3546381253872127
    assert event.medal.color == 6067854


def test_combo_send_keeps_raw(priced_samples: list[dict]) -> None:
    message = next(m for m in priced_samples if m.get("cmd") == "COMBO_SEND")
    event = parse_message(message, ROOM, 0.0, keep_raw=True)
    assert event is not None
    assert event.raw is not None


def test_gift_num_prefers_nonzero_count() -> None:
    """实测 gift_num=0（假值），真实数量在 total_num / batch_combo_num —— 不能被 `or` 穿透。"""
    msg = {
        "cmd": "COMBO_SEND",
        "data": {"gift_name": "传音符", "gift_num": 0, "total_num": 2, "batch_combo_num": 2},
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.extra["num"] == 2


def test_medal_is_light_zero_is_preserved() -> None:
    """is_lighted=0 是合法值，不能被当成「没有值」而落成 None。"""
    msg = {
        "cmd": "SEND_GIFT",
        "data": {"gift_name": "x", "medal_info": {"medal_name": "牌", "medal_level": 1, "is_lighted": 0}},
    }
    event = parse_message(msg, ROOM, 0.0)
    assert event is not None
    assert event.medal is not None
    assert event.medal.is_light == 0


def test_fans_medal_and_medal_info_both_supported() -> None:
    for key in ("fans_medal", "medal_info"):
        msg = {"cmd": "INTERACT_WORD", "data": {key: {"medal_name": "牌", "medal_level": 7}}}
        event = parse_message(msg, ROOM, 0.0)
        assert event is not None, key
        assert event.medal is not None, key
        assert event.medal.level == 7


def test_nested_user_blocks_all_supported() -> None:
    """uinfo / sender_uinfo / user_info 三种嵌套都要能取到 uid 与昵称。"""
    for key in ("uinfo", "sender_uinfo", "user_info"):
        msg = {"cmd": "LIKE_INFO_V3_CLICK", "data": {key: {"uid": 42, "base": {"name": "某人"}}}}
        event = parse_message(msg, ROOM, 0.0)
        assert event is not None, key
        assert event.uid == 42, key
        assert event.uname == "某人", key


# --------------------------------------------------------------------------- #
# 去重键
# --------------------------------------------------------------------------- #


def test_dedupe_key_prefers_msg_id(danmu_samples: list[dict]) -> None:
    event = parse_message(danmu_samples[0], ROOM, 0.0)
    assert event is not None
    assert "5fd7e66727d03c9b40851feb116aacc58384" in event.dedupe_key()


def test_dedupe_key_falls_back_without_msg_id() -> None:
    msg = _danmaku(text="同样的话", extra_override="{ 坏 JSON")
    first = parse_message(msg, ROOM, 0.0)
    second = parse_message(msg, ROOM, 0.0)
    assert first is not None and second is not None
    assert first.msg_id is None
    assert first.dedupe_key() == second.dedupe_key()
