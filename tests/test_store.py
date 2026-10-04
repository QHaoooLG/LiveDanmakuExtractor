"""SQLite 读取层（stats / export）单测。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bili_danmaku.models import Medal, SessionMeta
from bili_danmaku.sinks import SqliteSink
from bili_danmaku.store import DanmakuStore, StoreError


def db_path(config) -> Path:
    configured = (config.sinks.sqlite_path or "").strip()
    return Path(configured) if configured else Path(config.sinks.out_dir) / "danmaku.db"


def populate(config, events, *, session_id: str = "s1", room_id: int = 804456) -> Path:
    sink = SqliteSink(config)
    sink.open(SessionMeta(session_id=session_id, room_id=room_id, started_at=1.0))
    sink.write(events)
    sink.close()
    return db_path(config)


def sample_events(make_event) -> list:
    """注意每条必须有**唯一 msg_id**，否则会被 dedupe_key 的 UNIQUE 约束合并。"""
    return [
        make_event(seq=1, msg_id="m1", type="danmaku", uid=1001, uname="甲", text="主播好", latency_ms=100),
        make_event(seq=2, msg_id="m2", type="danmaku", uid=1001, uname="甲", text="再来一个", latency_ms=200),
        make_event(seq=3, msg_id="m3", type="danmaku", uid=2002, uname="乙", text="哈哈", latency_ms=300),
        make_event(seq=4, msg_id="m4", type="gift", uid=2002, uname="乙", text="辣条", price=500, latency_ms=400),
        make_event(seq=5, msg_id="m5", type="super_chat", uid=3003, uname="丙", text="加油", price=30, latency_ms=500),
        make_event(seq=6, msg_id="m6", type="danmaku", uid=0, uname="宅***", text="打码用户", latency_ms=600),
    ]


# --------------------------------------------------------------------------- #
# 打开与容错
# --------------------------------------------------------------------------- #


def test_missing_db_raises_store_error(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="不存在"):
        DanmakuStore(tmp_path / "nope.db")


def test_fts_phrase_is_gone(make_config, make_event) -> None:
    """刻意不使用 FTS5（中文分词语义不符），关键词统一走 LIKE 子串匹配。"""
    import bili_danmaku.store as store_module

    assert not hasattr(store_module, "fts_phrase")


def test_store_is_read_only(make_config, make_event, tmp_path: Path) -> None:
    """分析操作不该能写坏采集数据。"""
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        with pytest.raises(sqlite3.OperationalError):
            store._conn.execute("DELETE FROM event")


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def test_count_and_by_type(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        assert store.count() == 6
        by_type = store.by_type()
        assert by_type["danmaku"] == 4
        assert by_type["gift"] == 1
        assert by_type["super_chat"] == 1


def test_filters_narrow_scope(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        assert store.count(types=["danmaku"]) == 4
        assert store.count(session_id="s1") == 6
        assert store.count(session_id="other") == 0
        assert store.count(room_id=804456) == 6
        assert store.count(room_id=1) == 0


def test_time_range(make_config, make_event) -> None:
    events = [
        make_event(seq=1, msg_id="a", recv_ts=1000.0),
        make_event(seq=2, msg_id="b", recv_ts=2000.0),
        make_event(seq=3, msg_id="c", recv_ts=1500.0),
    ]
    path = populate(make_config(sinks={"types": ["sqlite"]}), events)
    with DanmakuStore(path) as store:
        assert store.time_range() == (1000.0, 2000.0)
        assert store.time_range(since=1200.0) == (1500.0, 2000.0)
        assert store.time_range(until=1200.0) == (1000.0, 1000.0)


def test_latency_percentiles(make_config, make_event) -> None:
    events = [make_event(seq=i, msg_id=f"m{i}", latency_ms=i * 100) for i in range(1, 101)]
    path = populate(make_config(sinks={"types": ["sqlite"]}), events)
    with DanmakuStore(path) as store:
        p50, p95 = store.latency_percentiles()
        assert p50 is not None and p95 is not None
        assert p50 <= p95
        assert 4000 <= p50 <= 5200
        assert 9400 <= p95 <= 9600


def test_latency_percentiles_without_data(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), [make_event(latency_ms=None)])
    with DanmakuStore(path) as store:
        assert store.latency_percentiles() == (None, None)


def test_top_senders_ignores_masked(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        top = store.top_senders(limit=5)
        uids = [item["uid"] for item in top]
        assert uids[0] == 1001
        assert 0 not in uids, "uid=0 的打码记录不应出现在发言榜"
        assert top[0]["count"] == 2
        assert top[0]["uname"] == "甲"


def test_top_gifts_sums_by_type(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        priced = {item["type"]: item for item in store.top_gifts()}
        assert priced["gift"]["total_price"] == 500
        assert priced["gift"]["count"] == 1
        assert priced["super_chat"]["total_price"] == 30


def test_by_source(make_config, make_event) -> None:
    events = [
        make_event(seq=1, msg_id="a", source="live"),
        make_event(seq=2, msg_id="b", source="history"),
    ]
    path = populate(make_config(sinks={"types": ["sqlite"]}), events)
    with DanmakuStore(path) as store:
        assert store.by_source() == {"live": 1, "history": 1}


def test_sessions_lists_event_counts(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        sessions = store.sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"
        assert sessions[0]["events"] == 6


# --------------------------------------------------------------------------- #
# 流式读取（导出用）
# --------------------------------------------------------------------------- #


def test_iter_events_streams_in_order(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        events = list(store.iter_events(batch_size=2))
    assert [event.text for event in events] == [
        "主播好",
        "再来一个",
        "哈哈",
        "辣条",
        "加油",
        "打码用户",
    ]
    assert events[3].price == 500
    assert events[0].latency_ms == 100


def test_iter_events_respects_limit_and_types(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        assert len(list(store.iter_events(limit=2))) == 2
        only_gift = list(store.iter_events(types=["gift", "super_chat"]))
    assert {event.type for event in only_gift} == {"gift", "super_chat"}


def test_iter_events_restores_medal_and_raw(make_config, make_event) -> None:
    event = make_event(msg_id="only", medal=Medal(name="牌子", level=9), raw={"cmd": "DANMU_MSG"})
    path = populate(make_config(sinks={"types": ["sqlite"]}), [event])
    with DanmakuStore(path) as store:
        restored = next(store.iter_events())
    assert restored.medal is not None
    assert restored.medal.name == "牌子"
    assert restored.medal.level == 9
    assert restored.raw == {"cmd": "DANMU_MSG"}


def test_keyword_search_matches_substring(make_config, make_event) -> None:
    """中文子串必须能搜到（这是不用 FTS5 的核心原因）。"""
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        assert [event.text for event in store.iter_events(keyword="主播")] == ["主播好"]
        assert [event.text for event in store.iter_events(keyword="哈哈")] == ["哈哈"]
        assert list(store.iter_events(keyword="不存在的词")) == []


def test_keyword_search_escapes_like_wildcards(make_config, make_event) -> None:
    """用户输入的 % 和 _ 不应被当成 SQL 通配符。"""
    events = [
        make_event(seq=1, msg_id="a", text="100%好评"),
        make_event(seq=2, msg_id="b", text="100X好评"),
        make_event(seq=3, msg_id="c", text="a_b"),
        make_event(seq=4, msg_id="d", text="axb"),
    ]
    path = populate(make_config(sinks={"types": ["sqlite"]}), events)
    with DanmakuStore(path) as store:
        assert [event.text for event in store.iter_events(keyword="100%")] == ["100%好评"]
        assert [event.text for event in store.iter_events(keyword="a_b")] == ["a_b"]


def test_keyword_search_is_not_sql_injectable(make_config, make_event) -> None:
    path = populate(make_config(sinks={"types": ["sqlite"]}), sample_events(make_event))
    with DanmakuStore(path) as store:
        # 恶意输入不应报错、也不应匹配到任何东西
        assert list(store.iter_events(keyword="'; DROP TABLE event; --")) == []
        assert store.count() == 6
