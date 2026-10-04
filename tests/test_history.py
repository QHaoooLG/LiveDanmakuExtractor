"""V3-a 历史浏览的存储层：游标分页 / 瘦投影 / 密度聚合 / 会话列表。

这些用例的重点不是"能查出数据"，而是**游标语义**——翻页不能重复、不能漏条，
尤其是**同一时间戳有多条**的情况（实测真实数据里 393 组并列、最多 10 条同刻）。
只比 `recv_ts` 的实现会在页边界悄悄丢数据，而这种缺陷在人工点几下时几乎看不出来。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bili_danmaku.sinks.sqlite import SCHEMA
from bili_danmaku.store import (
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    DanmakuStore,
    StoreError,
)

ROOM = 804456
OTHER_ROOM = 123456


def realistic_raw(index: int) -> str:
    """一条接近真实的 `raw_json`（实测真实样本约 1.6~2.4 KB）。

    夹具必须带上它，否则"瘦投影省了多少字节"这类断言会被一个假的小 raw 骗过去。
    """
    payload = {
        "cmd": "DANMU_MSG",
        "info": [
            [
                0, 1, 25, 16777215, 1_789_710_200_000 + index, 0, 0, "abcdef123456", 0, 0, 0,
                "", 0, "{}", "{}",
                {
                    "extra": json.dumps(
                        {
                            "content": f"内容{index}",
                            "id_str": f"m{index}",
                            "color": 16777215,
                            "font_size": 25,
                            "mode": 0,
                            "show_reply": True,
                            "reply_uname": "",
                            "emots": None,
                        },
                        ensure_ascii=False,
                    ),
                    "user": {
                        "uid": 1000 + index,
                        "base": {"name": f"用户{index}", "face": "https://i0.hdslb.com/bfs/face/x.jpg"},
                        "medal": {"name": "牌子", "level": 3, "color": 6067854},
                        "wealth": {"level": 5},
                        "guard": {"level": 0},
                    },
                    "check_info": {"ts": 1_789_710_200, "ct": "CT"},
                },
            ],
            f"内容{index}",
            [1000 + index, f"用户{index}", 0, 0, 0, 10000, 1, ""],
            [],
            [11, 0, 6406234, ">50000", 0],
            ["", ""],
            0, 0, None,
            {"ct": "CT", "ts": 1_789_710_200},
            0, 0, None, None, 0, 390, [5], None,
        ],
        "dm_v2": {"extra": "{\"nested\": \"payload to make raw realistically large\"}"},
    }
    return json.dumps(payload, ensure_ascii=False)


def build_db(
    tmp_path: Path,
    *,
    rows: int = 20,
    room_id: int = ROOM,
    tied: int = 3,
    session_id: str = "s1",
    with_extra: bool = True,
) -> Path:
    """建一个可控的小库。

    `tied` 条事件共用一个时间戳 —— 这是游标用例的关键输入（模拟"同一批推送"）。
    """
    db = tmp_path / "danmaku.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO session(id, room_id, short_id, started_at) VALUES (?,?,?,?)",
        (session_id, room_id, 456, 1_790_000_000.0),
    )
    for index in range(1, rows + 1):
        stamp = 1_790_000_000.0 + (index // tied) * 1.0
        conn.execute(
            "INSERT INTO event (session_id, room_id, recv_ts, type, cmd, source, msg_id,"
            " uid, uname, text, price, medal_name, medal_level, dedupe_key, raw_json, extra_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                room_id,
                stamp,
                "super_chat" if index % 7 == 0 else "danmaku",
                "DANMU_MSG",
                "live",
                f"m{index}",
                1000 + index,
                f"用户{index}",
                f"内容{index}",
                30 if index % 7 == 0 else None,
                "牌子",
                3,
                f"k{index}",
                realistic_raw(index),
                '{"emots": {"[dog]": {"url": "https://x/y.png"}}}' if with_extra else None,
            ),
        )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def store(tmp_path: Path):
    opened = DanmakuStore(build_db(tmp_path, rows=60, tied=3))
    yield opened
    opened.close()


# --------------------------------------------------------------------------- #
# 瘦投影
# --------------------------------------------------------------------------- #


def test_history_items_are_a_slim_projection(store: DanmakuStore) -> None:
    """投影必须窄：不含 raw、只保留 emots。"""
    items, _, _ = store.iter_history(room_id=ROOM, limit=5)
    assert items, "应该有数据"
    item = items[0]
    assert "raw" not in item and "dedupe_key" not in item and "field_source" not in item
    assert set(item) == {
        "id", "room_id", "session_id", "recv_ts", "type", "uid", "uname",
        "text", "price", "medal_name", "medal_level", "emots",
    }
    assert item["emots"] == {"[dog]": {"url": "https://x/y.png"}}
    assert item["medal_level"] == 3
    # 完整 Event 才有的字段不该出现
    assert "cmd" not in item and "server_ts" not in item


def test_projection_survives_missing_extra_column(tmp_path: Path) -> None:
    """旧库（schema v1，没有 extra_json）不应让历史查询崩。"""
    db = build_db(tmp_path, rows=5, with_extra=False)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE event DROP COLUMN extra_json")
    conn.commit()
    conn.close()

    opened = DanmakuStore(db)
    try:
        items, _, _ = opened.iter_history(room_id=ROOM, limit=3)
        assert len(items) == 3
        assert items[0]["emots"] is None
    finally:
        opened.close()


def test_missing_database_raises_store_error(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        DanmakuStore(tmp_path / "nope.db")


# --------------------------------------------------------------------------- #
# 游标语义：不重复、不漏条
# --------------------------------------------------------------------------- #


def test_first_page_is_the_newest_slice_in_ascending_order(store: DanmakuStore) -> None:
    items, has_older, has_newer = store.iter_history(room_id=ROOM, limit=4)
    assert len(items) == 4
    assert has_older is True
    assert has_newer is False, "已经是库尾，没有更新的了"
    keys = [(item["recv_ts"], item["id"]) for item in items]
    assert keys == sorted(keys), "返回必须升序，方便直接渲染"
    # 必须是"最新"的一页，而不是最旧的一页（旧 iter_events 的坑）
    assert items[-1]["id"] == 60


def test_paging_up_covers_every_row_exactly_once(store: DanmakuStore) -> None:
    """一路向上翻到底：累计必须恰好等于总数，且无重复。"""
    seen: list[int] = []
    cursor = None
    for _ in range(50):  # 上限防死循环
        items, has_older, _ = store.iter_history(room_id=ROOM, limit=7, before=cursor)
        if not items:
            break
        seen.extend(item["id"] for item in items)
        cursor = (items[0]["recv_ts"], items[0]["id"])
        if not has_older:
            break

    assert len(seen) == len(set(seen)), "翻页出现重复"
    assert sorted(seen) == list(range(1, 61)), "翻页漏掉了行"
    assert has_older is False, "翻到底后 has_older 应为 False"


def test_tied_timestamps_do_not_leak_across_pages(store: DanmakuStore) -> None:
    """**核心用例**：同一时间戳的多条事件必须能被逐页切干净。

    只比 recv_ts 的实现会在这里漏条（游标取 `recv_ts < ts` 会把同刻剩下的全丢掉）。
    数据构造：`(index // 3)` 决定时间戳 ⇒ 最末三条（58/59/60）同刻。
    """
    # 整库向上翻完：累计必须恰好 1..60，无重复无遗漏
    seen: list[int] = []
    cursor = None
    while True:
        items, has_older, _ = store.iter_history(room_id=ROOM, limit=7, before=cursor)
        if not items:
            break
        seen.extend(item["id"] for item in items)
        cursor = (items[0]["recv_ts"], items[0]["id"])
        if not has_older:
            break
    assert sorted(seen) == list(range(1, 61)), f"并列时间戳处漏条/重复：{sorted(seen)}"

    # 页大小 1 强制**每一页**都落在并列组内部 —— 这是最狠的切法
    seen_one: list[int] = []
    cursor = None
    while True:
        items, has_older, _ = store.iter_history(room_id=ROOM, limit=1, before=cursor)
        if not items:
            break
        seen_one.extend(item["id"] for item in items)
        cursor = (items[0]["recv_ts"], items[0]["id"])
        if not has_older:
            break
    assert sorted(seen_one) == list(range(1, 61)), "逐条翻页在并列时间戳上漏条"

    # 直接验证边界：`recv_ts = 1790000019.0` 恰好是 {58, 59} 两条同刻。
    # 游标停在 (19.0, 60) —— 也就是说"同刻但有更大的 id 已被取走"，
    # 复合游标必须靠 id 把 58/59 补回来（只比 recv_ts 的实现这里会返回空页）。
    page, _, _ = store.iter_history(
        room_id=ROOM, limit=2, before=(1_790_000_019.0, 60)
    )
    assert [item["id"] for item in page] == [58, 59]
    assert page[0]["recv_ts"] == page[1]["recv_ts"], "前置条件：这两条同刻"
    older, _, _ = store.iter_history(
        room_id=ROOM, limit=2, before=(page[0]["recv_ts"], page[0]["id"])
    )
    assert [item["id"] for item in older] == [56, 57]
    assert older[-1]["recv_ts"] == page[0]["recv_ts"], "同刻剩下的那条必须还能取到"


def test_paging_down_returns_newer_rows_without_overlap(store: DanmakuStore) -> None:
    first, _, _ = store.iter_history(room_id=ROOM, limit=5)
    newest = (first[-1]["recv_ts"], first[-1]["id"])
    # 库尾没有更新的数据
    items, has_older, has_newer = store.iter_history(room_id=ROOM, limit=5, after=newest)
    assert items == []
    assert has_older is False and has_newer is False

    # 从中间往下翻，应拿到紧邻的更新数据
    older, _, _ = store.iter_history(room_id=ROOM, limit=5, before=(first[0]["recv_ts"], first[0]["id"]))
    cursor = (older[-1]["recv_ts"], older[-1]["id"])
    newer, has_older2, has_newer2 = store.iter_history(room_id=ROOM, limit=5, after=cursor)
    assert [item["id"] for item in newer] == [item["id"] for item in first]
    assert has_older2 is True and has_newer2 is False


def test_before_and_after_together_is_rejected(store: DanmakuStore) -> None:
    with pytest.raises(ValueError, match="不能同时"):
        store.iter_history(room_id=ROOM, limit=5, before=(1.0, 1), after=(2.0, 2))


def test_page_size_is_clamped(store: DanmakuStore) -> None:
    items, _, _ = store.iter_history(room_id=ROOM, limit=10_000)
    assert len(items) <= MAX_HISTORY_LIMIT
    assert DEFAULT_HISTORY_LIMIT <= MAX_HISTORY_LIMIT
    assert store.iter_history(room_id=ROOM, limit=0)[0], "limit=0 应被夹到 1，而不是空页"


# --------------------------------------------------------------------------- #
# 过滤与时间窗
# --------------------------------------------------------------------------- #


def test_type_and_keyword_filters_apply(store: DanmakuStore) -> None:
    items, _, _ = store.iter_history(room_id=ROOM, limit=50, types=["super_chat"])
    assert items and all(item["type"] == "super_chat" for item in items)
    assert all(item["price"] == 30 for item in items)

    # 关键词是 `LIKE` 子串匹配（见 store.py docstring）。
    # 实测注意两点：① 「内容17」**不包含**子串「内容7」（中间隔着「容」），
    # 所以能跨 id 命中的是「内容1」这类前缀；② `limit` 限制的是"最新 N 条"，
    # 想覆盖全库就把 limit 放够，否则会误判成"没匹配到"。
    hit, _, _ = store.iter_history(room_id=ROOM, limit=60, keyword="内容1")
    assert [item["text"] for item in hit] == [
        "内容1", "内容10", "内容11", "内容12", "内容13", "内容14", "内容15",
        "内容16", "内容17", "内容18", "内容19",
    ], "子串匹配应命中多个（不是整句 token 相等）"

    # `limit` 是对**命中行**生效的（不是"先截最新 N 条再过滤"）：
    # 最新 10 条里一条都不含「内容1」，所以窗口会自动往更旧的时间戳延伸，
    # 直到凑满 10 条命中 —— 这正是翻页搜索想要的语义。
    windowed, _, _ = store.iter_history(room_id=ROOM, limit=10, keyword="内容1")
    assert len(windowed) == 10
    assert all("内容1" in (item["text"] or "") for item in windowed)
    assert [item["id"] for item in windowed] == [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
    baseline_newest, _, _ = store.iter_history(room_id=ROOM, limit=10)
    assert max(item["id"] for item in baseline_newest) == 60
    assert max(item["id"] for item in windowed) < 50, "窗口应已越过不含命中的最新区间"

    unique, _, _ = store.iter_history(room_id=ROOM, limit=60, keyword="内容60")
    assert [item["text"] for item in unique] == ["内容60"]

    # 关键词只作用于 text 列（不匹配 uname）
    assert store.iter_history(room_id=ROOM, limit=50, keyword="用户12")[0] == []


def test_session_filter_and_session_listing(tmp_path: Path) -> None:
    db = build_db(tmp_path, rows=6)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO session(id, room_id, started_at) VALUES (?,?,?)",
        ("s2", ROOM, 1_790_000_500.0),  # 晚于 s1，应排在前面
    )
    conn.commit()
    conn.close()

    opened = DanmakuStore(db)
    try:
        sessions = opened.sessions(limit=10, room_id=ROOM)
        assert [item["id"] for item in sessions] == ["s2", "s1"], "按开始时间倒序"
        assert sessions[0]["events"] == 0
        assert sessions[1]["events"] == 6
        assert opened.sessions(limit=10, room_id=OTHER_ROOM) == []

        items, _, _ = opened.iter_history(room_id=ROOM, session_id="s1", limit=50)
        assert len(items) == 6
        assert {item["session_id"] for item in items} == {"s1"}

        # 会话过滤也要能在游标分页上正确收窄（前端"按某次抓取浏览"就靠它）
        paged, has_older, _ = opened.iter_history(room_id=ROOM, session_id="s1", limit=2)
        assert len(paged) == 2 and has_older is True
        assert opened.iter_history(room_id=ROOM, session_id="s2", limit=5)[0] == []
    finally:
        opened.close()


def test_keyword_like_wildcards_are_escaped(store: DanmakuStore) -> None:
    """`%` / `_` 不能让关键词变成通配（沿用 `_build_where` 的转义）。"""
    items, _, _ = store.iter_history(room_id=ROOM, limit=50, keyword="%")
    assert items == [], "库里没有字面量 %，不该匹配全部"


def test_without_room_filter_needs_a_time_window(tmp_path: Path) -> None:
    """无房间过滤时用兜底时间窗，且锚点应能落在历史数据上（而不是墙钟）。"""
    db = build_db(tmp_path, rows=10)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO event (session_id, room_id, recv_ts, type, cmd, source, msg_id, dedupe_key)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("s1", OTHER_ROOM, 1_790_000_100.0, "danmaku", "DANMU_MSG", "live", "x1", "kx1"),
    )
    conn.commit()
    conn.close()

    opened = DanmakuStore(db)
    try:
        lo, hi = opened.time_range()
        # 锚在数据最新的时刻：两个房间的数据都该在窗内
        items, _, _ = opened.iter_history(limit=100, anchor_ts=hi, window_s=3600)
        assert {item["room_id"] for item in items} == {ROOM, OTHER_ROOM}

        # 锚错（用墙钟）时，老数据会落在窗外 —— 这正是要传 anchor_ts 的原因
        items_wrong, _, _ = opened.iter_history(limit=100, window_s=10)
        assert items_wrong == []
        assert lo is not None
    finally:
        opened.close()


def test_explicit_since_until_wins_over_default_window(tmp_path: Path) -> None:
    """显式给了 since/until 就不该再叠默认窗（否则会把要的数据切掉）。"""
    db = build_db(tmp_path, rows=10)
    opened = DanmakuStore(db)
    try:
        items, _, _ = opened.iter_history(limit=100, since=1_790_000_000.0)
        assert len(items) == 10, "显式时间过滤下不应被默认窗截断"
    finally:
        opened.close()


# --------------------------------------------------------------------------- #
# 密度聚合
# --------------------------------------------------------------------------- #


def test_density_buckets_counts_and_types(store: DanmakuStore) -> None:
    payload = store.density(room_id=ROOM, bucket_s=60.0)
    assert payload["bucket_s"] == 60.0
    assert payload["total"] == 60
    assert sum(bucket["count"] for bucket in payload["buckets"]) == 60
    assert sum(payload["by_type"].values()) == 60
    # 每个桶的 by_type 之和 = 该桶 count
    for bucket in payload["buckets"]:
        assert sum(bucket["by_type"].values()) == bucket["count"]
        assert bucket["ts"] % 60.0 == 0
    assert payload["range"]["lo"] is not None


def test_density_respects_type_filter(store: DanmakuStore) -> None:
    payload = store.density(room_id=ROOM, bucket_s=60.0, include_types=["super_chat"])
    assert set(payload["by_type"]) == {"super_chat"}
    assert payload["total"] == 60 // 7


def test_density_respects_time_bounds(store: DanmakuStore) -> None:
    everything = store.density(room_id=ROOM, bucket_s=60.0)
    narrowed = store.density(room_id=ROOM, bucket_s=60.0, until=1_790_000_000.0)
    assert narrowed["total"] < everything["total"]
    assert all(bucket["ts"] <= 1_790_000_000.0 for bucket in narrowed["buckets"])


def test_estimate_bucket_s_picks_readable_steps(store: DanmakuStore) -> None:
    assert store.estimate_bucket_s(0.0, 3600.0) == 30.0  # 1 小时 → 30 秒/桶
    assert store.estimate_bucket_s(0.0, 86_400.0) == 600.0  # 一天 → 10 分钟/桶
    assert store.estimate_bucket_s(None, None) == 60.0  # 没数据时给个默认
    assert store.estimate_bucket_s(5.0, 5.0) == 60.0
    assert store.estimate_bucket_s(0.0, 10_000_000.0) == 43_200.0  # 超过最大档就用最大档


def test_density_bucket_size_is_never_zero(store: DanmakuStore) -> None:
    payload = store.density(room_id=ROOM, bucket_s=0)
    assert payload["bucket_s"] > 0, "bucket_s=0 会导致除零"
