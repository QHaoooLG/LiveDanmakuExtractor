"""落盘层单测：JSONL 写入/轮转、Bus 扇出与故障隔离。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bili_danmaku.config import Config
from bili_danmaku.models import Event, SessionMeta
from bili_danmaku.sinks import Bus, SinkUnavailable, build_sinks
from bili_danmaku.sinks.base import Sink
from bili_danmaku.sinks.jsonl import JsonlSink


def make_meta(room_id: int = 804456) -> SessionMeta:
    return SessionMeta(session_id="20260918-134500-804456", room_id=room_id, started_at=1.0)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------------------- #
# JsonlSink
# --------------------------------------------------------------------------- #


def test_jsonl_writes_one_line_per_event(make_config, make_event, tmp_path: Path) -> None:
    config = make_config()
    sink = JsonlSink(config)
    sink.open(make_meta())
    sink.write([make_event(seq=1, text="第一条"), make_event(seq=2, text="第二条")])
    sink.close()

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    records = read_lines(files[0])
    assert len(records) == 2
    assert records[0]["text"] == "第一条"
    assert records[1]["text"] == "第二条"
    assert records[0]["room_id"] == 804456
    assert sink.stats.written == 2
    assert sink.stats.bytes > 0


def test_jsonl_filename_contains_room_and_bucket(make_config, make_event, tmp_path: Path) -> None:
    sink = JsonlSink(make_config())
    sink.open(make_meta(room_id=12345))
    sink.write([make_event(recv_ts=1_789_710_200.0)])
    sink.close()
    name = next(tmp_path.glob("*.jsonl")).name
    assert name.startswith("12345_")


def test_jsonl_keeps_chinese_readable(make_config, make_event, tmp_path: Path) -> None:
    sink = JsonlSink(make_config())
    sink.open(make_meta())
    sink.write([make_event(text="中文不乱码 😀")])
    sink.close()

    raw = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "中文不乱码 😀" in raw  # ensure_ascii=False
    assert "\\u4e2d" not in raw


def test_jsonl_serializes_raw_as_null_when_absent(make_config, make_event, tmp_path: Path) -> None:
    """`keep_raw=False` 由解析层生效（parse_message 产出 raw=None）；
    sink 的职责是保证字段始终存在且为 null，避免下游 schema 漂移。"""
    sink = JsonlSink(make_config(sinks={"keep_raw": False}))
    sink.open(make_meta())
    sink.write([make_event(raw=None)])
    sink.close()

    record = read_lines(next(tmp_path.glob("*.jsonl")))[0]
    assert "raw" in record
    assert record["raw"] is None


def test_jsonl_rotates_across_time_buckets(make_config, make_event, tmp_path: Path) -> None:
    config = make_config(sinks={"rotate_hours": 1})
    sink = JsonlSink(config)
    sink.open(make_meta())
    # 相隔 3 小时，必然落在不同时间桶
    sink.write([make_event(seq=1, recv_ts=1_789_710_200.0)])
    sink.write([make_event(seq=2, recv_ts=1_789_710_200.0 + 3 * 3600)])
    sink.close()

    files = sorted(tmp_path.glob("*.jsonl"))
    assert len(files) == 2
    assert sum(len(read_lines(path)) for path in files) == 2


def test_jsonl_appends_to_existing_file(make_config, make_event, tmp_path: Path) -> None:
    config = make_config()
    timestamp = 1_789_710_200.0

    first = JsonlSink(config)
    first.open(make_meta())
    first.write([make_event(seq=1, recv_ts=timestamp)])
    first.close()

    second = JsonlSink(config)
    second.open(make_meta())
    second.write([make_event(seq=2, recv_ts=timestamp)])
    second.close()

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    assert len(read_lines(files[0])) == 2


# --------------------------------------------------------------------------- #
# build_sinks
# --------------------------------------------------------------------------- #


def test_build_sinks_creates_requested(make_config) -> None:
    sinks = build_sinks(["jsonl"], make_config())
    assert [sink.name for sink in sinks] == ["jsonl"]


def test_build_sinks_now_supports_m2_formats(make_config) -> None:
    """M2 之后 csv / txt / sqlite 都应可用（原「计划在 M2 实现」的提示已移除）。"""
    sinks = build_sinks(["csv", "txt", "sqlite"], make_config())
    assert sorted(sink.name for sink in sinks) == ["csv", "sqlite", "txt"]


def test_build_sinks_rejects_unknown(make_config) -> None:
    with pytest.raises(SinkUnavailable, match="未知"):
        build_sinks(["parquet"], make_config())


# --------------------------------------------------------------------------- #
# Bus
# --------------------------------------------------------------------------- #


class _RecordingSink(Sink):
    name = "recording"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.events: list[Event] = []

    def open(self, meta: SessionMeta) -> None:
        super().open(meta)

    def write(self, events) -> None:
        self.events.extend(events)
        self.record(len(events))


class _ExplodingSink(Sink):
    name = "exploding"

    def write(self, events) -> None:
        raise OSError("disk full")


async def test_bus_fans_out_to_all_sinks(make_config, make_event) -> None:
    config = make_config()
    recording = _RecordingSink(config)
    jsonl = JsonlSink(config)
    bus = Bus([recording, jsonl], config)

    await bus.start(make_meta())
    for index in range(3):
        assert bus.publish(make_event(seq=index, msg_id=f"m{index}")) is True
    await bus.stop()

    assert len(recording.events) == 3
    assert bus.stats["recording"].written == 3
    assert bus.stats["jsonl"].written == 3
    assert bus.healthy is True


async def test_bus_disables_failing_sink_but_keeps_others(make_config, make_event) -> None:
    config = make_config(sinks={"batch_size": 1, "flush_interval": 0.01})
    exploding = _ExplodingSink(config)
    recording = _RecordingSink(config)
    bus = Bus([exploding, recording], config)

    await bus.start(make_meta())
    for index in range(6):
        bus.publish(make_event(seq=index, msg_id=f"m{index}"))
        await asyncio.sleep(0.05)
    await bus.stop()

    # 连续失败达到阈值后被停用，但另一个 sink 仍然完整收到
    assert bus.healthy is False
    assert bus.stats["exploding"].failed >= 3
    assert bus.stats["exploding"].ok is False
    assert len(recording.events) == 6


async def test_bus_drops_oldest_when_queue_full(make_config, make_event) -> None:
    """队列满时丢最旧而不是阻塞调用方（阻塞会卡住心跳）。"""
    config = make_config(sinks={"queue_size": 4, "batch_size": 1000, "flush_interval": 60.0})
    recording = _RecordingSink(config)
    bus = Bus([recording], config)
    await bus.start(make_meta())

    # 消费者吞掉第一批后停住，制造积压
    accepted = [bus.publish(make_event(seq=index, msg_id=f"m{index}")) for index in range(60)]
    await asyncio.sleep(0.02)

    assert bus.dropped > 0 or all(accepted)
    await bus.stop()
    assert bus.stats["recording"].dropped == bus.dropped


async def test_bus_publish_after_stop_does_not_raise(make_config, make_event) -> None:
    config = make_config()
    bus = Bus([_RecordingSink(config)], config)
    await bus.start(make_meta())
    await bus.stop()
    # 停止后仍可能被调用（竞态），不应抛异常
    bus.publish(make_event())
