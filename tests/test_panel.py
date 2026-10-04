"""M4：多房间并发（`PanelService`）—— 用两个假服务端驱动，完全不联网。

这里要证明的不是"能加两个房间"，而是三件在多房间下才会暴露的事：

1. **房间上限是准的**：达到 `max_rooms` 时连不存在的房间号也应报"上限"，
   而不是先去打一次网络接口再返回一个误导性的"房间不存在"；
2. **落盘按房间隔离**：JSONL 分文件、SQLite 同一个库但 `room_id`/`session` 各自独立
   （两个 `SqliteSink` 同时写一个库，靠 WAL + busy_timeout 共存）；
3. **房间之间互不拖累**：停掉一个房间，另一个继续采。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bili_danmaku.credentials import Credentials
from bili_danmaku.models import Event, HostInfo, RoomInfo, TokenBundle
from bili_danmaku.server.hub import Subscriber, WebHub
from bili_danmaku.server.panel import PanelService
from bili_danmaku.store import DanmakuStore

from fake_server import FakeDanmakuServer

ROOM_A = 804456
ROOM_B = 123456


# --------------------------------------------------------------------------- #
# 夹具与桩
# --------------------------------------------------------------------------- #


def danmaku(text: str, msg_id: str, *, uid: int = 1001, uname: str = "观众甲") -> dict:
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
                0, 1, 25, 16777215, 1_789_710_200_000, 0, 0, "abcd1234", 0, 0, 0, "", 0,
                "{}", "{}", {"extra": extra, "user": user},
            ],
            text,
            [uid, uname, 0, 0, 0, 10000, 1, ""],
            [],
            [11, 0, 6406234, ">50000", 0],
            ["", ""],
            0, 0, None, {"ct": "CT", "ts": 1_789_710_200}, 0, 0, None, None, 0, 390, [5], None,
        ],
    }


class MultiRoomApi:
    """按 room_id 把采集器指向各自的假服务端，模拟真实的多房间并发。"""

    def __init__(self, hosts: dict[int, HostInfo], rooms: dict[int, RoomInfo] | None = None) -> None:
        self.hosts = hosts
        self.rooms = rooms or {}
        self.token_calls: list[int] = []
        self.room_init_calls: list[int] = []
        self.anchor_calls: list[int] = []

    async def room_init(self, room_input: int | str) -> RoomInfo:
        self.room_init_calls.append(int(room_input))
        room = self.rooms.get(int(room_input))
        if room is None:
            from bili_danmaku.api import BiliApiError

            raise BiliApiError(f"room_init 失败 code=-400（房间 {room_input}）", -400)
        return room

    async def anchor_info(self, room_id: int) -> dict[str, Any] | None:
        self.anchor_calls.append(room_id)
        room = self.rooms.get(room_id)
        if room is None or not room.anchor_uid:
            return None
        return {"uid": room.anchor_uid, "uname": f"主播{room_id}"}

    async def room_info(self, room_input: int | str, *, with_anchor: bool = True) -> RoomInfo:
        room = await self.room_init(room_input)
        if not with_anchor:
            return room
        anchor = await self.anchor_info(room.room_id)
        return room.model_copy(update={"anchor_name": (anchor or {}).get("uname")})

    async def acquire_token(self, room_id: int) -> TokenBundle:
        self.token_calls.append(room_id)
        return TokenBundle(token="fake-token", hosts=[self.hosts[room_id]], source="getConf")

    async def history(self, room_id: int) -> list[dict]:
        return []

    async def fingerprint(self) -> tuple[str, str]:  # pragma: no cover - 面板启动才用
        return "", ""

    # 与 BiliApi 一致：PanelService.stop() 会用 __aexit__ 收尾
    async def __aenter__(self) -> "MultiRoomApi":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def panel_config(make_config):
    return make_config(
        general={"max_rooms": 3},
        client={
            "heartbeat_interval": 0.3,
            "heartbeat_timeout": 1.0,
            "recv_timeout": 0.2,
            "reconnect_max_delay": 0.3,
        },
        sinks={"batch_size": 1, "flush_interval": 0.02, "queue_size": 100},
    )


def attach_api(panel: PanelService, api: MultiRoomApi) -> MultiRoomApi:
    """绕过 `start()`（那里会构造真的 BiliApi），直接注入假 API。"""
    panel.api = api  # type: ignore[assignment]
    return api


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def jsonl_by_room(tmp_path: Path) -> dict[int, list[dict]]:
    """读取输出目录，按文件名里的 room_id 分组（多房间必须分文件）。"""
    grouped: dict[int, list[dict]] = {}
    for path in sorted(tmp_path.glob("*.jsonl")):
        room_id = int(path.name.split("_", 1)[0])
        grouped[room_id] = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
    return grouped


def make_event(*, room_id: int, text: str, seq: int = 1) -> Event:
    return Event(
        seq=seq,
        session_id=f"test-{room_id}",
        room_id=room_id,
        type="danmaku",
        cmd="DANMU_MSG",
        recv_ts=1_789_711_000.0,
        msg_id=f"{room_id}-{seq}",
        uid=1001,
        uname="观众甲",
        text=text,
    )


def drain(subscriber: Subscriber) -> list[str]:
    """取出订阅者队列里当前所有载荷（不等待）。"""
    out: list[str] = []
    while not subscriber.queue.empty():
        out.append(subscriber.queue.get_nowait())
    return out


# --------------------------------------------------------------------------- #
# 1. 并发上限
# --------------------------------------------------------------------------- #


async def test_max_rooms_is_enforced_before_any_network_call(make_config) -> None:
    """达到上限时，`add_room` 必须**先**拒绝，不该先打一次接口再报别的错。"""
    config = panel_config(make_config)
    config.general.max_rooms = 1
    panel = PanelService(config, Credentials(), WebHub())
    api = MultiRoomApi(
        {ROOM_A: HostInfo(host="127.0.0.1", ws_port=1)},
        {ROOM_A: RoomInfo(room_id=ROOM_A, anchor_uid=1, live_status=1)},
    )
    attach_api(panel, api)

    runtime = await panel.add_room(ROOM_A)
    assert panel.room_count == 1

    # 不存在于假 API 的房间号：若先查房间，错误信息会变成"房间不存在"，掩盖真正原因
    with pytest.raises(ValueError, match="并发上限"):
        await panel.add_room(999999)

    assert api.room_init_calls == [ROOM_A], "已达上限时不应再发起 room_init"
    await panel.remove_room(ROOM_A)
    runtime.collector.request_stop()


async def test_two_rooms_run_concurrently_and_write_separate_files(make_config, tmp_path: Path) -> None:
    """两个房间同时采集：各自落盘、各自会话、互不干扰。"""
    config = panel_config(make_config)
    hub = WebHub(buffer_size=100)
    panel = PanelService(config, Credentials(), hub)

    async with (
        FakeDanmakuServer(
            notifications=[danmaku("A-1", "a1"), danmaku("A-2", "a2")], notify_delay=0.05
        ) as server_a,
        FakeDanmakuServer(
            notifications=[danmaku("B-1", "b1"), danmaku("B-2", "b2")], notify_delay=0.05
        ) as server_b,
    ):
        api = MultiRoomApi(
            {
                ROOM_A: HostInfo(host="127.0.0.1", ws_port=server_a.ws_port),
                ROOM_B: HostInfo(host="127.0.0.1", ws_port=server_b.ws_port),
            },
            {
                ROOM_A: RoomInfo(room_id=ROOM_A, anchor_uid=1, live_status=1),
                ROOM_B: RoomInfo(room_id=ROOM_B, anchor_uid=2, live_status=1),
            },
        )
        attach_api(panel, api)

        runtime_a = await panel.add_room(ROOM_A)
        runtime_b = await panel.add_room(ROOM_B)
        assert set(panel.room_ids()) == {ROOM_A, ROOM_B}

        assert await wait_for(lambda: hub.published >= 4), "两个房间的事件都应到达面板总线"
        assert server_a.connections == 1 and server_b.connections == 1
        assert set(api.token_calls) == {ROOM_A, ROOM_B}, "每个房间各取一次 token"

        # 房间过滤：只订阅 B 的客户端不该看到 A 的内容（回填与实时都走同一判定）
        subscriber = hub.subscribe(rooms={ROOM_B})
        backfill_b = hub.recent(ROOM_B, limit=10, accept=lambda rec: hub.accepts(subscriber, rec))
        assert {json.loads(item)["data"]["text"] for item in backfill_b} == {"B-1", "B-2"}
        assert len(hub.recent(ROOM_A, limit=10)) >= 2

        hub.publish(make_event(room_id=ROOM_A, text="A-3"))
        hub.publish(make_event(room_id=ROOM_B, text="B-3"))
        delivered = [json.loads(item)["data"]["text"] for item in drain(subscriber)]
        assert delivered == ["B-3"], "room_id 订阅过滤必须同时作用于实时推送"

        sessions = {runtime_a.collector.session_id, runtime_b.collector.session_id}
        assert len(sessions) == 2 and all(sessions), "两个房间必须是两个会话"
        assert runtime_a.collector.session_id.endswith(str(ROOM_A))
        assert runtime_b.collector.session_id.endswith(str(ROOM_B))

        await panel.stop()

    # 落盘：每个房间一个 jsonl，内容不串台
    expected_session = {ROOM_A: runtime_a.collector.session_id, ROOM_B: runtime_b.collector.session_id}
    grouped = jsonl_by_room(tmp_path)
    assert sorted(grouped) == [ROOM_B, ROOM_A]
    assert {record["text"] for record in grouped[ROOM_A]} == {"A-1", "A-2"}
    assert {record["text"] for record in grouped[ROOM_B]} == {"B-1", "B-2"}
    for room_id, records in grouped.items():
        assert {record["room_id"] for record in records} == {room_id}
        assert {record["session_id"] for record in records} == {expected_session[room_id]}


async def test_two_rooms_share_one_sqlite_and_stay_isolated(make_config, tmp_path: Path) -> None:
    """两个 SqliteSink 同时写同一个库：靠 WAL + busy_timeout 共存，数据不串房间。"""
    config = panel_config(make_config)
    config.sinks.types = ["sqlite"]
    hub = WebHub(buffer_size=100)
    panel = PanelService(config, Credentials(), hub)

    async with (
        FakeDanmakuServer(notifications=[danmaku("A-1", "a1")], notify_delay=0.05) as server_a,
        FakeDanmakuServer(notifications=[danmaku("B-1", "b1")], notify_delay=0.05) as server_b,
    ):
        attach_api(
            panel,
            MultiRoomApi(
                {
                    ROOM_A: HostInfo(host="127.0.0.1", ws_port=server_a.ws_port),
                    ROOM_B: HostInfo(host="127.0.0.1", ws_port=server_b.ws_port),
                },
                {
                    ROOM_A: RoomInfo(room_id=ROOM_A, anchor_uid=1, live_status=1),
                    ROOM_B: RoomInfo(room_id=ROOM_B, anchor_uid=2, live_status=1),
                },
            ),
        )
        await panel.add_room(ROOM_A)
        await panel.add_room(ROOM_B)
        assert await wait_for(lambda: hub.published >= 2)
        await panel.stop()

    db = tmp_path / "danmaku.db"
    assert db.is_file()
    store = DanmakuStore(db)
    try:
        assert store.count(room_id=ROOM_A) == 1
        assert store.count(room_id=ROOM_B) == 1
        rows = list(store.iter_events(limit=10))
        assert {row.room_id for row in rows} == {ROOM_A, ROOM_B}
        assert len({row.session_id for row in rows}) == 2, "两个房间必须落在两个 session 上"
        for row in rows:
            assert row.session_id.endswith(str(row.room_id)), "事件不能指向别的房间的会话"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 2. 房间之间互不拖累
# --------------------------------------------------------------------------- #


async def test_removing_one_room_leaves_the_other_running(make_config, tmp_path: Path) -> None:
    """停掉一个房间不应影响另一个：这是"故障隔离"在多房间上的体现。"""
    config = panel_config(make_config)
    hub = WebHub(buffer_size=100)
    panel = PanelService(config, Credentials(), hub)

    async with (
        FakeDanmakuServer(
            notifications=[danmaku(f"A-{i}", f"a{i}") for i in range(1, 40)], notify_delay=0.02
        ) as server_a,
        FakeDanmakuServer(
            notifications=[danmaku(f"B-{i}", f"b{i}") for i in range(1, 40)], notify_delay=0.02
        ) as server_b,
    ):
        attach_api(
            panel,
            MultiRoomApi(
                {
                    ROOM_A: HostInfo(host="127.0.0.1", ws_port=server_a.ws_port),
                    ROOM_B: HostInfo(host="127.0.0.1", ws_port=server_b.ws_port),
                },
                {
                    ROOM_A: RoomInfo(room_id=ROOM_A, anchor_uid=1, live_status=1),
                    ROOM_B: RoomInfo(room_id=ROOM_B, anchor_uid=2, live_status=1),
                },
            ),
        )
        await panel.add_room(ROOM_A)
        await panel.add_room(ROOM_B)
        assert await wait_for(lambda: hub.published >= 2)

        assert await panel.remove_room(ROOM_A) is True
        assert panel.room_ids() == [ROOM_B]
        assert await panel.remove_room(ROOM_A) is False, "重复删除应返回 False"

        before = hub.published
        assert await wait_for(lambda: hub.published > before), "另一个房间应继续采集"

        snapshot = panel.rooms()[0]
        assert snapshot["room_id"] == ROOM_B
        assert snapshot["conn_state"] in {"STREAMING", "AUTHED", "STARTING", "CONNECTING"}
        assert snapshot["stored"] >= 1

        # 健康快照要带上内容范围开关，面板据此显示"当前采集范围"
        health = panel.health()
        assert health["rooms"] == 1
        assert health["content_only"] is True

        await panel.stop()

    grouped = jsonl_by_room(tmp_path)
    assert ROOM_B in grouped
    assert {record["room_id"] for record in grouped[ROOM_B]} == {ROOM_B}


# --------------------------------------------------------------------------- #
# 4. 房间信息查询（右侧栏「XXX 的直播间」的数据源）
# --------------------------------------------------------------------------- #


async def test_lookup_room_returns_anchor_title_and_monitored_flag(make_config) -> None:
    config = panel_config(make_config)
    panel = PanelService(config, Credentials(), WebHub())

    async with FakeDanmakuServer(notifications=[danmaku("hello", "h1")]) as server:
        api = attach_api(
            panel,
            MultiRoomApi(
                {ROOM_A: HostInfo(host="127.0.0.1", ws_port=server.ws_port)},
                {ROOM_A: RoomInfo(room_id=ROOM_A, short_id=456, anchor_uid=8431064, live_status=1)},
            ),
        )

        payload = await panel.lookup_room(ROOM_A)
        assert payload["room_id"] == ROOM_A
        assert payload["anchor_name"] == f"主播{ROOM_A}"
        assert payload["title"] == f"主播{ROOM_A}的直播间"
        assert payload["live_status"] == 1
        assert payload["monitored"] is False, "只查信息不应启动采集"
        assert panel.room_ids() == []

        # 短号也能命中同一房间（缓存按 room_id + short_id 双键查）
        assert (await panel.lookup_room(456))["room_id"] == ROOM_A
        assert api.room_init_calls == [ROOM_A], "第二次查询应命中缓存，不再打接口"

        await panel.add_room(ROOM_A)
        assert (await panel.lookup_room(ROOM_A))["monitored"] is True
        assert panel.rooms()[0]["anchor_name"] == f"主播{ROOM_A}"
        await panel.stop()


async def test_add_room_survives_anchor_lookup_failure(make_config) -> None:
    """主播信息取不到时，采集必须照常启动 —— 昵称只是展示信息。"""
    config = panel_config(make_config)
    panel = PanelService(config, Credentials(), WebHub())

    async with FakeDanmakuServer(notifications=[danmaku("hello", "h1")]) as server:

        class NoAnchorApi(MultiRoomApi):
            async def anchor_info(self, room_id: int):
                return None

        attach_api(
            panel,
            NoAnchorApi(
                {ROOM_A: HostInfo(host="127.0.0.1", ws_port=server.ws_port)},
                {ROOM_A: RoomInfo(room_id=ROOM_A, anchor_uid=8431064, live_status=1)},
            ),
        )
        await panel.add_room(ROOM_A)
        assert panel.rooms()[0]["anchor_name"] is None
        payload = await panel.lookup_room(ROOM_A)
        assert payload["title"] == f"房间 {ROOM_A}", "没有昵称时退化成房间号标题"
        await panel.stop()



async def test_lookup_room_falls_back_to_room_init_when_api_lacks_room_info(make_config) -> None:
    """假 API / 旧实现只有 `room_init` 时，`lookup_room` 必须走单步回退路径。

    这条分支是给"没有 `room_info()` 的 API 对象"留的兼容口（真实 `BiliApi` 有，
    所以只有桩会走到）；不测它的话，回退路径坏了也没人知道。
    """
    config = panel_config(make_config)
    panel = PanelService(config, Credentials(), WebHub())

    class InitOnlyApi:
        def __init__(self, room: RoomInfo) -> None:
            self.room = room
            self.init_calls = 0
            self.anchor_calls = 0

        async def room_init(self, room_input: int | str) -> RoomInfo:
            self.init_calls += 1
            return self.room

        async def anchor_info(self, room_id: int):
            self.anchor_calls += 1
            return {"uid": 8431064, "uname": "只有room_init的桩"}

    api = InitOnlyApi(RoomInfo(room_id=ROOM_A, short_id=456, anchor_uid=8431064, live_status=0))
    panel.api = api  # type: ignore[assignment]

    payload = await panel.lookup_room(ROOM_A)
    assert payload["anchor_name"] == "只有room_init的桩"
    assert payload["title"] == "只有room_init的桩的直播间"
    assert api.init_calls == 1 and api.anchor_calls == 1

    # 第二次查询命中缓存：不再打接口（这条对风控很重要）
    again = await panel.lookup_room(456)
    assert again["room_id"] == ROOM_A
    assert api.init_calls == 1 and api.anchor_calls == 1


async def test_lookup_room_accepts_a_live_url(make_config) -> None:
    """直播间链接要能在 `lookup_room` / `add_room` 里被解析成房间号。

    这是**必需**而不只是方便：粘进来的链接若直接进 `int()` 会被拒，
    用户只能看到一句"添加失败"，无从判断哪里错了。
    """
    config = panel_config(make_config)
    panel = PanelService(config, Credentials(), WebHub())

    async with FakeDanmakuServer(notifications=[danmaku("hello", "h1")]) as server:
        api = attach_api(
            panel,
            MultiRoomApi(
                {ROOM_A: HostInfo(host="127.0.0.1", ws_port=server.ws_port)},
                {ROOM_A: RoomInfo(room_id=ROOM_A, anchor_uid=8431064, live_status=1)},
            ),
        )
        link = f"https://live.bilibili.com/{ROOM_A}?live_from=85001&spm_id_from=333.1365.x"
        payload = await panel.lookup_room(link)
        assert payload["room_id"] == ROOM_A
        assert payload["anchor_name"] == f"主播{ROOM_A}"
        assert api.room_init_calls == [ROOM_A], "链接应先被解析成一个房间号再查"

        await panel.add_room(link)
        assert panel.room_ids() == [ROOM_A]
        await panel.stop()


async def test_lookup_room_rejects_unrecognisable_links(make_config) -> None:
    """识别不出房间号的链接要明确报错，而不是猜一个数字出来。"""
    config = panel_config(make_config)
    panel = PanelService(config, Credentials(), WebHub())

    async with FakeDanmakuServer(notifications=[danmaku("hello", "h1")]) as server:
        api = attach_api(
            panel,
            MultiRoomApi(
                {ROOM_A: HostInfo(host="127.0.0.1", ws_port=server.ws_port)},
                {ROOM_A: RoomInfo(room_id=ROOM_A, anchor_uid=1, live_status=1)},
            ),
        )
        for bad in ("https://example.com/12345", "", "   ", "https://live.bilibili.com/blanc/8432038"):
            with pytest.raises(ValueError):
                await panel.lookup_room(bad)
        assert api.room_init_calls == [], "解析失败时不该打任何接口"
        await panel.stop()


# --------------------------------------------------------------------------- #
# 5. 房间运行时快照
# --------------------------------------------------------------------------- #


async def test_room_snapshot_reports_state_and_session(make_config) -> None:
    config = panel_config(make_config)
    hub = WebHub()
    panel = PanelService(config, Credentials(), hub)

    async with FakeDanmakuServer(notifications=[danmaku("hello", "h1")]) as server:
        attach_api(
            panel,
            MultiRoomApi(
                {ROOM_A: HostInfo(host="127.0.0.1", ws_port=server.ws_port)},
                {ROOM_A: RoomInfo(room_id=ROOM_A, short_id=456, anchor_uid=8431064, live_status=1)},
            ),
        )
        await panel.add_room(str(ROOM_A))
        assert await wait_for(lambda: hub.published >= 1)
        await asyncio.sleep(0.15)

        snapshot: dict[str, Any] = panel.rooms()[0]
        assert snapshot["room_id"] == ROOM_A
        assert snapshot["short_id"] == 456
        assert snapshot["anchor_uid"] == 8431064
        assert snapshot["anchor_name"] == f"主播{ROOM_A}", "快照要带上主播昵称供右侧栏显示"
        assert snapshot["stored"] >= 1
        assert snapshot["session_id"] == snapshot["session_id"].strip()
        assert snapshot["uptime_s"] >= 0
        assert snapshot["token_source"] == "getConf"
        assert panel.session_meta_for(ROOM_A) is not None
        assert panel.session_meta_for(999999) is None

        await panel.stop()

    assert panel.room_count == 0
    assert panel.health()["status"] == "stopping"
