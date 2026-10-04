"""采集器集成测试：用假服务端驱动完整的连接 / 重连 / 落盘链路。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bili_danmaku.collector import RoomCollector
from bili_danmaku.credentials import Credentials
from bili_danmaku.models import Event, HostInfo, RoomInfo, SessionMeta, TokenBundle
from bili_danmaku.sinks import Bus, Sink
from bili_danmaku.sinks.jsonl import JsonlSink

from fake_server import FakeDanmakuServer

ROOM_ID = 804456


def danmaku(
    text: str,
    msg_id: str,
    *,
    uid: int = 1001,
    uname: str = "观众甲",
    ts: int = 1_789_710_200,
) -> dict:
    """构造一条结构接近实测样本的 DANMU_MSG。"""
    extra = json.dumps(
        {"content": text, "id_str": msg_id, "color": 16777215, "font_size": 25},
        ensure_ascii=False,
    )
    user = {"uid": uid, "base": {"name": uname}, "wealth": {"level": 5}}
    return {
        "cmd": "DANMU_MSG",
        "info": [
            [
                0, 1, 25, 16777215, ts * 1000, 0, 0, "abcd1234", 0, 0, 0, "", 0, "{}", "{}",
                {"extra": extra, "user": user},
            ],
            text,
            [uid, uname, 0, 0, 0, 10000, 1, ""],
            [],
            [11, 0, 6406234, ">50000", 0],
            ["", ""],
            0, 0, None, {"ct": "CT", "ts": ts}, 0, 0, None, None, 0, 390, [5], None,
        ],
    }


class FakeApi:
    """替代 BiliApi，让测试完全不联网。"""

    def __init__(self, hosts: list[HostInfo], *, history_records: list[dict] | None = None) -> None:
        self.hosts = hosts
        self.history_records = history_records or []
        self.token_calls = 0

    async def room_init(self, room):
        return RoomInfo(room_id=ROOM_ID, anchor_uid=8431064, live_status=1)

    async def acquire_token(self, room_id: int) -> TokenBundle:
        self.token_calls += 1
        return TokenBundle(token="fake-token", hosts=self.hosts, source="getConf")

    async def history(self, room_id: int) -> list[dict]:
        return self.history_records


def build(config, server: FakeDanmakuServer, **kwargs):
    api = FakeApi([HostInfo(host="127.0.0.1", ws_port=server.ws_port)])
    sink = JsonlSink(config)
    bus = Bus([sink], config)
    collector = RoomCollector(ROOM_ID, config, Credentials(), api, bus, **kwargs)
    return collector, api, sink, bus


def fast_config(make_config, **client_overrides):
    client = {
        "heartbeat_interval": 0.3,
        "heartbeat_timeout": 1.0,
        "recv_timeout": 0.2,
        "reconnect_max_delay": 0.3,
    }
    client.update(client_overrides)
    return make_config(
        client=client,
        sinks={"batch_size": 1, "flush_interval": 0.02, "queue_size": 100},
    )


def read_all(tmp_path: Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted(tmp_path.glob("*.jsonl")):
        records.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        )
    return records


# --------------------------------------------------------------------------- #
# 基本抓取
# --------------------------------------------------------------------------- #


async def test_collector_writes_danmaku_to_jsonl(make_config, tmp_path: Path) -> None:
    config = fast_config(make_config)
    messages = [danmaku("你好", "m1"), danmaku("世界", "m2")]

    async with FakeDanmakuServer(notifications=messages) as server:
        collector, api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=1.2)

    assert stats.parsed == 2, stats.as_dict()
    assert api.token_calls == 1
    assert server.auth_count == 1

    records = read_all(tmp_path)
    assert len(records) == 2
    assert {record["text"] for record in records} == {"你好", "世界"}
    assert records[0]["room_id"] == ROOM_ID
    assert records[0]["type"] == "danmaku"
    assert records[0]["session_id"].endswith(str(ROOM_ID))
    # 序列号应递增且唯一
    assert {record["seq"] for record in records} == {1, 2}


async def test_collector_sends_auth_payload_with_token(make_config) -> None:
    config = fast_config(make_config)
    async with FakeDanmakuServer(notifications=[danmaku("x", "m1")]) as server:
        collector, _api, _sink, _bus = build(config, server)
        await collector.run(duration=0.8)

    assert server.auth_payloads, "服务端应收到认证包"
    auth = server.auth_payloads[0]
    assert auth["roomid"] == ROOM_ID
    assert auth["key"] == "fake-token"
    assert auth["platform"] == "web"
    assert auth["protover"] in (2, 3)


async def test_collector_dedupes_same_msg_id(make_config, tmp_path: Path) -> None:
    """同一 msg_id 重复下发（含重连后的重放）只应入库一次。"""
    config = fast_config(make_config)
    messages = [danmaku("重复内容", "same-id"), danmaku("重复内容", "same-id")]

    async with FakeDanmakuServer(notifications=messages) as server:
        collector, _api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=1.0)

    assert stats.received == 2
    assert stats.duplicates == 1
    assert stats.parsed == 1
    assert len(read_all(tmp_path)) == 1


async def test_collector_keeps_distinct_msg_ids(make_config, tmp_path: Path) -> None:
    config = fast_config(make_config)
    messages = [danmaku("同样的话", "id-a"), danmaku("同样的话", "id-b")]

    async with FakeDanmakuServer(notifications=messages) as server:
        collector, _api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=1.0)

    assert stats.parsed == 2
    assert stats.duplicates == 0
    assert len(read_all(tmp_path)) == 2


async def test_collector_records_popularity_from_heartbeat(make_config) -> None:
    config = fast_config(make_config)
    async with FakeDanmakuServer(notifications=[danmaku("x", "m1")], popularity=2466) as server:
        collector, _api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=1.0)

    assert server.heartbeats >= 1
    assert stats.last_popularity == 2466


# --------------------------------------------------------------------------- #
# 重连
# --------------------------------------------------------------------------- #


async def test_collector_reconnects_after_server_drop(
    make_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "bili_danmaku.collector.backoff_delay", lambda *a, **k: 0.05
    )
    config = fast_config(make_config)
    messages = [danmaku("第一条", "m1"), danmaku("第二条", "m2")]

    async with FakeDanmakuServer(notifications=messages, drop_after=1) as server:
        collector, api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=2.5)

    assert stats.reconnects >= 1, stats.as_dict()
    assert server.auth_count >= 2, "重连后应重新认证"
    assert api.token_calls >= 2, "每次重连都必须重新获取 token"
    # 服务端重放同一条消息，去重必须生效
    texts = [record["text"] for record in read_all(tmp_path)]
    assert texts.count("第一条") == 1


async def test_collector_reacquires_token_on_every_reconnect(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """token 会过期，每次重连都必须重新获取，不能复用。"""
    monkeypatch.setattr("bili_danmaku.collector.backoff_delay", lambda *a, **k: 0.05)
    config = fast_config(make_config)

    async with FakeDanmakuServer(notifications=[danmaku("x", "m1")], drop_after=1) as server:
        collector, api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=1.2)

    assert stats.reconnects >= 1
    # 每一次重连都消耗了一个新 token（因此获取次数不会少于重连次数）
    assert api.token_calls >= stats.reconnects
    assert api.token_calls >= 2


async def test_collector_recovers_from_transient_auth_failure(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前两次认证失败，之后成功 —— 不应永久卡死。"""
    monkeypatch.setattr("bili_danmaku.collector.backoff_delay", lambda *a, **k: 0.05)
    config = fast_config(make_config)

    async with FakeDanmakuServer(
        notifications=[danmaku("恢复后收到", "m1")], auth_fail_times=2, auth_code=-101
    ) as server:
        collector, _api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=3.0)

    assert stats.auth_failures >= 2
    assert server.auth_count >= 3
    assert any(record["text"] == "恢复后收到" for record in read_all_holder(collector))


def read_all_holder(collector: RoomCollector) -> list[dict]:
    """从 collector 关联的 sink 目录读取记录（便于断言）。"""
    return read_all(Path(collector.config.sinks.out_dir))


async def test_collector_detects_heartbeat_silence(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务端不回心跳时应判假死并重连，而不是永久干等。"""
    monkeypatch.setattr("bili_danmaku.collector.backoff_delay", lambda *a, **k: 0.05)
    config = fast_config(make_config, heartbeat_interval=0.2, heartbeat_timeout=0.6)

    async with FakeDanmakuServer(notifications=[], silent_heartbeat=True) as server:
        collector, _api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=2.0)

    assert stats.reconnects >= 1, stats.as_dict()
    assert server.heartbeats >= 1, "客户端应确实发过心跳"


# --------------------------------------------------------------------------- #
# 停止与收尾
# --------------------------------------------------------------------------- #


async def test_collector_request_stop_ends_run(make_config) -> None:
    config = fast_config(make_config)
    async with FakeDanmakuServer(notifications=[danmaku("x", "m1")]) as server:
        collector, _api, _sink, _bus = build(config, server)
        task = asyncio.create_task(collector.run())
        await asyncio.sleep(0.6)
        collector.request_stop()
        stats = await asyncio.wait_for(task, timeout=5.0)

    assert stats.ended_at is not None
    assert collector.state.value == "STOPPED"


async def test_collector_flushes_on_stop(make_config, tmp_path: Path) -> None:
    """停止时必须把缓冲刷干净，不能丢数据。"""
    config = fast_config(make_config)
    config.sinks.batch_size = 1000  # 强制走 flush 路径而不是批满路径
    config.sinks.flush_interval = 60.0

    async with FakeDanmakuServer(notifications=[danmaku("必须落盘", "m1")]) as server:
        collector, _api, _sink, _bus = build(config, server)
        task = asyncio.create_task(collector.run())
        await asyncio.sleep(0.8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=5.0)

    texts = [record["text"] for record in read_all(tmp_path)]
    assert "必须落盘" in texts


async def test_collector_unparsable_message_is_counted(make_config) -> None:
    config = fast_config(make_config)
    async with FakeDanmakuServer(
        notifications=[{"cmd": "DANMU_MSG"}, danmaku("正常", "m1")]
    ) as server:
        collector, _api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=1.0)

    assert stats.bad_messages == 1
    assert stats.parsed == 1


async def test_collector_anonymize_removes_identity(make_config, tmp_path: Path) -> None:
    config = fast_config(make_config)
    async with FakeDanmakuServer(notifications=[danmaku("匿名", "m1", uid=999, uname="某人")]) as server:
        collector, _api, _sink, _bus = build(config, server, anonymize=True)
        await collector.run(duration=1.0)

    record = read_all(tmp_path)[0]
    assert record["uid"] is None
    assert record["uname"] != "某人"
    assert record["uname"].startswith("user_")
    assert record["extra"]["uid_hash"].startswith("u_")


# --------------------------------------------------------------------------- #
# 采集范围：默认只保留有用户文字内容的事件
# --------------------------------------------------------------------------- #


async def test_collector_default_drops_non_content_events(make_config, tmp_path: Path) -> None:
    """不显式传 filters 时默认启用 content_only：点赞等无文字事件被过滤。"""
    config = fast_config(make_config)
    messages = [
        danmaku("有内容", "m1"),
        {"cmd": "LIKE_INFO_V3_CLICK", "data": {"uid": 1, "uname": "甲", "like_text": "为主播点赞了"}},
    ]

    async with FakeDanmakuServer(notifications=messages) as server:
        collector, _api, _sink, _bus = build(config, server)  # 不传 filters
        stats = await collector.run(duration=1.0)

    assert stats.received == 2
    assert stats.filtered == 1, "点赞事件应被内容规则挡掉"
    assert stats.parsed == 1
    assert [record["text"] for record in read_all(tmp_path)] == ["有内容"]


async def test_collector_all_events_mode_keeps_non_content(make_config, tmp_path: Path) -> None:
    """--all-events 等价的配置下，非内容事件也应入库（供协议调试）。"""
    from bili_danmaku.filters import build_filter

    config = fast_config(make_config)
    messages = [
        danmaku("有内容", "m1"),
        {"cmd": "LIKE_INFO_V3_CLICK", "data": {"uid": 1, "uname": "甲", "like_text": "为主播点赞了"}},
    ]

    async with FakeDanmakuServer(notifications=messages) as server:
        collector, _api, _sink, _bus = build(
            config, server, filters=build_filter(content_only=False)
        )
        stats = await collector.run(duration=1.0)

    assert stats.parsed == 2
    assert stats.filtered == 0


async def test_collector_drops_empty_text_danmaku(make_config, tmp_path: Path) -> None:
    """空内容弹幕不入库。"""
    config = fast_config(make_config)
    messages = [danmaku("", "empty-1"), danmaku("有内容", "m2")]

    async with FakeDanmakuServer(notifications=messages) as server:
        collector, _api, _sink, _bus = build(config, server)
        stats = await collector.run(duration=1.0)

    assert stats.parsed == 1
    assert stats.filtered == 1
    assert [record["text"] for record in read_all(tmp_path)] == ["有内容"]


# --------------------------------------------------------------------------- #
# 背压：sink 跟不上时不许拖慢读循环，也不许无声丢数据
#
# 这一组是真实压测（房间 7878，~330 条/分钟）逼出来的契约：
# 把 sink 队列调到 3 条后，队列确实顶满、确实丢最旧，但
# **心跳没受影响、没有重连**，而且丢了多少条被如实计数。
# --------------------------------------------------------------------------- #


class SlowSink(Sink):
    """消费端被卡住的 sink：用来稳定复现"sink 队列顶满"。

    `write()` 里同步阻塞（绝不 await），Bus 的消费协程就会停在这里不再排空队列，
    于是队列必然顶满、必然丢最旧 —— 而读循环不受影响（这正是要验证的点）。
    """

    name = "slow"

    def __init__(self, config, *, block_seconds: float = 0.0) -> None:
        super().__init__(config)
        self.block_seconds = block_seconds
        self.written: list[str | None] = []

    def write(self, events: list[Event]) -> None:
        # **第一条就阻塞**：这样消费协程会卡在这里，队列只能等读循环去塞满它。
        # （只在后续批次阻塞是不够的 —— worker 每次 get 超时也会凑出一批，
        #   足以跟上 12 条的到达速度，队列反而永远填不满。）
        if self.block_seconds:
            time.sleep(self.block_seconds)
        for event in events:
            self.written.append(event.text)
        self.record(len(events))


async def test_queue_full_keeps_newest_and_never_blocks_reader(make_config) -> None:
    """sink 卡住时：队列顶满丢最旧，但**心跳与读循环不受影响**。

    这条用例的价值在于它同时钉住两个契约：

    * sink 的同步写必须在工作线程里执行（`Bus._flush_off_loop`）——
      否则一个 sleep 3 秒的 sink 会让心跳发不出去、采集器判定假死并重连
      （改 `to_thread` 之前实测就是这样：只收到 2 条就重连了）；
    * 队列顶满时丢最旧、留最新，并把丢弃条数如实记到 `stats.dropped`。
    """
    config = fast_config(make_config, heartbeat_interval=0.2, heartbeat_timeout=1.0)
    config.sinks.queue_size = 4
    # 批大小必须为 1：否则 worker 会靠"get 超时"一条条攒进 batch，
    # 队列永远是空的（实测踩过这个坑），根本压不出背压。
    config.sinks.batch_size = 1
    config.sinks.flush_interval = 60.0  # 也不按时间刷盘

    total = 12
    messages = [danmaku(f"第{i}条", f"m{i}") for i in range(total)]

    async with FakeDanmakuServer(notifications=messages, notify_delay=0.02) as server:
        api = FakeApi([HostInfo(host="127.0.0.1", ws_port=server.ws_port)])
        # 消费协程取走第一条后就卡住：此后读循环只能往 4 格队列里塞，必然顶满
        sink = SlowSink(config, block_seconds=1.2)
        bus = Bus([sink], config)
        collector = RoomCollector(ROOM_ID, config, Credentials(), api, bus)

        task = asyncio.create_task(collector.run())
        await asyncio.sleep(0.9)  # 推完 12 条只需 12 × 0.02s，留足余量
        queued_at_stop = [event.text for event in list(bus.workers[0].queue._queue)]
        collector.request_stop()
        stats = await asyncio.wait_for(task, timeout=20.0)

    worker = bus.workers[0]
    order = [f"第{i}条" for i in range(total)]
    assert stats.received == total, f"卡住的 sink 不该让读循环漏消息: {stats.as_dict()}"
    assert stats.reconnects == 0, "sink 卡住绝不能拖到心跳（否则会假死重连）"
    assert server.heartbeats >= 1, "整个过程中心跳必须照常发出"

    assert stats.parsed + stats.filtered + stats.duplicates == total
    # 精确到个位数会不稳：worker 可能已经把下一条取进 batch（正在写入时被卡住），
    # 或者还没轮到那条。契约是"确实丢了旧的、且计数与 sink 侧一致"。
    assert 6 <= stats.dropped <= 8, stats.as_dict()
    assert stats.dropped == worker.dropped, "采集侧与 sink 侧的丢弃计数应一致"

    # 丢的是最旧的：停止那一刻队列里留下的是最新的那几条，顺序不变
    tail = queued_at_stop
    assert tail == order[len(order) - len(tail) :], f"应留下最新的: {tail}"
    assert order[-1] in tail, "最新一条必须被保住"

    # 收尾后：写下来的部分必须保持原顺序（允许中间有被丢掉的空洞），
    # 且"写下来的 + 丢掉的"正好等于全部 —— 不重复、不少、也不错序。
    final = list(sink.written)
    assert [order.index(item) for item in final] == sorted(order.index(item) for item in final), final
    assert len(final) + stats.dropped == total, f"{final} + 丢 {stats.dropped} != {total}"


async def test_blocking_sink_does_not_delay_heartbeat(make_config) -> None:
    """sink 写死在磁盘上时，心跳仍要按间隔发出（回归 D17）。"""
    config = fast_config(make_config, heartbeat_interval=0.2, heartbeat_timeout=1.0)
    config.sinks.queue_size = 100
    config.sinks.batch_size = 1
    config.sinks.flush_interval = 0.05

    messages = [danmaku(f"第{i}条", f"m{i}") for i in range(6)]
    async with FakeDanmakuServer(notifications=messages) as server:
        api = FakeApi([HostInfo(host="127.0.0.1", ws_port=server.ws_port)])
        sink = SlowSink(config, block_seconds=0.8)
        bus = Bus([sink], config)
        collector = RoomCollector(ROOM_ID, config, Credentials(), api, bus)

        task = asyncio.create_task(collector.run())
        await asyncio.sleep(0.8)
        heartbeats_while_blocked = server.heartbeats
        await asyncio.sleep(0.5)
        collector.request_stop()
        stats = await asyncio.wait_for(task, timeout=20.0)

    assert heartbeats_while_blocked >= 2, "sink 阻塞期间心跳仍应持续发出"
    assert server.heartbeats > heartbeats_while_blocked
    assert stats.reconnects == 0
    assert stats.received == len(messages)


async def test_queue_drop_is_visible_in_stats_even_when_all_sinks_full(make_config) -> None:
    """队列满时 `bus.publish` 返回 False —— 采集侧必须把它记成 dropped。"""
    config = fast_config(make_config)
    config.sinks.queue_size = 1
    config.sinks.batch_size = 1
    config.sinks.flush_interval = 60.0

    sink = SlowSink(config)
    bus = Bus([sink], config)
    await bus.start(SessionMeta(session_id="s", room_id=ROOM_ID, started_at=1.0))
    try:
        # 填满唯一的格子，再投一条 → 丢最旧腾位置，但返回 False（本条已被丢弃）
        assert bus.publish(Event(seq=0)) is True
        assert bus.publish(Event(seq=1)) is False
        assert bus.dropped == 1
        assert [event.seq for event in list(bus.workers[0].queue._queue)] == [1], "应留下最新那条"
    finally:
        await bus.stop()
