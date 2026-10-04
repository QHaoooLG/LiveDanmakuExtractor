"""Web 面板测试：广播中枢、REST 接口与 WebSocket。

用 StubPanel 替代真实 PanelService，测试**完全不联网**。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from bili_danmaku.models import Event, RoomInfo
from bili_danmaku.rooms import parse_room_input
from bili_danmaku.server.app import bind_port, create_app
from bili_danmaku.server.hub import WebHub


# --------------------------------------------------------------------------- #
# 桩：不联网的 PanelService
# --------------------------------------------------------------------------- #


class StubPanel:
    def __init__(self) -> None:
        self._rooms: dict[int, RoomInfo] = {}
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def add_room(self, room_input: Any):
        room_input = parse_room_input(room_input)
        if str(room_input) == "0":
            # 与真实实现一致：房间不存在由 room_init 抛 BiliApiError
            from bili_danmaku.api import BiliApiError

            raise BiliApiError("room_init 失败 code=-400", -400)
        room_id = int(room_input)
        if room_id in self._rooms:
            raise ValueError(f"房间 {room_id} 已在监控中")
        room = RoomInfo(room_id=room_id, anchor_uid=8431064, live_status=1)
        self._rooms[room_id] = room
        return SimpleNamespace(
            room=room,
            snapshot=lambda: {
                "room_id": room_id,
                "conn_state": "STREAMING",
                "stored": 0,
                "reconnects": 0,
                "live_status": 1,
            },
        )

    async def remove_room(self, room_id: int) -> bool:
        return self._rooms.pop(room_id, None) is not None

    async def lookup_room(self, room_input: Any) -> dict[str, Any]:
        """与真实 `PanelService.lookup_room` 同形：先解析输入（号或链接）+ 主播昵称。

        链接解析必须在这里也做：真实实现里**解析是 `PanelService` 的职责**
        （端点只额外做一次 `parse_room_input` 以便把参数错误判成 400），
        桩不同步的话，端点用例就测不到真实链路。
        """
        room_input = parse_room_input(room_input)
        if str(room_input) == "0":
            from bili_danmaku.api import BiliApiError

            raise BiliApiError("room_init 失败 code=-400", -400)
        room_id = int(room_input)
        return {
            "room_id": room_id,
            "short_id": None,
            "anchor_uid": 8431064,
            "anchor_name": f"主播{room_id}",
            "anchor_face": None,
            "live_status": 1,
            "title": f"主播{room_id}的直播间",
            "monitored": room_id in self._rooms,
        }

    def rooms(self) -> list[dict[str, Any]]:
        return [
            {"room_id": rid, "conn_state": "STREAMING", "stored": 0, "live_status": 1}
            for rid in sorted(self._rooms)
        ]

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "rooms": len(self._rooms), "sinks": ["jsonl"]}


@pytest.fixture
def hub() -> WebHub:
    # slow_threshold 调低便于测试慢客户端判定
    return WebHub(queue_size=4, buffer_size=50, slow_threshold=5)


@pytest.fixture
def make_event_dict():
    def _make(seq: int = 1, room_id: int = 804456, type_: str = "danmaku", text: str = "测试") -> Event:
        return Event(
            seq=seq,
            session_id="s1",
            room_id=room_id,
            type=type_,
            cmd="DANMU_MSG",
            recv_ts=1_789_711_000.0 + seq,
            msg_id=f"m{seq}",
            uid=1001,
            uname="观众",
            text=text,
        )

    return _make


# --------------------------------------------------------------------------- #
# WebHub
# --------------------------------------------------------------------------- #


def test_subscriber_is_hashable(hub: WebHub) -> None:
    """回归：dataclass 默认生成 __eq__ 会把 __hash__ 置空，导致放进 set 抛 TypeError。"""
    first = hub.subscribe()
    second = hub.subscribe()
    assert len({first, second}) == 2
    assert hub.subscriber_count == 2
    hub.unsubscribe(first)
    assert hub.subscriber_count == 1


def test_publish_delivers_to_subscriber(hub: WebHub, make_event_dict) -> None:
    subscriber = hub.subscribe()
    hub.publish(make_event_dict(text="你好"))
    payload = subscriber.queue.get_nowait()
    assert json.loads(payload)["data"]["text"] == "你好"
    assert hub.published == 1


def test_publish_respects_type_filter(hub: WebHub, make_event_dict) -> None:
    subscriber = hub.subscribe(types={"danmaku"})
    hub.publish(make_event_dict(type_="danmaku"))
    hub.publish(make_event_dict(seq=2, type_="gift"))
    assert subscriber.queue.qsize() == 1
    assert json.loads(subscriber.queue.get_nowait())["data"]["type"] == "danmaku"


def test_publish_respects_room_filter(hub: WebHub, make_event_dict) -> None:
    subscriber = hub.subscribe(rooms={804456})
    hub.publish(make_event_dict(room_id=804456))
    hub.publish(make_event_dict(seq=2, room_id=999))
    assert subscriber.queue.qsize() == 1


def test_publish_respects_keyword_filter(hub: WebHub, make_event_dict) -> None:
    subscriber = hub.subscribe(keyword="主播")
    hub.publish(make_event_dict(text="主播好"))
    hub.publish(make_event_dict(seq=2, text="别的"))
    assert subscriber.queue.qsize() == 1


def test_unfiltered_subscriber_gets_everything(hub: WebHub, make_event_dict) -> None:
    subscriber = hub.subscribe()
    for index in range(3):
        hub.publish(make_event_dict(seq=index, type_="gift" if index else "danmaku"))
    assert subscriber.queue.qsize() == 3


def test_queue_full_drops_oldest_and_counts(hub: WebHub, make_event_dict) -> None:
    subscriber = hub.subscribe()
    for index in range(10):  # queue_size=4
        hub.publish(make_event_dict(seq=index, text=f"第{index}条"))
    assert subscriber.queue.qsize() == 4
    assert subscriber.dropped == 6
    assert subscriber.is_slow, "累计丢弃达到阈值应判定为慢客户端"

    texts = [json.loads(subscriber.queue.get_nowait())["data"]["text"] for _ in range(4)]
    assert texts == ["第6条", "第7条", "第8条", "第9条"], "应保留最新的一批"


def test_recent_returns_latest_first_slice(hub: WebHub, make_event_dict) -> None:
    for index in range(5):
        hub.publish(make_event_dict(seq=index, text=f"t{index}"))
    recent = hub.recent(limit=2)
    assert [json.loads(item)["data"]["text"] for item in recent] == ["t3", "t4"]


def test_recent_applies_accept_filter(hub: WebHub, make_event_dict) -> None:
    """回归：历史回填必须走订阅者的过滤条件。"""
    hub.publish(make_event_dict(seq=1, type_="danmaku", text="要的"))
    hub.publish(make_event_dict(seq=2, type_="gift", text="不要的"))
    hub.publish(make_event_dict(seq=3, type_="danmaku", text="也要的"))

    subscriber = hub.subscribe(types={"danmaku"})
    filtered = hub.recent(limit=10, accept=lambda record: hub.accepts(subscriber, record))
    kinds = [json.loads(item)["data"]["type"] for item in filtered]
    assert kinds == ["danmaku", "danmaku"]


def test_recent_per_room(hub: WebHub, make_event_dict) -> None:
    hub.publish(make_event_dict(seq=1, room_id=111))
    hub.publish(make_event_dict(seq=2, room_id=222))
    assert len(hub.recent(room_id=111)) == 1
    assert len(hub.recent(room_id=None)) == 2


def test_publish_control_reaches_everyone(hub: WebHub) -> None:
    filtered = hub.subscribe(types={"danmaku"})
    hub.publish_control("rooms", {"rooms": []})
    assert json.loads(filtered.queue.get_nowait())["t"] == "rooms"


def test_hub_stats(hub: WebHub, make_event_dict) -> None:
    hub.subscribe()
    hub.publish(make_event_dict())
    stats = hub.stats
    assert stats["published"] == 1
    assert stats["subscribers"] == 1
    assert stats["rooms"] == [804456]
    assert stats["cumulative_total"] == 1


# --------------------------------------------------------------------------- #
# 累计计数（面板"从启动累计"统计口径的数据源）
# --------------------------------------------------------------------------- #


def test_hub_tracks_cumulative_counts(hub: WebHub, make_event_dict) -> None:
    for index in range(3):
        hub.publish(make_event_dict(seq=index, type_="danmaku"))
    hub.publish(make_event_dict(seq=9, type_="super_chat", room_id=999))

    assert hub.cumulative_total == 4
    assert hub.cumulative["total"] == 4
    assert hub.cumulative["by_type"] == {"danmaku": 3, "super_chat": 1}
    assert hub.cumulative["by_room"] == {"804456": 3, "999": 1}
    assert hub.cumulative["uptime_s"] >= 0
    assert hub.cumulative["started_at"] > 0


def test_hub_by_room_type_scopes_correctly(hub: WebHub, make_event_dict) -> None:
    hub.publish(make_event_dict(seq=1, type_="danmaku", room_id=111))
    hub.publish(make_event_dict(seq=2, type_="gift", room_id=111))
    hub.publish(make_event_dict(seq=3, type_="danmaku", room_id=222))

    assert hub.by_room_type(111) == {"danmaku": 1, "gift": 1}
    assert hub.by_room_type(222) == {"danmaku": 1}
    assert hub.by_room_type() == {"danmaku": 2, "gift": 1}, "不传房间 = 全部合并"
    assert hub.by_room_type(333) == {}

    hub.publish(make_event_dict(seq=4, type_="danmaku", room_id=111))
    assert list(hub.by_room_type(111)) == ["danmaku", "gift"], "按次数降序"


def test_hub_mark_reset_clears_cumulative_only(hub: WebHub, make_event_dict) -> None:
    hub.subscribe()
    hub.publish(make_event_dict(seq=1))
    hub.mark_reset()

    assert hub.cumulative_total == 0
    assert hub.cumulative["by_type"] == {}
    assert hub.subscriber_count == 1, "重置统计不该动订阅者"
    assert hub.published == 1, "published 是历史计数，不参与重置"
    assert hub.recent(limit=10), "缓冲也不该被清空"


# --------------------------------------------------------------------------- #
# 端口选择
# --------------------------------------------------------------------------- #


def test_bind_port_returns_requested_when_free() -> None:
    port = bind_port("127.0.0.1", 18787)
    assert port == 18787


def test_bind_port_skips_occupied() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 18790))
        blocker.listen(1)
        port = bind_port("127.0.0.1", 18790)
        assert port != 18790
        assert 18791 <= port <= 18810


def test_bind_port_zero_returns_real_ephemeral_port() -> None:
    """`port=0` 必须返回**内核真正分配的临时端口**，而不是 0/1。

    回归：老实现把 0 也丢进扫描列表，`bind((host, 0))` 成功却返回了迭代值
    （实测返回 `1`），调用方于是对外报出一个根本没在监听的地址。
    """
    import socket

    port = bind_port("127.0.0.1", 0)
    assert port > 0, f"port=0 不应返回 {port}"
    # 必须是**真正可用**的端口：能立刻再绑定一次
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))
    # 且应当落在临时端口区间（Linux 常见 32768+；Windows 从 49152 起，
    # 但这里放宽到 1024 以上，只排除"跑到特权端口"这类明显错误）
    assert port > 1024, f"临时端口看起来不合理: {port}"


def test_bind_port_falls_back_to_ephemeral_when_range_full() -> None:
    """请求端口及其顺延区间全被占用时，回退到系统分配的临时端口。"""
    import socket

    blockers = []
    try:
        base = 18900
        span = 2
        for offset in range(span + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", base + offset))
            sock.listen(1)
            blockers.append(sock)
        port = bind_port("127.0.0.1", base, span=span)
        assert port > 1024
        assert not (base <= port <= base + span)
    finally:
        for sock in blockers:
            sock.close()


def test_try_bind_returns_none_when_occupied() -> None:
    """`_try_bind` 占用时返回 None（而不是抛异常），供 `bind_port` 循环使用。"""
    import socket

    from bili_danmaku.server.app import _try_bind

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 18950))
        blocker.listen(1)
        assert _try_bind("127.0.0.1", 18950) is None
    # 释放后应能拿到同一个端口
    assert _try_bind("127.0.0.1", 18950) == 18950


# --------------------------------------------------------------------------- #
# REST / WebSocket（走 ASGI，不启动真实服务器）
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(make_config, hub):
    from fastapi.testclient import TestClient

    panel = StubPanel()
    config = make_config()
    app = create_app(config, None, hub, panel, initial_rooms=(804456,))  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        test_client.panel = panel  # type: ignore[attr-defined]
        yield test_client


def test_health_reports_frontend_and_panel(client) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["rooms"] == 1
    assert "frontend_built" in payload


def test_index_serves_html(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_initial_rooms_are_started(client) -> None:
    rooms = client.get("/api/rooms").json()["rooms"]
    assert [room["room_id"] for room in rooms] == [804456]


def test_add_and_remove_room(client) -> None:
    response = client.post("/api/rooms", json={"room": 123456})
    assert response.status_code == 201
    assert response.json()["room"]["room_id"] == 123456

    assert client.post("/api/rooms", json={"room": 123456}).status_code == 409
    assert client.delete("/api/rooms/123456").status_code == 200
    assert client.delete("/api/rooms/123456").status_code == 404


def test_add_room_reports_failure(client) -> None:
    response = client.post("/api/rooms", json={"room": 0})
    assert response.status_code == 400
    assert "失败" in response.json()["detail"]


def test_room_info_returns_anchor_and_title(client) -> None:
    """右侧栏「XXX 的直播间」的数据源：查信息**不启动采集**。"""
    payload = client.get("/api/room-info?room=804456").json()
    assert payload["room_id"] == 804456
    assert payload["anchor_name"] == "主播804456"
    assert payload["title"] == "主播804456的直播间"
    assert payload["monitored"] is True
    assert client.get("/api/room-info?room=123456").json()["monitored"] is False


def test_room_info_reports_failure(client) -> None:
    response = client.get("/api/room-info?room=0")
    assert response.status_code == 400
    assert "查询房间失败" in response.json()["detail"]


def test_room_info_requires_room_param(client) -> None:
    assert client.get("/api/room-info").status_code == 422


def test_room_info_accepts_a_live_url(client) -> None:
    """粘直播间链接也能查（需求 5）：由后端负责把房间号取出来。"""
    link = "https://live.bilibili.com/804456?live_from=85001&spm_id_from=333.1365.x"
    assert client.get("/api/room-info", params={"room": link}).json()["room_id"] == 804456


def test_room_info_rejects_unrecognisable_links(client) -> None:
    for bad in ("https://example.com/12345", "https://live.bilibili.com/blanc/804456"):
        response = client.get("/api/room-info", params={"room": bad})
        assert response.status_code == 400, bad
        assert "链接" in response.json()["detail"]


def test_add_room_accepts_a_live_url(client) -> None:
    link = "https://live.bilibili.com/123456?live_from=85001"
    response = client.post("/api/rooms", json={"room": link})
    assert response.status_code == 201
    assert response.json()["room"]["room_id"] == 123456
    assert client.delete("/api/rooms/123456").status_code == 200


def test_add_room_bad_link_is_400_not_409(client) -> None:
    """坏链接是**参数错误**（400），不能报成 409「已在监控中」—— 那会完全误导用户。"""
    response = client.post("/api/rooms", json={"room": "https://example.com/12345"})
    assert response.status_code == 400
    assert "链接" in response.json()["detail"]


def test_events_without_sqlite_falls_back_to_memory(client, hub, make_event_dict) -> None:
    for index in range(3):
        hub.publish(make_event_dict(seq=index, text=f"弹幕{index}"))
    payload = client.get("/api/events?limit=10").json()
    assert payload["source"] == "memory"
    assert payload["total"] == 3
    assert [item["text"] for item in payload["items"]] == ["弹幕0", "弹幕1", "弹幕2"]


def test_events_memory_filter_by_type(client, hub, make_event_dict) -> None:
    hub.publish(make_event_dict(seq=1, type_="danmaku"))
    hub.publish(make_event_dict(seq=2, type_="gift"))
    payload = client.get("/api/events?type=gift").json()
    assert payload["total"] == 1
    assert payload["items"][0]["type"] == "gift"


def test_events_memory_filter_by_keyword(client, hub, make_event_dict) -> None:
    hub.publish(make_event_dict(seq=1, text="主播好"))
    hub.publish(make_event_dict(seq=2, text="别的内容"))
    payload = client.get("/api/events?q=主播").json()
    assert payload["total"] == 1


def test_stats_without_sqlite(client, hub, make_event_dict) -> None:
    hub.publish(make_event_dict())
    payload = client.get("/api/stats").json()
    assert payload["source"] == "memory"
    assert payload["hub"]["published"] == 1


def test_export_requires_sqlite(client) -> None:
    response = client.get("/api/export?format=csv")
    assert response.status_code == 409
    assert "sqlite" in response.json()["detail"]


def test_websocket_receives_hello_backfill_and_live(client, hub, make_event_dict) -> None:
    """WebSocket 端到端：hello → 历史回填 → 实时推送。"""
    hub.publish(make_event_dict(seq=1, text="回填内容"))

    with client.websocket_connect("/ws/live?room_id=804456") as socket:
        hello = socket.receive_json()
        assert hello["t"] == "hello"
        assert hello["data"]["rooms"][0]["room_id"] == 804456

        backfill = socket.receive_json()
        assert backfill["t"] == "event"
        assert backfill["data"]["text"] == "回填内容"

        # 实时推送：publish 之后应能在同一个连接上收到
        hub.publish(make_event_dict(seq=2, text="实时内容"))
        live = socket.receive_json()
        assert live["data"]["text"] == "实时内容"


def test_websocket_applies_type_filter_to_backfill(client, hub, make_event_dict) -> None:
    """回归：订阅 types=danmaku 时，历史回填不该混入其它类型。"""
    hub.publish(make_event_dict(seq=1, type_="gift", text="不要的"))
    hub.publish(make_event_dict(seq=2, type_="danmaku", text="要的"))

    with client.websocket_connect("/ws/live?types=danmaku") as socket:
        assert socket.receive_json()["t"] == "hello"
        first_event = socket.receive_json()
        assert first_event["data"]["type"] == "danmaku"
        assert first_event["data"]["text"] == "要的"
