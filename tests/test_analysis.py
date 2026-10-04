"""本地感知分析（V3-b 3A）：`analysis.py` 纯函数 + `LiveWindow` 有界性 + `/api/analysis`。

三类关注点，按重要性排序：

1. **边界与不变量**：窗口必须**有界**（面板要开一整天），`add()` 必须不抛异常
   （它在采集的 sink 协程里被同步调用）；
2. **启发式的口径**：疑似刷屏/异常账号是"参考"不是"判定" —— 所以既要能抓到
   明显的刷屏，也要**不能**把正常的短句刷屏（`哈哈`/`6`）全报成异常；
3. **热点突增用篇均速率**，不能用绝对条数（最后一片常常是半截）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bili_danmaku.analysis import (
    MAX_WORDS_PER_MESSAGE,
    Message,
    WordBucket,
    analyze,
    bucket_of,
    hotspot_words,
    spam_groups,
    suspicious_users,
    word_frequency,
    words_from_buckets,
)
from bili_danmaku.config import Config
from bili_danmaku.models import Event
from bili_danmaku.server.app import create_app
from bili_danmaku.server.hub import (
    ANALYSIS_WINDOW_MESSAGES,
    LiveWindow,
    WebHub,
)

ROOM = 7


class StubPanel:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def rooms(self) -> list[dict[str, Any]]:
        return []

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def lookup_room(self, room: Any) -> dict[str, Any]:
        return {"room_id": int(room)}


def msg(ts: float, text: str, *, uid: int | None = 1, uname: str = "甲", type_: str = "danmaku"):
    return Message(recv_ts=ts, uid=uid, uname=uname, text=text, type=type_)


def event(ts: float, text: str, *, uid: int = 1, uname: str = "甲", type_: str = "danmaku") -> Event:
    return Event(seq=1, room_id=ROOM, type=type_, recv_ts=ts, uid=uid, uname=uname, text=text)


# --------------------------------------------------------------------------- #
# 词频
# --------------------------------------------------------------------------- #


def test_word_frequency_counts_and_users() -> None:
    messages = [
        msg(1.0, "主播好厉害", uid=1, uname="甲"),
        msg(2.0, "主播真的好厉害", uid=2, uname="乙"),
        msg(3.0, "厉害", uid=1, uname="甲"),
    ]
    payload = word_frequency(messages, top=10)
    items = {item["word"]: item for item in payload["items"]}
    assert "主播" in items and "厉害" in items
    assert items["主播"]["count"] == 2
    assert items["主播"]["users"] == 2, "两个人说过「主播」"
    assert items["厉害"]["count"] == 3
    assert items["厉害"]["users"] == 2
    assert payload["total_words"] == sum(item["count"] for item in payload["items"])
    assert 0 < items["主播"]["share"] <= 1


def test_word_frequency_ignores_non_danmaku_and_empty() -> None:
    messages = [
        msg(1.0, "主播好", uid=1),
        msg(2.0, "进入直播间", type_="interact"),
        msg(3.0, "", uid=2),
        msg(4.0, None, uid=3),
        msg(5.0, "[dog]", uid=4),  # 纯表情占位符 → 清洗后为空
    ]
    payload = word_frequency(messages, top=10)
    assert [item["word"] for item in payload["items"]] == ["主播"]
    assert payload["total_words"] == 1


def test_word_frequency_caps_words_per_message() -> None:
    long_text = " ".join(f"词{i}" for i in range(200))
    payload = word_frequency([msg(1.0, long_text)], top=500)
    assert payload["total_words"] <= MAX_WORDS_PER_MESSAGE, "超长文本不该主导词频"


def test_word_frequency_accepts_dicts_and_objects() -> None:
    """输入形状要宽容：hub 给的是 dict，测试与别处可能给 Event/Message。"""
    payload = word_frequency(
        [
            {"recv_ts": 1.0, "uid": 1, "uname": "甲", "text": "主播好", "type": "danmaku"},
            event(2.0, "主播好"),
            msg(3.0, "主播好"),
        ],
        top=5,
    )
    assert payload["items"][0]["word"] == "主播"
    assert payload["items"][0]["count"] == 3


def test_words_from_buckets_merges_without_tokenizing() -> None:
    buckets = [
        WordBucket(start=0.0, counts={"主播": 3, "厉害": 1}),
        WordBucket(start=60.0, counts={"主播": 2, "哈哈": 5}),
    ]
    payload = words_from_buckets(buckets, top=10)
    assert payload["total_words"] == 11
    assert payload["unique_words"] == 3
    by_word = {item["word"]: item for item in payload["items"]}
    # 注意 主播 与 哈哈 都是 5 次（并列），所以断言要按词取值而不是按下标
    assert by_word["哈哈"] == {
        "word": "哈哈",
        "count": 5,
        "share": round(5 / 11, 4),
        "users": 0,
        "from_buckets": True,
    }
    assert by_word["厉害"]["count"] == 1


# --------------------------------------------------------------------------- #
# 热点突增
# --------------------------------------------------------------------------- #


def test_hotspot_uses_rate_not_absolute_count() -> None:
    """最后一片是半截（时间更短）时，不能用绝对条数判突增。"""
    buckets = [
        WordBucket(start=0.0, counts={"主播": 10}),  # 篇均基线偏高
        WordBucket(start=60.0, counts={"主播": 10}),
        WordBucket(start=120.0, counts={"主播": 11}),  # 与基线持平
    ]
    payload = hotspot_words(buckets, min_count=3, ratio=2.5)
    assert payload["items"] == [], "持平不该被判成突增"


def test_hotspot_detects_spike_and_new_word() -> None:
    # 基线：两片各 1 次 ⇒ 篇均 1.0；当前片 6 次 ⇒ 6 倍
    buckets = [
        WordBucket(start=0.0, counts={"主播": 1}),
        WordBucket(start=60.0, counts={"主播": 1}),
        WordBucket(start=120.0, counts={"主播": 6, "抽奖": 9}),
    ]
    payload = hotspot_words(buckets, min_count=3, ratio=2.0)
    by_word = {item["word"]: item for item in payload["items"]}
    assert by_word["主播"]["ratio"] == 6.0
    assert by_word["主播"]["baseline"] == 1.0
    assert "抽奖" in by_word
    assert by_word["抽奖"]["is_new"] is True
    assert by_word["抽奖"]["ratio"] is None, "基线为 0 的词不该编造倍率"


def test_hotspot_needs_at_least_two_buckets() -> None:
    assert hotspot_words([WordBucket(start=0.0, counts={"a": 9})])["items"] == []
    payload = hotspot_words([])
    assert payload["items"] == [] and "note" in payload


def test_hotspot_respects_min_count() -> None:
    buckets = [
        WordBucket(start=0.0, counts={}),
        WordBucket(start=60.0, counts={"冷门": 1}),
    ]
    assert hotspot_words(buckets, min_count=3)["items"] == [], "只出现一次不算热点"


def test_bucket_of_floors_to_slot() -> None:
    assert bucket_of(125.0, size_s=60) == 120.0
    assert bucket_of(120.0, size_s=60) == 120.0
    assert bucket_of(1.0, size_s=0) == 1.0, "size_s=0 不能除零"


# --------------------------------------------------------------------------- #
# 疑似刷屏
# --------------------------------------------------------------------------- #


def test_spam_groups_flags_repeated_text() -> None:
    messages = [msg(1.0 + index, "这波太强了吧", uid=1) for index in range(6)]
    payload = spam_groups(messages, min_count=4)
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["count"] == 6
    assert item["users"] == 1
    assert item["single_user"] is True, "同一个人反复发 → 更可疑"
    assert item["span_s"] == 5.0


def test_spam_groups_distinguishes_echo_from_one_person() -> None:
    # uid 从 1 开始：uid=0 是"打码匿名用户"，不属于这个用例的范围
    messages = [msg(1.0 + index, "哈哈哈笑死", uid=index + 1) for index in range(5)]
    item = spam_groups(messages, min_count=4)["items"][0]
    assert item["users"] == 5
    assert item["single_user"] is False, "五个人都在发 → 更像跟风而不是刷屏"


def test_spam_groups_counts_uid_zero_as_a_real_user() -> None:
    """uid=0 是被打码的匿名用户：不能因为"假值"就被漏掉（users 会偏小）。"""
    messages = [msg(1.0, "打个卡表示我看过这里", uid=0) for _ in range(4)]
    item = spam_groups(messages, min_count=4)["items"][0]
    assert item["count"] == 4
    assert item["users"] == 1, "uid=0 也算一个用户"


def test_spam_groups_ignores_short_banter() -> None:
    """`哈哈`/`6`/`？？？` 这类短句在直播间大量出现是正常气氛，不能全报成刷屏。"""
    messages = [msg(1.0 + index, "哈哈", uid=index) for index in range(20)]
    assert spam_groups(messages)["items"] == []

    messages = [msg(1.0 + index, "666", uid=index) for index in range(20)]
    assert spam_groups(messages)["items"] == [], "3 个字符仍低于 min_length=4"


def test_spam_groups_respects_min_count() -> None:
    messages = [msg(1.0 + index, "同一个长句子内容", uid=1) for index in range(3)]
    assert spam_groups(messages, min_count=4)["items"] == []


# --------------------------------------------------------------------------- #
# 疑似异常账号
# --------------------------------------------------------------------------- #


def test_suspicious_scores_dense_repeater() -> None:
    # 跨度要够长（> MIN_RATE_SPAN_S）才是"可推断的行为"：真实复读机是在
    # 几十秒内反复刷，而不是在同一毫秒里刷 12 条
    messages = [
        msg(1.0 + index * 3.0, "复读同一句内容", uid=99, uname="复读机") for index in range(12)
    ]
    payload = suspicious_users(messages, min_messages=8)
    assert payload["items"], "高频 + 高重复应上榜"
    top = payload["items"][0]
    assert top["uid"] == 99 and top["uname"] == "复读机"
    # 12 条完全相同 ⇒ 重复率 100%（用 `unique` 而不是 `unique-1` 会得到 91.7%）
    assert top["repeat_ratio"] == 1.0
    assert top["score"] >= 45
    assert top["reasons"], "必须给出理由，而不是只给一个分数"


def test_suspicious_ignores_normal_chatter() -> None:
    """正常聊天：发言稀疏、每条都不一样 —— 不该上榜。"""
    messages = [
        msg(1.0 + index * 6.0, f"这是第 {index} 条完全不同的正常发言内容", uid=7, uname="普通人")
        for index in range(10)
    ]
    assert suspicious_users(messages)["items"] == []


def test_suspicious_respects_min_messages() -> None:
    messages = [msg(1.0 + index * 0.1, "刷屏", uid=1) for index in range(5)]
    assert suspicious_users(messages, min_messages=8)["items"] == []


def test_suspicious_skips_messages_without_uid() -> None:
    messages = [msg(1.0 + index, "没有 uid 的内容", uid=None) for index in range(20)]
    assert suspicious_users(messages)["items"] == []
    assert suspicious_users(messages)["scanned"] == 0


def test_suspicious_ignores_backfilled_history_with_identical_timestamps() -> None:
    """**实测踩到的误报**：历史补种会把一批弹幕写成同一个 `recv_ts`。

    那时 `末条 - 首条 = 0`，若直接 `count / max(span, 1)` 会算出"600 条/分钟"，
    把一位正常用户报成疑似异常账号。正确做法是**放弃速率类结论**，
    而不是硬套一个窗口编出数字。
    """
    messages = [
        msg(1790776026.12, f"这是第 {index} 条不同的内容", uid=42, uname="普通人") for index in range(10)
    ]
    payload = suspicious_users(messages)
    assert payload["items"] == [], f"不该因时间戳精度不足而误报：{payload['items']}"
    assert payload["rate_available"] is False


def test_suspicious_still_flags_identical_timestamp_repeater() -> None:
    """同刻但内容全一样 → 仍应靠**重复率**上榜（那一项与时间精度无关）。

    注意默认门槛是 45 分，而同刻场景下速率两项已被放弃（只剩重复率 40 分），
    所以这里调低门槛来验证"重复率这一路仍然有效"。
    """
    messages = [msg(1790776026.12, "同一句话反复刷", uid=42, uname="复读机") for _ in range(10)]
    item = suspicious_users(messages, min_score=35)["items"][0]
    assert item["repeat_ratio"] == 1.0
    assert item["rate_available"] is False
    assert any("重复率" in reason for reason in item["reasons"])
    assert item["reasons"][-1].startswith("（时间戳同刻")

    # 而同刻 + 内容各不相同 ⇒ 什么都不该报（既没速率证据、也没重复证据）
    varied = [msg(1790776026.12, f"第 {index} 条各不相同", uid=43) for index in range(10)]
    assert suspicious_users(varied, min_score=35)["items"] == []


def test_suspicious_scoring_uses_the_real_span_when_timestamps_differ() -> None:
    """跨度足够时，速率是**真实值**（不被任何下限压平）。"""
    messages = [msg(1000.0 + index * 1.5, "复读复读", uid=5, uname="真爆发") for index in range(40)]
    item = suspicious_users(messages, min_messages=8)["items"][0]
    # 58.5 秒内 40 条 ≈ 41 条/分钟
    assert 35 <= item["per_min"] <= 45, item["per_min"]
    assert item["rate_available"] is True
    assert any("密度" in reason for reason in item["reasons"])


def test_suspicious_ignores_short_observation_span() -> None:
    """**第二次实测误报**：房间未开播时 10 条补种落在 0.9 秒内。

    那时时间戳各不相同（不是"完全同刻"），但 1 秒的样本不足以推断行为 ——
    `count/max(span,1)` 会给出"600 条/分钟"，还会命中"集中发送"。
    跨度不足 `MIN_RATE_SPAN_S` 时必须放弃速率类结论。
    """
    messages = [
        msg(1790776026.12 + index * 0.1, f"这是第 {index} 条不同的内容", uid=42, uname="翎夜律")
        for index in range(10)
    ]
    payload = suspicious_users(messages)
    assert payload["items"] == [], f"0.9 秒的样本不该推断出速率结论：{payload['items']}"
    assert payload["rate_available"] is False


def test_suspicious_flags_long_enough_burst() -> None:
    """跨度够长（60 秒内 60 条 = 每分钟 60 条）时，速率结论应当成立。"""
    messages = [
        msg(1000.0 + index, "复读同一句", uid=5, uname="真爆发") for index in range(60)
    ]
    item = suspicious_users(messages)["items"][0]
    assert item["rate_available"] is True
    assert item["per_min"] == pytest.approx(60.9, abs=1.0)
    assert any("密度" in reason for reason in item["reasons"])


def test_suspicious_scores_are_bounded() -> None:
    messages = [msg(1.0 + index, "同", uid=1) for index in range(60)]
    item = suspicious_users(messages, min_messages=8)["items"][0]
    assert 0 < item["score"] <= 100, "分数必须有上限（40+40+20）"


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def test_analyze_returns_all_sections() -> None:
    messages = [msg(1.0 + index, "主播好厉害", uid=1) for index in range(10)]
    buckets = [WordBucket(start=0.0, counts={"主播": 5}), WordBucket(start=60.0, counts={"主播": 20})]
    payload = analyze(messages, buckets=buckets, top_words=5)
    assert set(payload) >= {"messages", "window_s", "words", "hotspot", "spam", "suspicious", "note"}
    assert payload["messages"] == 10
    assert "本机" in payload["note"], "必须声明数据不出本机"


def test_analyze_accepts_precomputed_words() -> None:
    """传了 `words` 就不该再分词（省掉最贵的一步）。"""
    messages = [msg(1.0, "主播好")]
    payload = analyze(messages, words={"total_words": 0, "unique_words": 0, "items": []})
    assert payload["words"]["items"] == []


def test_analyze_handles_empty_input() -> None:
    payload = analyze([])
    assert payload["messages"] == 0
    assert payload["words"]["items"] == []
    assert payload["spam"]["items"] == []


# --------------------------------------------------------------------------- #
# LiveWindow：有界性与健壮性
# --------------------------------------------------------------------------- #


def test_live_window_keeps_messages_bounded() -> None:
    window = LiveWindow(window_messages=50)
    for index in range(500):
        window.add(event(1000.0 + index * 0.1, f"内容{index}"))
    assert window.size == 50, "弹幕原文必须有硬上限"
    assert window.messages[-1]["text"] == "内容499"


def test_live_window_keeps_buckets_bounded() -> None:
    window = LiveWindow(bucket_s=60.0, bucket_count=5)
    # 跨 20 个时间片
    for index in range(20):
        window.add(event(1000.0 + index * 60.0, f"第{index}片"))
    assert len(window.buckets) <= 5, "时间片数量必须有上限"
    assert window.buckets[-1].start == pytest.approx(bucket_of(1000.0 + 19 * 60.0, size_s=60))


def test_live_window_resets_after_a_long_gap() -> None:
    """面板卡了很久/房间刚恢复时，不能拿旧数据当基线（会制造假突增）。"""
    window = LiveWindow(bucket_s=60.0, bucket_count=5)
    window.add(event(1000.0, "旧内容"))
    window.add(event(1000.0 + 60 * 60, "新内容"))  # 隔了一小时
    assert window.size == 1, "空档过大应整体重置"
    assert window.messages[0]["text"] == "新内容"


def test_live_window_caps_words_per_bucket() -> None:
    window = LiveWindow(bucket_s=600.0, words_per_bucket=20)
    for index in range(200):
        window.add(event(1000.0 + index, f"唯一词{index}", type_="danmaku"))
    counts = window.buckets[-1].counts
    assert len(counts) <= 20, "词表必须有上限（否则等于内存泄漏）"
    assert sum(counts.values()) > 0


def test_live_window_survives_garbage_input() -> None:
    """`add()` 在采集链路上被调用 —— 任何异常都不许冒出去。"""
    window = LiveWindow()
    for bad in (None, object(), {"recv_ts": "x"}, event(0.0, "没有时间戳")):
        window.add(bad)  # 不该抛
    assert window.size >= 0


def test_live_window_skips_events_without_text() -> None:
    window = LiveWindow()
    window.add(event(1000.0, "", type_="interact"))
    window.add(event(1001.0, None, type_="like"))
    assert window.size == 0, "没有正文的事件不参与分析"


def test_live_window_does_not_count_non_danmaku_words() -> None:
    window = LiveWindow(bucket_s=600.0)
    window.add(event(1000.0, "进入直播间", type_="interact"))
    assert window.buckets[-1].counts == {}, "只有弹幕才参与词频"


def test_live_window_reset() -> None:
    window = LiveWindow()
    window.add(event(1000.0, "内容"))
    window.reset()
    assert window.size == 0 and window.buckets == []


# --------------------------------------------------------------------------- #
# hub 集成
# --------------------------------------------------------------------------- #


def test_hub_publish_feeds_the_window() -> None:
    hub = WebHub()
    for index in range(10):
        hub.publish(event(1000.0 + index, "主播好厉害"))
    messages, buckets = hub.window_snapshot(ROOM)
    assert len(messages) == 10
    assert buckets and buckets[-1].counts["主播"] == 10


def test_hub_window_snapshot_merges_rooms() -> None:
    hub = WebHub()
    hub.publish(Event(seq=1, room_id=1, type="danmaku", recv_ts=1000.0, text="甲房间的话"))
    hub.publish(Event(seq=2, room_id=2, type="danmaku", recv_ts=1001.0, text="乙房间的话"))
    assert len(hub.window_snapshot(1)[0]) == 1
    merged_messages, merged_buckets = hub.window_snapshot(None)
    assert len(merged_messages) == 2
    assert sum(sum(b.counts.values()) for b in merged_buckets) > 0


def test_hub_window_snapshot_empty_for_unknown_room() -> None:
    assert WebHub().window_snapshot(999) == ([], [])


def test_hub_mark_reset_clears_windows() -> None:
    hub = WebHub()
    hub.publish(event(1000.0, "内容"))
    hub.mark_reset()
    assert hub.window_snapshot(ROOM)[0] == []


def test_hub_window_does_not_grow_without_bound() -> None:
    """常驻面板跑一整天：窗口必须收敛到一个定值，而不是一直涨。"""
    hub = WebHub()
    for index in range(ANALYSIS_WINDOW_MESSAGES * 2):
        hub.publish(event(1000.0 + index * 0.01, f"内容{index}"))
    assert len(hub.window_snapshot(ROOM)[0]) <= ANALYSIS_WINDOW_MESSAGES


# --------------------------------------------------------------------------- #
# /api/analysis
# --------------------------------------------------------------------------- #


@pytest.fixture
def client():
    config = Config.load(path="missing.toml")
    hub = WebHub()
    for index in range(30):
        hub.publish(
            Event(
                seq=index,
                room_id=ROOM,
                type="danmaku",
                recv_ts=1000.0 + index * 0.5,
                uid=100 + index % 3,
                uname=f"用户{index % 3}",
                text="主播好厉害" if index % 3 else "这波操作太强了",
            )
        )
    app = create_app(config, None, hub, StubPanel())  # type: ignore[arg-type]
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
        test_client.hub = hub  # type: ignore[attr-defined]
        yield test_client


def test_analysis_endpoint_contract(client) -> None:
    payload = client.get("/api/analysis", params={"room_id": ROOM, "top": 10}).json()
    assert set(payload) >= {
        "messages", "window_s", "words", "hotspot", "spam", "suspicious",
        "note", "room_id", "bucket_s", "window_cap", "source",
    }
    assert payload["source"] == "memory"
    assert payload["room_id"] == ROOM
    assert payload["messages"] == 30
    assert payload["words"]["items"], "应有词频"
    assert payload["words"]["items"][0]["from_buckets"] is True, "词频走省算路径"
    assert "本机" in payload["note"]


def test_analysis_endpoint_validation(client) -> None:
    assert client.get("/api/analysis", params={"top": 1}).status_code == 422
    assert client.get("/api/analysis", params={"top": 500}).status_code == 422
    assert client.get("/api/analysis", params={"tokenizer": "bogus"}).status_code == 422
    assert client.get("/api/analysis", params={"tokenizer": "fallback"}).status_code == 200


def test_analysis_endpoint_all_rooms(client) -> None:
    payload = client.get("/api/analysis").json()
    assert payload["room_id"] is None
    assert payload["messages"] == 30


def test_analysis_endpoint_empty_room(client) -> None:
    payload = client.get("/api/analysis", params={"room_id": 999999}).json()
    assert payload["messages"] == 0
    assert payload["words"]["items"] == []
    assert payload["hotspot"]["items"] == []


def test_analysis_endpoint_does_not_touch_the_database(client) -> None:
    """分析只看内存窗口：不该因为没启用 sqlite 就报错，也不该去查库。"""
    response = client.get("/api/analysis", params={"room_id": ROOM})
    assert response.status_code == 200
