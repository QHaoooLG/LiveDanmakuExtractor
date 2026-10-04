"""标注存储（V3-a2）：`annotations.py` + `/api/annotations*`。

关注三点：
  1. **独立库**：标注库与采集库完全不共享文件/表/schema；
  2. 只读模式**不建文件**（面板首屏拉列表不该在工作区凭空留一个空库）；
  3. 按标记区间导出的**语义**：只给起点时终点取"下一个标记"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bili_danmaku.annotations import (
    MAX_LABEL_LEN,
    AnnotationError,
    AnnotationStore,
    resolve_annotations_path,
    summarize,
)
from bili_danmaku.config import Config
from bili_danmaku.server.app import create_app
from bili_danmaku.server.hub import WebHub

from test_history import ROOM, build_db


class StubPanel:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def rooms(self) -> list[dict[str, Any]]:
        return []

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "rooms": 0, "sinks": ["sqlite"]}

    async def lookup_room(self, room_input: Any) -> dict[str, Any]:
        return {"room_id": int(room_input)}


def make_client(tmp_path, *, rows: int = 60, tied: int = 3, with_sqlite: bool = True):
    """采集库 + 标注库都落在 tmp_path，两者是**不同文件**。"""
    db = build_db(tmp_path, rows=rows, tied=tied)
    annotations = tmp_path / "annotations.db"
    overrides: dict[str, Any] = {
        "annotations_path": str(annotations),
        "sqlite_path": str(db),
        "types": ["sqlite"] if with_sqlite else ["jsonl"],
    }
    config = Config.load(path="missing.toml", overrides={"sinks": overrides})
    app = create_app(config, None, WebHub(), StubPanel())  # type: ignore[arg-type]
    return TestClient(app, client=("127.0.0.1", 51234)), db, annotations


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #


def test_add_and_list_roundtrip(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path / "a.db")
    try:
        item = store.add(room_id=ROOM, recv_ts=1_790_000_010.0, label="高能", note="笑死")
        assert item["label"] == "高能"
        assert item["note"] == "笑死"
        assert item["room_id"] == ROOM
        assert item["anchor_id"] is None, "纯时间点标注的 anchor_id 为空"
        assert item["created_at"] > 0

        store.add(room_id=ROOM, recv_ts=1_790_000_005.0, anchor_id=42)
        items = store.list(room_id=ROOM)
        assert [i["recv_ts"] for i in items] == [1_790_000_005.0, 1_790_000_010.0], "按时间升序"
        assert items[0]["label"] == "标记", "缺省标签"
        assert items[0]["anchor_id"] == 42
    finally:
        store.close()


def test_default_label_and_length_limits(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path / "a.db")
    try:
        assert store.add(room_id=ROOM, recv_ts=1.0, label="   ")["label"] == "标记"
        with pytest.raises(ValueError, match="label 过长"):
            store.add(room_id=ROOM, recv_ts=1.0, label="x" * (MAX_LABEL_LEN + 1))
        with pytest.raises(ValueError, match="note 过长"):
            store.add(room_id=ROOM, recv_ts=1.0, note="y" * 3000)
        with pytest.raises(ValueError, match="recv_ts"):
            store.add(room_id=ROOM, recv_ts=0)
    finally:
        store.close()


def test_delete_and_clear(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path / "a.db")
    try:
        first = store.add(room_id=ROOM, recv_ts=1.0)
        store.add(room_id=ROOM, recv_ts=2.0)
        store.add(room_id=999, recv_ts=3.0)

        assert store.delete(first["id"]) is True
        assert store.delete(first["id"]) is False, "重复删除返回 False"
        with pytest.raises(KeyError):
            store.get(first["id"])

        assert store.clear(room_id=ROOM) == 1
        assert store.list(room_id=ROOM) == []
        assert len(store.list(room_id=999)) == 1, "只清指定房间"
        assert store.clear() == 1
        assert store.list() == []
    finally:
        store.close()


def test_readonly_does_not_create_the_file(tmp_path: Path) -> None:
    """只读模式不该在工作区凭空留下空库（面板首屏就会拉一次列表）。"""
    path = tmp_path / "missing.db"
    store = AnnotationStore(path, readonly=True)
    try:
        assert store.available is False
        assert store.list(room_id=ROOM) == []
        assert store.nearest_before(999.0) is None
        assert store.bounds() == (None, None)
        with pytest.raises(AnnotationError):
            store.add(room_id=ROOM, recv_ts=1.0)
    finally:
        store.close()
    assert not path.exists(), "只读模式创建了文件"


def test_readonly_reads_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    writer = AnnotationStore(path)
    try:
        writer.add(room_id=ROOM, recv_ts=1_790_000_000.0, label="甲")
    finally:
        writer.close()

    reader = AnnotationStore(path, readonly=True)
    try:
        assert reader.available is True
        assert [i["label"] for i in reader.list(room_id=ROOM)] == ["甲"]
        with pytest.raises(AnnotationError):
            reader.delete(1)
    finally:
        reader.close()


def test_time_filters_and_bounds(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path / "a.db")
    try:
        for offset in (0.0, 10.0, 20.0):
            store.add(room_id=ROOM, recv_ts=1_790_000_000.0 + offset)
        assert len(store.list(room_id=ROOM, since=1_790_000_005.0)) == 2
        assert len(store.list(room_id=ROOM, until=1_790_000_005.0)) == 1
        assert store.bounds(room_id=ROOM) == (1_790_000_000.0, 1_790_000_020.0)
        assert store.bounds(room_id=999) == (None, None)
        assert summarize(store.list(room_id=ROOM)) == {
            "count": 3,
            "lo": 1_790_000_000.0,
            "hi": 1_790_000_020.0,
        }
        assert summarize([]) == {"count": 0, "lo": None, "hi": None}
    finally:
        store.close()


def test_nearest_before_picks_the_closest_earlier_one(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path / "a.db")
    try:
        store.add(room_id=ROOM, recv_ts=100.0, label="A")
        store.add(room_id=ROOM, recv_ts=200.0, label="B")
        store.add(room_id=999, recv_ts=150.0, label="别的房间")
        assert store.nearest_before(150.0, room_id=ROOM)["label"] == "A"
        assert store.nearest_before(250.0, room_id=ROOM)["label"] == "B"
        assert store.nearest_before(50.0, room_id=ROOM) is None
    finally:
        store.close()


def test_annotations_live_in_a_separate_file(tmp_path: Path) -> None:
    """核心约束：标注库与采集库不共享文件，采集库的表一张都不多。"""
    collection = build_db(tmp_path, rows=5)
    annotation = tmp_path / "annotations.db"
    assert collection != annotation

    before = {
        row[0]
        for row in sqlite3.connect(str(collection)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    store = AnnotationStore(annotation)
    try:
        store.add(room_id=ROOM, recv_ts=1.0)
    finally:
        store.close()
    after = {
        row[0]
        for row in sqlite3.connect(str(collection)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert before == after, "标注操作污染了采集库的 schema"
    assert "bookmark" not in after


def test_resolve_path_prefers_explicit_then_out_dir(tmp_path: Path) -> None:
    explicit = Config.load(
        path="missing.toml",
        overrides={"sinks": {"annotations_path": str(tmp_path / "x.db"), "out_dir": "data"}},
    )
    assert resolve_annotations_path(explicit) == tmp_path / "x.db"

    fallback = Config.load(path="missing.toml", overrides={"sinks": {"out_dir": str(tmp_path)}})
    assert resolve_annotations_path(fallback) == tmp_path / "annotations.db"


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #


def test_annotations_list_is_empty_not_an_error(tmp_path) -> None:
    """一条都没打过时应当返回空列表，而不是 404/409。"""
    client, _, annotations = make_client(tmp_path)
    with client:
        response = client.get("/api/annotations", params={"room_id": ROOM})
    assert response.status_code == 200
    assert response.json() == {"items": [], "summary": {"count": 0, "lo": None, "hi": None}, "source": "annotations"}
    assert not annotations.exists(), "只读列表请求不该创建工作区里的空库"


def test_annotations_crud_over_http(tmp_path) -> None:
    client, _, _ = make_client(tmp_path)
    with client:
        created = client.post(
            "/api/annotations",
            json={"room_id": ROOM, "recv_ts": 1_790_000_010.0, "label": "名场面", "note": "看这里"},
        )
        assert created.status_code == 201
        item = created.json()["item"]
        assert item["label"] == "名场面" and item["id"] > 0

        listed = client.get("/api/annotations", params={"room_id": ROOM}).json()
        assert listed["summary"]["count"] == 1
        assert listed["items"][0]["note"] == "看这里"

        assert client.delete(f"/api/annotations/{item['id']}").status_code == 200
        assert client.delete(f"/api/annotations/{item['id']}").status_code == 404
        assert client.get("/api/annotations", params={"room_id": ROOM}).json()["items"] == []


def test_annotation_validation_over_http(tmp_path) -> None:
    client, _, _ = make_client(tmp_path)
    with client:
        assert client.post("/api/annotations", json={"room_id": ROOM}).status_code == 422
        assert (
            client.post("/api/annotations", json={"room_id": ROOM, "recv_ts": 0}).status_code == 422
        )
        bad_label = client.post(
            "/api/annotations",
            json={"room_id": ROOM, "recv_ts": 1.0, "label": "x" * 200},
        )
        assert bad_label.status_code == 422


def test_annotations_do_not_require_sqlite_sink(tmp_path) -> None:
    """标注只依赖时间戳：采集没开 sqlite 也能打标记。"""
    client, _, _ = make_client(tmp_path, with_sqlite=False)
    with client:
        created = client.post("/api/annotations", json={"room_id": ROOM, "recv_ts": 1.0})
        assert created.status_code == 201
        assert client.get("/api/annotations", params={"room_id": ROOM}).json()["summary"]["count"] == 1
        # 但按标记导出需要采集库
        assert client.get("/api/annotations/export", params={"bookmark_id": 1}).status_code == 409


def _seed_marks(client) -> list[int]:
    """在采集库数据范围内打三个等距标记（数据时间戳见 build_db：每 3 条同刻）。"""
    ids = []
    for recv_ts in (1_790_000_003.0, 1_790_000_010.0, 1_790_000_017.0):
        item = client.post(
            "/api/annotations", json={"room_id": ROOM, "recv_ts": recv_ts, "label": f"@{recv_ts:.0f}"}
        ).json()["item"]
        ids.append(item["id"])
    return ids


def test_export_range_from_a_single_bookmark_stops_at_the_next_one(tmp_path) -> None:
    """只给起点时，终点取**下一个标记** —— "把这个标记到下一个标记导出来"。"""
    client, _, _ = make_client(tmp_path)
    with client:
        first, second, _third = _seed_marks(client)
        response = client.get("/api/annotations/export", params={"bookmark_id": first})
        assert response.status_code == 200
        assert response.headers["X-Range-Since"] == "1790000003.0"
        assert response.headers["X-Range-Until"] == "1790000010.0", "终点应为下一个标记"
        rows = [line for line in response.text.splitlines() if line.strip()]
        assert len(rows) - 1 == int(response.headers["X-Total-Count"]), "CSV 行数（去表头）= total"
        assert response.text.startswith("\ufeff"), "CSV 必须带 BOM"

        # 显式给终点
        pair = client.get(
            "/api/annotations/export",
            params={"bookmark_id": first, "to_bookmark_id": _third},
        )
        assert pair.headers["X-Range-Until"] == "1790000017.0"


def test_export_range_last_bookmark_runs_to_the_end(tmp_path) -> None:
    client, _, _ = make_client(tmp_path)
    with client:
        _first, _second, third = _seed_marks(client)
        response = client.get("/api/annotations/export", params={"bookmark_id": third})
        assert response.status_code == 200
        assert response.headers["X-Range-Until"] == "None", "没有下一个标记 ⇒ 到数据末尾"
        assert int(response.headers["X-Total-Count"]) > 0


def test_export_range_reverses_swapped_ends(tmp_path) -> None:
    """起点终点给反了应自动正过来，而不是导出空结果。"""
    client, _, _ = make_client(tmp_path)
    with client:
        first, _second, third = _seed_marks(client)
        response = client.get(
            "/api/annotations/export",
            params={"bookmark_id": third, "to_bookmark_id": first},
        )
        assert response.headers["X-Range-Since"] == "1790000003.0"
        assert response.headers["X-Range-Until"] == "1790000017.0"
        assert int(response.headers["X-Total-Count"]) > 0


def test_export_range_errors(tmp_path) -> None:
    client, _, _ = make_client(tmp_path)
    with client:
        assert client.get("/api/annotations/export").status_code == 400
        assert client.get("/api/annotations/export", params={"bookmark_id": 999}).status_code == 404
        first, _, _ = _seed_marks(client)
        bad = client.get(
            "/api/annotations/export", params={"bookmark_id": first, "to_bookmark_id": 999}
        )
        assert bad.status_code == 404


def test_export_range_supports_formats_and_filters(tmp_path) -> None:
    client, _, _ = make_client(tmp_path)
    with client:
        first, _, _ = _seed_marks(client)
        jsonl = client.get(
            "/api/annotations/export", params={"bookmark_id": first, "format": "jsonl"}
        )
        assert jsonl.status_code == 200
        assert "application/x-ndjson" in jsonl.headers["content-type"]
        first_line = jsonl.text.strip().splitlines()[0]
        assert first_line.startswith("{") and "\"text\"" in first_line

        sc_only = client.get(
            "/api/annotations/export",
            params={"bookmark_id": first, "to_bookmark_id": 999_999, "type": "super_chat"},
        )
        # to_bookmark_id 不存在 → 404（过滤参数不会掩盖错误）
        assert sc_only.status_code == 404


def test_annotations_do_not_touch_the_collection_db(tmp_path) -> None:
    """走完整 HTTP 路径也不该往采集库写任何东西。"""
    client, db, _ = make_client(tmp_path)
    with client:
        before = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
            "SELECT COUNT(*) FROM event"
        ).fetchone()[0]
        _seed_marks(client)
        client.get("/api/annotations", params={"room_id": ROOM})
        after = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
            "SELECT COUNT(*) FROM event"
        ).fetchone()[0]
    assert before == after
