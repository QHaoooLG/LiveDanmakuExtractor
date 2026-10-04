"""M4 覆盖率：WebSocket 连接层（`client.py`）。

`DanmakuConnection` 是「协议假设」的集中地，此前只被 collector 的集成测试间接打到
（67%）。这里直接驱动它，锁死四件事：

* 认证包的内容与 `AuthFailed` 判定；
* **读超时必须被生成器内部吞掉**（缺陷 D1：逃出去会形成重连风暴）；
* 心跳回应里的人气值解析与假死判定（`ConnectionStale`）；
* `probe()` 端到端采样。
"""

from __future__ import annotations

import asyncio
import json
import time
import zlib
from typing import Any

import pytest
import websockets

from bili_danmaku.client import AuthFailed, ConnectionStale, DanmakuConnection, probe
from bili_danmaku.config import Config
from bili_danmaku.credentials import Credentials
from bili_danmaku.models import HostInfo, RoomInfo, TokenBundle
from bili_danmaku.protocol import HEARTBEAT_PAYLOAD, Operation, ProtoVer, pack, unpack

from fake_server import FakeDanmakuServer

ROOM = RoomInfo(room_id=804456, anchor_uid=8431064, live_status=1)


def config(make_config, **client) -> Config:
    settings = {"recv_timeout": 0.15, "heartbeat_interval": 0.2, "heartbeat_timeout": 0.8}
    settings.update(client)
    return make_config(client=settings)


def bundle(server: FakeDanmakuServer, *, token: str = "tok") -> TokenBundle:
    return TokenBundle(
        token=token, hosts=[HostInfo(host="127.0.0.1", ws_port=server.ws_port)], source="getConf"
    )


def notification(text: str, msg_id: str) -> dict[str, Any]:
    """结构接近实测样本的 DANMU_MSG（只需能穿过 client 层被原样产出）。"""
    extra = json.dumps({"content": text, "id_str": msg_id}, ensure_ascii=False)
    user = {"uid": 1, "base": {"name": "甲"}, "wealth": {"level": 5}}
    return {
        "cmd": "DANMU_MSG",
        "info": [
            [0, 1, 25, 16777215, 0, 0, 0, "", 0, 0, 0, "", 0, "{}", "{}",
             {"extra": extra, "user": user}],
            text,
            [1, "甲", 0, 0, 0, 10000, 1, ""],
        ],
    }


def frame(payload: bytes, protover: int = ProtoVer.PLAIN, operation: int = Operation.MESSAGE) -> bytes:
    return pack(operation, payload, protover)


async def consume(connection: DanmakuConnection, limit: int = 0) -> list[dict[str, Any]]:
    """把 messages() 抽干（limit=0 表示读到 should_stop 为真为止）。"""
    out: list[dict[str, Any]] = []
    stream = connection.messages(should_stop=(lambda: len(out) >= limit) if limit else None)
    async for message in stream:
        out.append(message)
    return out


# --------------------------------------------------------------------------- #
# 连接 / 认证
# --------------------------------------------------------------------------- #


async def test_authenticate_sends_token_and_room(make_config) -> None:
    async with FakeDanmakuServer() as server:
        connection = DanmakuConnection(
            bundle(server), ROOM, Credentials(cookies={"SESSDATA": "s" * 24}), config(make_config)
        )
        async with connection:
            reply = await connection.authenticate()

        assert reply["code"] == 0
        assert connection.auth_reply == {"code": 0}
        auth = server.auth_payloads[0]
        assert auth["roomid"] == 804456
        assert auth["key"] == "tok"
        assert auth["platform"] == "web"
        assert "buvid" not in auth, "没有 buvid3 时不应塞空字段"
        assert connection.last_heartbeat_reply_at > 0, "认证成功即开始假死计时"


async def test_authenticate_includes_buvid_when_available(make_config) -> None:
    async with FakeDanmakuServer() as server:
        creds = Credentials(cookies={"SESSDATA": "s" * 24, "buvid3": "B-3"})
        connection = DanmakuConnection(bundle(server), ROOM, creds, config(make_config))
        async with connection:
            await connection.authenticate()
        assert server.auth_payloads[0]["buvid"] == "B-3"


async def test_auth_rejection_raises_auth_failed(make_config) -> None:
    async with FakeDanmakuServer(auth_code=-101, auth_fail_times=1) as server:
        connection = DanmakuConnection(bundle(server), ROOM, Credentials(), config(make_config))
        async with connection:
            with pytest.raises(AuthFailed) as excinfo:
                await connection.authenticate()
        assert excinfo.value.code == -101


async def test_auth_reply_missing_means_failure(make_config) -> None:
    """服务端没回 op=8 时不能误判为认证成功。"""

    async def handler(websocket: Any) -> None:
        await websocket.recv()
        await websocket.send(frame(b"{}"))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        hosts = [HostInfo(host="127.0.0.1", ws_port=port)]
        connection = DanmakuConnection(
            TokenBundle(token="t", hosts=hosts, source="getConf"), ROOM, Credentials(),
            config(make_config),
        )
        async with connection:
            with pytest.raises(AuthFailed) as excinfo:
                await connection.authenticate()
        assert excinfo.value.code == -1
    finally:
        server.close()
        await server.wait_closed()


async def test_operations_before_open_raise_runtime_error(make_config) -> None:
    connection = DanmakuConnection(
        TokenBundle(token="t", hosts=[HostInfo(host="127.0.0.1", ws_port=1)], source="getConf"),
        ROOM,
        Credentials(),
        config(make_config),
    )
    with pytest.raises(RuntimeError):
        await connection.authenticate()
    with pytest.raises(RuntimeError):
        await connection.send_heartbeat()
    with pytest.raises(RuntimeError):
        await connection.recv_packets()


async def test_uri_rotates_hosts_and_prefers_wss(make_config) -> None:
    hosts = [HostInfo(host="a", wss_port=2245), HostInfo(host="b", ws_port=2244)]
    connection = DanmakuConnection(
        TokenBundle(token="t", hosts=hosts, source="getConf"), ROOM, Credentials(),
        config(make_config),
    )
    assert connection.uri == "wss://a:2245/sub"
    connection.host_index = 1
    assert connection.uri == "ws://b:2244/sub", "没有 wss_port 时必须退回明文端口"


# --------------------------------------------------------------------------- #
# 收包与 messages()
# --------------------------------------------------------------------------- #


async def test_messages_yields_danmaku_and_tracks_popularity(make_config) -> None:
    messages = [notification("你好", "m1"), notification("世界", "m2")]
    async with FakeDanmakuServer(notifications=messages, popularity=4321, linger=2.0) as server:
        connection = DanmakuConnection(bundle(server), ROOM, Credentials(), config(make_config))
        async with connection:
            await connection.authenticate()
            await connection.send_heartbeat()
            collected = await asyncio.wait_for(consume(connection, limit=2), timeout=5)

        assert [item["info"][1] for item in collected] == ["你好", "世界"]
        assert connection.stats.messages == 2
        assert connection.stats.packets >= 2
        assert connection.stats.last_popularity == 4321


async def test_messages_swallows_read_timeout(make_config) -> None:
    """缺陷 D1 回归：读超时必须被吞掉，不能终结迭代器。"""
    async with FakeDanmakuServer(notifications=[]) as server:
        connection = DanmakuConnection(bundle(server), ROOM, Credentials(), config(make_config))
        async with connection:
            await connection.authenticate()
            task = asyncio.create_task(consume(connection))
            await asyncio.sleep(0.4)  # 期间必然经历多次 recv_timeout（0.15s）
            assert not task.done(), "读超时不该让迭代器结束（否则会误判连接结束并重连）"
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_messages_raises_stale_when_heartbeat_dies(make_config) -> None:
    async with FakeDanmakuServer(silent_heartbeat=True) as server:
        connection = DanmakuConnection(
            bundle(server), ROOM, Credentials(), config(make_config, heartbeat_timeout=0.25)
        )
        async with connection:
            await connection.authenticate()
            with pytest.raises(ConnectionStale):
                await asyncio.wait_for(consume(connection), timeout=5)


async def test_counts_bad_json_and_skips_unknown_protover(make_config) -> None:
    """非法 JSON 与未知 protover 都只能计数跳过，不能打死读循环。"""
    garbage = [frame(b"\xff\xfe not json"), frame(b'{"ok":1}', protover=9)]
    async with FakeDanmakuServer(
        raw_frames=garbage, notifications=[notification("好的", "m9")]
    ) as server:
        connection = DanmakuConnection(
            bundle(server), ROOM, Credentials(), config(make_config, heartbeat_timeout=10.0)
        )
        async with connection:
            await connection.authenticate()
            collected = await asyncio.wait_for(consume(connection, limit=1), timeout=5)

        assert collected[0]["info"][1] == "好的", "坏帧必须被跳过而不是终止读循环"
        assert connection.stats.bad_json == 1
        assert connection.stats.unknown_protover == 1


async def test_capture_raw_is_recorded(make_config) -> None:
    async with FakeDanmakuServer(notifications=[notification("x", "m1")], linger=1.0) as server:
        connection = DanmakuConnection(
            bundle(server), ROOM, Credentials(), config(make_config, heartbeat_timeout=10.0)
        )
        connection.capture_raw = True
        async with connection:
            await connection.authenticate()
            await connection.recv_packets()
        assert len(connection.raw_frames) == 1


async def test_recv_packets_returns_empty_on_protocol_error(make_config) -> None:
    """拆包失败要计数并返回空列表，而不是把异常抛给读循环。"""
    truncated = frame(b"x" * 10)[:-4]  # 截断 → "包长越界"
    async with FakeDanmakuServer(
        raw_frames=[truncated, truncated], notifications=[notification("占位", "keep-alive")]
    ) as server:
        connection = DanmakuConnection(
            bundle(server), ROOM, Credentials(), config(make_config, heartbeat_timeout=10.0)
        )
        async with connection:
            await connection.authenticate()
            # 读循环必须继续：先跳过坏帧，再拿到服务端随后推的占位通知
            collected = await asyncio.wait_for(consume(connection, limit=1), timeout=5)

        assert collected[0]["info"][1] == "占位"
        assert connection.stats.protocol_errors == 2, "两个坏帧都应被计数"


def test_zlib_frame_is_unpacked_recursively() -> None:
    """protover=2 解压后仍是带头部的包 —— 必须递归拆包。"""
    inner = pack(Operation.MESSAGE, json.dumps({"cmd": "DANMU_MSG"}).encode(), ProtoVer.PLAIN)
    packets = unpack(pack(Operation.MESSAGE, zlib.compress(inner), ProtoVer.ZLIB))
    assert len(packets) == 1
    assert json.loads(packets[0].payload)["cmd"] == "DANMU_MSG"


# --------------------------------------------------------------------------- #
# probe()
# --------------------------------------------------------------------------- #


async def test_probe_collects_messages_and_sends_heartbeat(make_config) -> None:
    messages = [notification(f"n{i}", f"m{i}") for i in range(3)]
    async with FakeDanmakuServer(notifications=messages) as server:
        connection, collected = await probe(
            room=ROOM,
            bundle=bundle(server),
            credentials=Credentials(),
            config=config(make_config, heartbeat_interval=0.05),
            seconds=3.0,
            max_messages=3,
        )
        await connection.close()

    assert len(collected) == 3
    assert connection.auth_reply["code"] == 0
    assert server.heartbeats >= 1
    assert HEARTBEAT_PAYLOAD == b"[object Object]", "心跳负载是实测的字面量"


async def test_probe_reports_popularity_from_heartbeat_reply(make_config) -> None:
    async with FakeDanmakuServer(notifications=[], popularity=777) as server:
        connection, collected = await probe(
            room=ROOM,
            bundle=bundle(server),
            credentials=Credentials(),
            config=config(make_config, heartbeat_interval=0.05),
            seconds=0.4,
            max_messages=1,
        )
        await connection.close()

    assert collected == []
    assert connection.stats.last_popularity == 777


async def test_probe_stops_early_on_stale_connection(make_config) -> None:
    async with FakeDanmakuServer(silent_heartbeat=True) as server:
        started = time.monotonic()
        connection, collected = await probe(
            room=ROOM,
            bundle=bundle(server),
            credentials=Credentials(),
            config=config(make_config, heartbeat_timeout=0.25),
            seconds=10.0,
            max_messages=5,
        )
        elapsed = time.monotonic() - started
        await connection.close()

    assert collected == []
    assert elapsed < 5.0, "假死应尽快结束探测，而不是等满 seconds"


async def test_probe_on_event_and_raw_capture(make_config) -> None:
    seen: list[str] = []
    async with FakeDanmakuServer(notifications=[notification("回调", "m1")]) as server:
        connection, _collected = await probe(
            room=ROOM,
            bundle=bundle(server),
            credentials=Credentials(),
            config=config(make_config),
            seconds=1.5,
            max_messages=1,
            on_event=lambda message: seen.append(message["info"][1]),
            capture_raw=True,
        )
        await connection.close()
    assert seen == ["回调"]
    assert connection.raw_frames
