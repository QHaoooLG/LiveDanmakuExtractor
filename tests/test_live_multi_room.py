"""M4：**真实**多房间并发验收（需要联网 + 有效凭据，默认跳过）。

单测里的多房间用的是两个假服务端；真正的验收必须打通真实链路：
两个房间各建一条 WS、各自认证、各自落盘，并且互不串台。

跑法（用你的两个真实房间号替换；**支持短号**，会按 `room_init` 解析成真实房间号）：

    $env:BILI_LIVE_ROOMS="804456,7878"
    .\\.venv\\Scripts\\python.exe -m pytest -m live tests/test_live_multi_room.py -v -s

留空或不设置 `BILI_LIVE_ROOMS` 时自动跳过 —— 这样默认的 `pytest` 永远是离线的。

实测参考（2026-09-21）：
    804456 很冷（约 10 条/分钟，常常整段窗口只有历史补种）；
    7878（真实房间 23438368）很热（实测 ~500 条/分钟，收 580 条时入库 423、过滤 167）。
想要压力/背压，就把热门房间放在第一个位置。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

ROOMS_ENV = "BILI_LIVE_ROOMS"
DURATION_ENV = "BILI_LIVE_SECONDS"
DEFAULT_DURATION = 60.0


def live_rooms() -> list[int]:
    raw = os.environ.get(ROOMS_ENV, "").strip()
    if not raw:
        pytest.skip(f"未设置 {ROOMS_ENV}（形如 '804456,123456'），跳过真实多房间验收")
    return [int(item) for item in raw.replace("，", ",").split(",") if item.strip()]


def live_duration() -> float:
    return float(os.environ.get(DURATION_ENV, DEFAULT_DURATION))


async def test_two_real_rooms_collect_concurrently(tmp_path: Path) -> None:
    """真实链路：N 个房间并发采集，各自会话、各自落盘、互不串台。"""
    from bili_danmaku.config import Config
    from bili_danmaku.credentials import load_credentials, save_credentials
    from bili_danmaku.server.hub import WebHub
    from bili_danmaku.server.panel import PanelService

    credentials = load_credentials()
    if not credentials.logged_in:
        pytest.skip("未登录：先执行 bili-danmaku login（匿名下昵称会被打码且更易触发风控）")

    rooms = live_rooms()
    duration = live_duration()

    config = Config.load(
        path="definitely-not-a-real-config.toml",
        overrides={
            "general": {"max_rooms": max(len(rooms), 3)},
            "sinks": {
                "out_dir": str(tmp_path),
                "types": ["jsonl", "csv", "sqlite"],
                "queue_size": 20000,
                "flush_interval": 0.5,
                "batch_size": 20,
            },
        },
    )

    hub = WebHub()
    panel = PanelService(config, credentials, hub)
    runtimes = []
    try:
        await panel.start()
        for room in rooms:
            runtime = await panel.add_room(room)
            runtimes.append(runtime)

        # 房间号可能是短号（如 7878 → 真实房间号 23438368），
        # 所以后面一律按 room_init 解析出的真实 id 来核对，而不是用户输入的那个数。
        resolved = {runtime.room.room_id: runtime for runtime in runtimes}
        if len(resolved) != len(runtimes):
            pytest.fail(f"不同输入解析到了同一个房间: {[r.room.room_id for r in runtimes]}")

        print(f"\n已开始监控 {len(runtimes)} 个房间，采集 {duration:.0f} 秒…")
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            await asyncio.sleep(5.0)
            for snapshot in panel.rooms():
                print(
                    f"  [{snapshot['room_id']}] {snapshot['conn_state']:<12} "
                    f"收 {snapshot['received']:>5} 入库 {snapshot['stored']:>5} "
                    f"过滤 {snapshot['filtered']:>5} 重连 {snapshot['reconnects']} "
                    f"人气 {snapshot['popularity']}"
                )

        snapshots = {snapshot["room_id"]: snapshot for snapshot in panel.rooms()}
        assert set(snapshots) == set(resolved), "监控中的房间集合应与添加的一致"

        # 1) 每个房间都必须真的连上过（重连可以有，但状态不能一直卡在失败）
        for room_id, runtime in resolved.items():
            snapshot = snapshots[room_id]
            state = str(getattr(snapshot["conn_state"], "value", snapshot["conn_state"]))
            assert not state.startswith("FAILED"), (
                f"房间 {room_id}（输入 {runtime.request}）连接失败: {snapshot['error']}"
            )
            # 冷房间可能整段窗口没有新弹幕，但至少有历史补种；两者都 0 才算链路没通
            assert snapshot["received"] > 0 or snapshot["stored"] > 0, (
                f"房间 {room_id} 既没收到实时消息也没有历史补种，链路可能没通"
            )
            assert snapshot["token_source"], "应记录 token 来源（getConf / getDanmuInfo+wbi）"
            print(
                f"房间 {room_id}（输入 {runtime.request}）: token={snapshot['token_source']} "
                f"session={snapshot['session_id']} 收={snapshot['received']} 入库={snapshot['stored']}"
            )

        # 2) 会话必须互不相同，且各自的 session_id 指向自己
        session_ids = {room_id: snapshot["session_id"] for room_id, snapshot in snapshots.items()}
        assert len(set(session_ids.values())) == len(session_ids), f"会话应互不相同: {session_ids}"
        for room_id, session_id in session_ids.items():
            assert session_id.endswith(str(room_id)), f"{session_id} 与房间 {room_id} 不匹配"

        # 3) 落盘按房间隔离：JSONL 文件名带 room_id，内容不串台
        files = sorted(tmp_path.glob("*.jsonl"))
        print("落盘文件:", [path.name for path in files])
        assert files, "应产出 JSONL"
        for path in files:
            room_id = int(path.name.split("_", 1)[0])
            assert room_id in resolved, f"{path.name} 对应一个没在监控的房间"
    finally:
        await panel.stop()

    # 4) SQLite 同库共存，且每行都指向本房间的会话
    from bili_danmaku.store import DanmakuStore

    db = tmp_path / "danmaku.db"
    assert db.is_file()
    store = DanmakuStore(db)
    try:
        for room_id in resolved:
            count = store.count(room_id=room_id)
            print(f"SQLite 房间 {room_id}: {count} 条")
            for event in store.iter_events(room_id=room_id, limit=2000):
                assert event.room_id == room_id
                assert event.session_id.endswith(str(room_id)), "事件不得指向别的房间的会话"
            if count:
                assert {event.type for event in store.iter_events(room_id=room_id, limit=2000)} <= {
                    "danmaku",
                    "super_chat",
                }, "默认采集范围只应有文字弹幕与 SC"
    finally:
        store.close()

    # 5) 顺带记录真实采集速率，供压测与后续排障参考
    for room_id, snapshot in snapshots.items():
        rate = snapshot["received"] * 60.0 / max(duration, 1.0)
        print(
            f"房间 {room_id} 速率: 收 {rate:.1f} 条/分钟，"
            f"入库 {snapshot['stored']}，过滤 {snapshot['filtered']}，"
            f"重连 {snapshot['reconnects']}，丢弃 {snapshot['dropped']}"
        )


async def test_adding_beyond_max_rooms_is_rejected_offline_safely() -> None:
    """真实 API 下的上限行为：第 N+1 个房间应被拒绝，且**不产生任何网络请求**。

    用配置把上限设成 1，先加一个真实房间占满名额，再用一个**确定不存在**的房间号
    触发拒绝 —— 如果代码顺序写反（先 `room_init` 再查上限），这里会先打一次网络接口，
    然后抛出"房间不存在"(60004)，把这个测试和真正的原因一起掩盖掉（这正是 D14）。
    """
    from bili_danmaku.config import Config
    from bili_danmaku.credentials import load_credentials
    from bili_danmaku.server.hub import WebHub
    from bili_danmaku.server.panel import PanelService

    credentials = load_credentials()
    if not credentials.logged_in:
        pytest.skip("未登录")

    occupied = live_rooms()[0]  # 未设置 BILI_LIVE_ROOMS 时内部已 skip

    config = Config.load(
        path="definitely-not-a-real-config.toml",
        overrides={"general": {"max_rooms": 1}},
    )
    panel = PanelService(config, credentials, WebHub())
    try:
        await panel.start()
        await panel.add_room(occupied)
        assert panel.room_count == 1

        with pytest.raises(ValueError, match="并发上限"):
            await panel.add_room(999_999_999)
    finally:
        await panel.stop()
