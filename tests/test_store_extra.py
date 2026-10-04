"""V2 新增：`extra_json` 的落盘/读取，以及旧库（schema v1）的兼容。

为什么单独立一个文件：`extra_json` 是 V2 词云要用的"表情占位符 → 图片"映射，
而它牵扯到三处必须同时正确 —— 写入端（`SqliteSink`）、读取端（`DanmakuStore`）、
以及**旧库没有这一列**时的降级。这里逐条钉死。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bili_danmaku.config import Config
from bili_danmaku.models import Event, SessionMeta
from bili_danmaku.sinks import SqliteSink
from bili_danmaku.sinks.sqlite import MIGRATIONS, SCHEMA_VERSION
from bili_danmaku.store import DanmakuStore

ROOM = 804456
EMOTS = {
    "[dog]": {
        "url": "https://i0.hdslb.com/bfs/live/dog.png",
        "width": 20,
        "height": 20,
        "emoticon_id": 1,
        "emoticon_unique": "room_1_1",
    }
}

#: schema v1（没有 extra_json）—— 用来验证"老数据 + 新代码"
LEGACY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE session (
    id TEXT PRIMARY KEY, room_id INTEGER NOT NULL, short_id INTEGER, anchor_uid INTEGER,
    token_source TEXT, started_at REAL NOT NULL, ended_at REAL, note TEXT
);
CREATE TABLE event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, room_id INTEGER NOT NULL, recv_ts REAL NOT NULL,
    server_ts INTEGER, latency_ms INTEGER, type TEXT NOT NULL, cmd TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'live', msg_id TEXT, uid INTEGER, uname TEXT, text TEXT,
    price INTEGER, guard_level INTEGER, user_level INTEGER, medal_name TEXT,
    medal_level INTEGER, field_source TEXT, dedupe_key TEXT UNIQUE, raw_json TEXT
);
"""


def make_event(
    seq: int,
    *,
    text: str,
    extra: dict | None = None,
    msg_id: str | None = None,
    raw: dict | None = None,
) -> Event:
    return Event(
        seq=seq,
        session_id="s",
        room_id=ROOM,
        type="danmaku",
        cmd="DANMU_MSG",
        recv_ts=1_789_710_200.0 + seq,
        server_ts=1_789_710_200,
        msg_id=msg_id or f"m{seq}",
        uid=1001,
        uname="甲",
        text=text,
        extra=extra or {},
        raw=raw,
    )


def config_for(tmp_path: Path, **sinks) -> Config:
    data = {"out_dir": str(tmp_path), "types": ["sqlite"], "keep_raw": False}
    data.update(sinks)
    return Config.load(path="missing.toml", overrides={"sinks": data})


def populate(tmp_path: Path, events: list[Event], config: Config | None = None) -> Path:
    sink = SqliteSink(config or config_for(tmp_path))
    sink.open(SessionMeta(session_id="20260918-141801-804456", room_id=ROOM, started_at=1.0))
    sink.write(events)
    sink.close()
    return tmp_path / "danmaku.db"


def make_legacy_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', '1')"
        )
        connection.execute(
            """
            INSERT INTO session (id, room_id, started_at)
            VALUES ('legacy-session', ?, 1.0)
            """,
            (ROOM,),
        )
        connection.execute(
            """
            INSERT INTO event (session_id, room_id, recv_ts, type, cmd, source, msg_id, uid,
                               uname, text, dedupe_key)
            VALUES ('legacy-session', ?, 1.0, 'danmaku', 'DANMU_MSG', 'live', 'old-1', 1001,
                    '甲', '老库里的弹幕', 'danmaku|1|old-1')
            """,
            (ROOM,),
        )
        connection.commit()
    finally:
        connection.close()
    return path


# --------------------------------------------------------------------------- #
# 写入 / 读取
# --------------------------------------------------------------------------- #


def test_extra_is_persisted_and_read_back(tmp_path: Path) -> None:
    event = make_event(1, text="哈哈[dog]", extra={"emots": EMOTS, "has_emoticon": True})
    db = populate(tmp_path, [event])

    connection = sqlite3.connect(db)
    try:
        stored = connection.execute("SELECT extra_json FROM event").fetchone()[0]
    finally:
        connection.close()
    assert json.loads(stored)["emots"] == EMOTS

    store = DanmakuStore(db)
    try:
        (loaded,) = list(store.iter_events())
        assert loaded.extra["emots"] == EMOTS
        assert loaded.extra["has_emoticon"] is True
    finally:
        store.close()


def test_extra_persists_even_without_raw(tmp_path: Path) -> None:
    """`--no-raw` 时 raw 不落盘，但 extra 必须还在（词云不依赖 raw）。"""
    db = populate(
        tmp_path,
        [make_event(1, text="哈哈[dog]", extra={"emots": EMOTS}, raw=None)],
        config_for(tmp_path, keep_raw=False),
    )
    store = DanmakuStore(db)
    try:
        (loaded,) = list(store.iter_events())
        assert loaded.raw is None
        assert loaded.extra["emots"] == EMOTS
    finally:
        store.close()


def test_empty_extra_is_not_stored(tmp_path: Path) -> None:
    db = populate(tmp_path, [make_event(1, text="没有表情")])
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT extra_json FROM event").fetchone()[0] is None
    finally:
        connection.close()


def test_iter_texts_yields_type_text_extra(tmp_path: Path) -> None:
    db = populate(
        tmp_path,
        [
            make_event(1, text="哈哈[dog]", extra={"emots": EMOTS}),
            make_event(2, text="普通弹幕"),
        ],
    )
    store = DanmakuStore(db)
    try:
        rows = list(store.iter_texts())
    finally:
        store.close()

    assert [text for _type, text, _extra in rows] == ["哈哈[dog]", "普通弹幕"]
    assert rows[0][2]["emots"] == EMOTS
    assert rows[1][2] == {}


def test_iter_texts_supports_limit_and_type_filter(tmp_path: Path) -> None:
    events = [make_event(index, text=f"第{index}条") for index in range(1, 5)]
    events.append(
        Event(
            seq=9, session_id="s", room_id=ROOM, type="super_chat", cmd="SUPER_CHAT_MESSAGE",
            recv_ts=1_789_710_300.0, msg_id="sc-1", text="SC 正文", price=30000,
        )
    )
    db = populate(tmp_path, events)

    store = DanmakuStore(db)
    try:
        assert len(list(store.iter_texts(limit=2))) == 2
        only_sc = list(store.iter_texts(types=["super_chat"]))
        assert [text for _t, text, _e in only_sc] == ["SC 正文"]
        by_room = list(store.iter_texts(room_id=ROOM))
        assert len(by_room) == 5
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 旧库兼容
# --------------------------------------------------------------------------- #


def test_has_column_detects_missing_extra_json(tmp_path: Path) -> None:
    legacy = make_legacy_db(tmp_path / "legacy.db")
    store = DanmakuStore(legacy)
    try:
        assert store.has_column("extra_json") is False
        assert store.has_column("text") is True
    finally:
        store.close()


def test_iter_texts_works_on_legacy_db(tmp_path: Path) -> None:
    """旧库没有 extra_json：用 NULL 占位，分词照常，只是没有表情元数据。"""
    legacy = make_legacy_db(tmp_path / "legacy.db")
    store = DanmakuStore(legacy)
    try:
        rows = list(store.iter_texts())
    finally:
        store.close()
    assert rows == [("danmaku", "老库里的弹幕", {})]


def test_iter_events_works_on_legacy_db(tmp_path: Path) -> None:
    """旧库上 `iter_events` 也必须能跑（导出/面板都走它）。"""
    legacy = make_legacy_db(tmp_path / "legacy.db")
    store = DanmakuStore(legacy)
    try:
        (loaded,) = list(store.iter_events())
    finally:
        store.close()
    assert loaded.text == "老库里的弹幕"
    assert loaded.extra == {}


def test_sqlite_sink_migrates_legacy_db(tmp_path: Path) -> None:
    """用新代码继续往旧库写：SqliteSink 必须自动 ALTER 补列，而不是报 no such column。"""
    legacy = make_legacy_db(tmp_path / "danmaku.db")

    sink = SqliteSink(config_for(tmp_path))
    sink.open(SessionMeta(session_id="new-session", room_id=ROOM, started_at=1.0))
    sink.write([make_event(1, text="新库写入", extra={"emots": EMOTS})])
    sink.close()

    connection = sqlite3.connect(legacy)
    try:
        version = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        rows = connection.execute("SELECT text, extra_json FROM event ORDER BY id").fetchall()
    finally:
        connection.close()

    assert version == SCHEMA_VERSION
    assert len(rows) == 2, "旧数据要保留，新数据要能写进去"
    assert rows[0][0] == "老库里的弹幕"
    assert json.loads(rows[1][1])["emots"] == EMOTS

    store = DanmakuStore(legacy)
    try:
        assert store.has_column("extra_json") is True
        texts = [text for _t, text, _e in store.iter_texts()]
    finally:
        store.close()
    assert texts == ["老库里的弹幕", "新库写入"]


def test_migration_list_matches_schema_expectation() -> None:
    """迁移清单的列必须真的在 SCHEMA 里声明过，否则迁移是死代码。"""
    from bili_danmaku.sinks import sqlite as module

    for column, statement in MIGRATIONS:
        assert f"    {column} " in module.SCHEMA, f"{column} 未出现在 SCHEMA 中"
        assert statement.startswith("ALTER TABLE event ADD COLUMN"), statement


@pytest.mark.parametrize("keep_raw", [True, False])
def test_export_round_trip_keeps_extra(tmp_path: Path, keep_raw: bool) -> None:
    """导出 JSONL 再回放，extra（表情映射）不能丢 —— 词云要能在导出文件上跑。"""
    from bili_danmaku.cli import _write_records
    from bili_danmaku.models import Event as Model

    db = populate(
        tmp_path,
        [make_event(1, text="哈哈[dog]", extra={"emots": EMOTS})],
        config_for(tmp_path, keep_raw=keep_raw),
    )
    store = DanmakuStore(db)
    try:
        out = tmp_path / "out.jsonl"
        with out.open("w", encoding="utf-8") as handle:
            _write_records(handle, store.iter_events(), "jsonl")
    finally:
        store.close()

    payload = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert payload["extra"]["emots"] == EMOTS
    replayed = Model.model_validate(payload)
    assert replayed.extra["emots"] == EMOTS
