"""M2 落盘层单测：CSV / TXT / SQLite。"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from bili_danmaku.models import Medal, SessionMeta
from bili_danmaku.sinks import CSV_COLUMNS, CsvSink, SqliteSink, TxtSink, build_sinks
from bili_danmaku.sinks.csv_txt import sanitize_cell
from bili_danmaku.sinks.sqlite import SCHEMA_VERSION

from test_sinks import make_meta


def meta(room_id: int = 804456) -> SessionMeta:
    return SessionMeta(session_id="20260918-150000-804456", room_id=room_id, started_at=1.0)


# --------------------------------------------------------------------------- #
# CSV 公式注入防护
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["=1+1", "+A1", "-2", "@SUM(A1)"])
def test_sanitize_cell_prefixes_formula(raw: str) -> None:
    assert sanitize_cell(raw) == "'" + raw


def test_sanitize_cell_leaves_normal_text() -> None:
    assert sanitize_cell("你好") == "你好"
    assert sanitize_cell(None) == ""
    assert sanitize_cell(42) == "42"


# --------------------------------------------------------------------------- #
# CsvSink
# --------------------------------------------------------------------------- #


def test_csv_writes_header_and_rows(make_config, make_event, tmp_path: Path) -> None:
    sink = CsvSink(make_config(sinks={"types": ["csv"]}))
    sink.open(meta())
    sink.write(
        [
            make_event(seq=1, type="danmaku", text="第一条", medal=Medal(level=12)),
            make_event(seq=2, type="gift", text="辣条", price=500),
        ]
    )
    sink.close()

    path = next(tmp_path.glob("*.csv"))
    raw = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(raw.splitlines()))
    assert rows[0] == list(CSV_COLUMNS)
    assert len(rows) == 3
    assert rows[1][1] == "danmaku"
    assert rows[1][4] == "第一条"
    assert rows[1][7] == "12"  # medal_level
    assert rows[2][5] == "500"  # price
    assert sink.stats.written == 2


def test_csv_starts_with_bom_for_excel(make_config, make_event, tmp_path: Path) -> None:
    sink = CsvSink(make_config(sinks={"types": ["csv"]}))
    sink.open(meta())
    sink.write([make_event()])
    sink.close()
    assert next(tmp_path.glob("*.csv")).read_bytes().startswith(b"\xef\xbb\xbf")


def test_csv_protects_against_formula_injection(make_config, make_event, tmp_path: Path) -> None:
    sink = CsvSink(make_config(sinks={"types": ["csv"]}))
    sink.open(meta())
    sink.write([make_event(text="=cmd|'/c calc'!A1", uname="@evil")])
    sink.close()

    rows = list(csv.reader(next(tmp_path.glob("*.csv")).read_text(encoding="utf-8-sig").splitlines()))
    assert rows[1][4].startswith("'=")
    assert rows[1][3].startswith("'@")


def test_csv_rotates_across_buckets(make_config, make_event, tmp_path: Path) -> None:
    sink = CsvSink(make_config(sinks={"types": ["csv"], "rotate_hours": 1}))
    sink.open(meta())
    sink.write([make_event(seq=1, recv_ts=1_789_710_200.0)])
    sink.write([make_event(seq=2, recv_ts=1_789_710_200.0 + 3 * 3600)])
    sink.close()
    assert len(list(tmp_path.glob("*.csv"))) == 2


# --------------------------------------------------------------------------- #
# TxtSink
# --------------------------------------------------------------------------- #


def test_txt_writes_only_danmaku(make_config, make_event, tmp_path: Path) -> None:
    sink = TxtSink(make_config(sinks={"types": ["txt"]}))
    sink.open(meta())
    sink.write(
        [
            make_event(seq=1, type="danmaku", text="你好", uname="甲"),
            make_event(seq=2, type="gift", text="辣条"),
            make_event(seq=3, type="danmaku", text="世界", uname="乙"),
        ]
    )
    sink.close()

    lines = next(tmp_path.glob("*.txt")).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("甲: 你好")
    assert lines[1].endswith("乙: 世界")
    assert lines[0].startswith("[")  # [HH:MM:SS]
    assert sink.skipped == 1
    assert sink.stats.written == 2


def test_txt_skips_empty_text(make_config, make_event, tmp_path: Path) -> None:
    sink = TxtSink(make_config(sinks={"types": ["txt"]}))
    sink.open(meta())
    sink.write([make_event(type="danmaku", text=None)])
    sink.close()
    assert sink.stats.written == 0


def test_txt_flattens_newlines(make_config, make_event, tmp_path: Path) -> None:
    sink = TxtSink(make_config(sinks={"types": ["txt"]}))
    sink.open(meta())
    sink.write([make_event(type="danmaku", text="第一行\n第二行")])
    sink.close()
    content = next(tmp_path.glob("*.txt")).read_text(encoding="utf-8")
    assert content.count("\n") == 1, "换行必须被压平，否则一行弹幕会裂成两条"


def test_txt_uses_placeholder_for_missing_uname(make_config, make_event, tmp_path: Path) -> None:
    sink = TxtSink(make_config(sinks={"types": ["txt"]}))
    sink.open(meta())
    sink.write([make_event(type="danmaku", uname=None, text="嗨")])
    sink.close()
    assert "匿名: 嗨" in next(tmp_path.glob("*.txt")).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# SqliteSink
# --------------------------------------------------------------------------- #


def _populate(config, events, *, session_id: str = "20260918-150000-804456") -> SqliteSink:
    sink = SqliteSink(config)
    sink.open(SessionMeta(session_id=session_id, room_id=804456, started_at=1.0))
    sink.write(events)
    sink.close()
    return sink


def test_sqlite_creates_schema_and_rows(make_config, make_event, tmp_path: Path) -> None:
    sink = _populate(
        make_config(sinks={"types": ["sqlite"]}),
        [make_event(seq=1, msg_id="a"), make_event(seq=2, msg_id="b")],
    )
    db = tmp_path / "danmaku.db"
    assert db.is_file()

    connection = sqlite3.connect(db)
    try:
        rows = connection.execute("SELECT text, type, room_id FROM event ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0][1] == "danmaku"
        assert rows[0][2] == 804456
        version = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert version[0] == SCHEMA_VERSION, "写入的版本号应跟代码常量一致"
        session = connection.execute("SELECT room_id, ended_at FROM session").fetchone()
        assert session[0] == 804456
        assert session[1] is not None, "关闭时应回填 ended_at"
    finally:
        connection.close()
    assert sink.stats.written == 2


def test_sqlite_dedupe_key_is_unique(make_config, make_event, tmp_path: Path) -> None:
    """内存去重之外的第二道防线：同一 dedupe_key 第二次写入应被忽略。"""
    config = make_config(sinks={"types": ["sqlite"]})
    event = make_event(msg_id="same-id", text="重复")

    sink = SqliteSink(config)
    sink.open(meta())
    sink.write([event, event])
    sink.close()

    connection = sqlite3.connect(tmp_path / "danmaku.db")
    try:
        count = connection.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    finally:
        connection.close()
    assert count == 1
    assert sink.ignored == 1
    assert sink.stats.written == 1


def test_sqlite_stores_raw_only_when_enabled(make_config, make_event, tmp_path: Path) -> None:
    with_raw = _populate(
        make_config(sinks={"types": ["sqlite"], "keep_raw": True}),
        [make_event(msg_id="raw-1", raw={"cmd": "DANMU_MSG"})],
    )
    assert with_raw.stats.written == 1
    connection = sqlite3.connect(tmp_path / "danmaku.db")
    try:
        stored = connection.execute("SELECT raw_json FROM event").fetchone()[0]
    finally:
        connection.close()
    assert json.loads(stored)["cmd"] == "DANMU_MSG"


def test_sqlite_no_raw_when_disabled(make_config, make_event, tmp_path: Path) -> None:
    _populate(
        make_config(sinks={"types": ["sqlite"], "keep_raw": False}),
        [make_event(msg_id="raw-2", raw={"cmd": "DANMU_MSG"})],
    )
    connection = sqlite3.connect(tmp_path / "danmaku.db")
    try:
        assert connection.execute("SELECT raw_json FROM event").fetchone()[0] is None
    finally:
        connection.close()


def test_sqlite_respects_custom_path(make_config, make_event, tmp_path: Path) -> None:
    custom = tmp_path / "nested" / "custom.db"
    _populate(make_config(sinks={"types": ["sqlite"], "sqlite_path": str(custom)}), [make_event()])
    assert custom.is_file()


def test_sqlite_uses_wal_mode(make_config, make_event, tmp_path: Path) -> None:
    _populate(make_config(sinks={"types": ["sqlite"]}), [make_event()])
    connection = sqlite3.connect(tmp_path / "danmaku.db")
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "wal"


def test_sqlite_write_failure_propagates(
    make_config, make_event, tmp_path: Path, monkeypatch
) -> None:
    """写入失败必须抛出去，让 Bus 能发现并降级，而不是静默丢数据。"""
    config = make_config(sinks={"types": ["sqlite"]})
    sink = SqliteSink(config)
    sink.open(meta())

    # 构造一个绑定参数数量不符的行，触发 sqlite3.Error
    monkeypatch.setattr(sink, "_row", lambda event: ("too", "few"))
    with pytest.raises(sqlite3.Error):
        sink.write([make_event(msg_id="boom")])

    monkeypatch.undo()
    sink.close()


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


def test_registry_contains_all_m2_sinks(make_config) -> None:
    sinks = build_sinks(["jsonl", "csv", "txt", "sqlite"], make_config())
    assert sorted(sink.name for sink in sinks) == ["csv", "jsonl", "sqlite", "txt"]


def test_registry_deduplicates_names(make_config) -> None:
    assert len(build_sinks(["csv", "csv"], make_config())) == 1
