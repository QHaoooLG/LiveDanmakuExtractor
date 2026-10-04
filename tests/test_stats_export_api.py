"""`/api/stats/export` 与 `store.density` 的时间边界。

两条被实测抓出来的东西：

1. **`density()` 的 `range` 必须服从 `since`/`until`** —— 否则界面会出现
   "概要行说跨了 264 小时、但一根柱子都没画"这种自相矛盾的样子
   （需求 3 修时间轴起点时实测到）。
2. **`/api/stats/export` 的默认格宽必须是 5 分钟**（需求约定），
   且要能在同一个 CSV 里给出多张表。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bili_danmaku.config import Config
from bili_danmaku.models import Event, SessionMeta
from bili_danmaku.server import WebHub, create_app
from bili_danmaku.server.panel import PanelService
from bili_danmaku.sinks import SqliteSink
from bili_danmaku.store import DanmakuStore

ROOM = 804456
BASE_TS = 1_789_710_200.0


def make_event(seq: int, *, room_id: int = ROOM, offset: float = 0.0) -> Event:
    return Event(
        seq=seq,
        session_id="20260918-141801-804456",
        room_id=room_id,
        type="danmaku",
        cmd="DANMU_MSG",
        recv_ts=BASE_TS + offset,
        server_ts=int(BASE_TS),
        msg_id=f"m{seq}",
        uid=1000 + seq,
        uname=f"用户{seq}",
        text=f"第 {seq} 条",
        extra={},
    )


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """100 条弹幕，时间跨度 100 秒（00:00 起每条 +1s）。"""
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"out_dir": str(tmp_path), "types": ["sqlite"], "keep_raw": False}},
    )
    sink = SqliteSink(config)
    sink.open(SessionMeta(session_id="20260918-141801-804456", room_id=ROOM, started_at=1.0))
    sink.write([make_event(i, offset=float(i)) for i in range(100)])
    sink.close()
    return tmp_path / "danmaku.db"


@pytest.fixture()
def client(db: Path, tmp_path: Path) -> TestClient:
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"out_dir": str(tmp_path), "types": ["sqlite"], "keep_raw": False}},
    )
    hub = WebHub()
    panel = PanelService(config, credentials=None, hub=hub)
    return TestClient(create_app(config, None, hub, panel))


# --------------------------------------------------------------------------- #
# store.density 的时间边界
# --------------------------------------------------------------------------- #

def test_density_range_respects_since(db: Path) -> None:
    """`since` 之后没有数据时，`range` 必须是 `(None, None)`，而不是全库范围。

    回归：只过滤桶、不过滤 `range` 会让概要行报出一个**没有柱子的**跨度。
    """
    store = DanmakuStore(db)
    try:
        whole = store.density(bucket_s=60)
        assert whole["range"]["lo"] == pytest.approx(BASE_TS)
        assert whole["range"]["hi"] == pytest.approx(BASE_TS + 99)

        empty = store.density(bucket_s=60, since=BASE_TS + 10_000)
        assert empty["buckets"] == []
        assert empty["range"] == {"lo": None, "hi": None}
    finally:
        store.close()


def test_density_range_narrows_to_window(db: Path) -> None:
    """给了 `since` 后，`lo` 应是**该范围内第一条数据**的时间（不是 max(since, lo)）。"""
    store = DanmakuStore(db)
    try:
        # 从第 40 秒开始：范围内第一条数据的 recv_ts = BASE_TS + 40
        payload = store.density(bucket_s=60, since=BASE_TS + 40)
        assert payload["range"]["lo"] == pytest.approx(BASE_TS + 40)
        assert payload["range"]["hi"] == pytest.approx(BASE_TS + 99)
        assert payload["total"] == 60  # 第 40..99 条
    finally:
        store.close()


def test_density_until_is_inclusive(db: Path) -> None:
    store = DanmakuStore(db)
    try:
        payload = store.density(bucket_s=60, until=BASE_TS + 9)
        assert payload["total"] == 10
        assert payload["range"]["hi"] == pytest.approx(BASE_TS + 9)
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# /api/stats/export
# --------------------------------------------------------------------------- #

def test_stats_export_defaults_to_five_minutes(client: TestClient) -> None:
    """需求：web 端没选时段时长时，按 5 分钟导出。"""
    response = client.get("/api/stats/export")
    assert response.status_code == 200
    payload = response.json()
    assert payload["bucket_s"] == 300.0


def test_stats_export_honours_requested_bucket(client: TestClient) -> None:
    """web 端选了时段时长就按它导出（1/3 分钟也要能传）。"""
    for span in (60, 180, 900):
        payload = client.get(f"/api/stats/export?bucket_s={span}").json()
        assert float(payload["bucket_s"]) == float(span)


def test_stats_export_csv_has_multiple_tables(client: TestClient) -> None:
    payload = client.get("/api/stats/export").json()
    csv_text = payload["csv"]
    for title in ("弹幕统计 · 概要", "弹幕统计 · 类型分布", "每时段弹幕量统计", "弹幕统计 · 发言 TOP"):
        assert f"# {title}" in csv_text


def test_stats_export_reports_a_room_for_the_filename(client: TestClient) -> None:
    """文件名要 `<主播名字>_<房间号>_...`，所以必须给出一个房间号。"""
    payload = client.get("/api/stats/export").json()
    assert payload["room_id"] == ROOM


def test_stats_export_respects_room_filter(client: TestClient) -> None:
    payload = client.get(f"/api/stats/export?room_id={ROOM}").json()
    assert payload["room_id"] == ROOM
    assert payload["total"] == 100


def test_stats_export_requires_sqlite(tmp_path: Path) -> None:
    """没启用 sqlite sink 时要 409（与历史浏览/原始导出一致的口径）。"""
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"out_dir": str(tmp_path), "types": ["jsonl"], "keep_raw": False}},
    )
    hub = WebHub()
    panel = PanelService(config, credentials=None, hub=hub)
    client = TestClient(create_app(config, None, hub, panel))
    assert client.get("/api/stats/export").status_code == 409
