"""V3-a 历史浏览的 HTTP 层：`/api/history`、`/api/density`、`/api/sessions`。

关注点：
  1. 契约（响应形状、游标回传、状态码）；
  2. **与 `/api/events` 的宽度分工** —— 新端点返回瘦投影，老端点保持完整 Event；
  3. 未启用 sqlite 时统一 409（不要因为"上次遗留的库文件"而返回数据）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bili_danmaku.config import Config
from bili_danmaku.server.app import create_app, _open_store_or_409
from bili_danmaku.server.hub import WebHub

from test_history import ROOM, build_db


class StubPanel:
    """只为让 `create_app` 的 lifespan 能跑起来（这些用例不碰房间）。"""

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


def make_client(tmp_path, *, rows: int = 60, with_sqlite: bool = True, tied: int = 3):
    db = build_db(tmp_path, rows=rows, tied=tied)
    overrides: dict[str, Any] = {"keep_raw": True}
    if with_sqlite:
        overrides.update({"types": ["sqlite"], "sqlite_path": str(db)})
    else:
        # 关键：此时库文件**仍然存在**（上一次运行的遗留），但配置没启用 sqlite
        overrides.update({"types": ["jsonl"], "sqlite_path": str(db)})
    config = Config.load(path="missing.toml", overrides={"sinks": overrides})
    app = create_app(config, None, WebHub(), StubPanel())  # type: ignore[arg-type]
    return TestClient(app, client=("127.0.0.1", 51234)), db


# --------------------------------------------------------------------------- #
# /api/history
# --------------------------------------------------------------------------- #


def test_history_first_page_contract(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        payload = client.get("/api/history", params={"room_id": ROOM, "limit": 5}).json()

    assert set(payload) == {
        "items", "has_older", "has_newer", "oldest", "newest",
        "source", "room_id", "session_id", "window_s", "range",
    }
    assert payload["source"] == "sqlite"
    assert payload["room_id"] == ROOM
    assert payload["window_s"] == 0.0, "有 room_id 时不需要兜底时间窗"
    assert len(payload["items"]) == 5
    assert payload["has_older"] is True and payload["has_newer"] is False
    assert payload["oldest"] == [payload["items"][0]["recv_ts"], payload["items"][0]["id"]]
    assert payload["newest"] == [payload["items"][-1]["recv_ts"], payload["items"][-1]["id"]]
    assert payload["range"]["lo"] is not None


def test_history_items_are_narrower_than_events(tmp_path) -> None:
    """瘦投影与完整 Event 的分工不能混（ROADMAP §6.1 的契约）。"""
    client, _ = make_client(tmp_path)
    with client:
        slim = client.get("/api/history", params={"room_id": ROOM, "limit": 20})
        full = client.get("/api/events", params={"room_id": ROOM, "limit": 20})

    slim_item = slim.json()["items"][0]
    full_item = full.json()["items"][0]
    assert "raw" in full_item, "老端点必须保持完整 Event"
    assert "raw" not in slim_item and "cmd" not in slim_item
    assert len(slim.content) < len(full.content), "瘦投影必须真的更小"

    # 逐字节量级：瘦投影应显著小于完整事件
    per_slim = len(slim.content) / 20
    per_full = len(full.content) / 20
    assert per_full > per_slim * 3, f"投影收益不足：{per_full:.0f} B vs {per_slim:.0f} B"


def test_history_cursor_paging_has_no_gap_or_overlap(tmp_path) -> None:
    client, _ = make_client(tmp_path, rows=50)
    with client:
        seen: list[int] = []
        cursor: dict[str, Any] = {}
        for _ in range(30):
            payload = client.get(
                "/api/history", params={"room_id": ROOM, "limit": 6, **cursor}
            ).json()
            items = payload["items"]
            if not items:
                break
            seen.extend(item["id"] for item in items)
            cursor = {"before_ts": payload["oldest"][0], "before_id": payload["oldest"][1]}
            if not payload["has_older"]:
                break

    assert len(seen) == len(set(seen)), "翻页出现重复"
    assert sorted(seen) == list(range(1, 51)), "翻页漏条"


def test_history_rejects_conflicting_cursors(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        response = client.get(
            "/api/history",
            params={"before_ts": 1, "before_id": 1, "after_ts": 2, "after_id": 2},
        )
    assert response.status_code == 400
    assert "不能同时" in response.json()["detail"]


def test_history_ignores_half_a_cursor(tmp_path) -> None:
    """只给 `before_ts` 不给 `before_id` 时按"没有游标"处理，而不是报错或漏条。"""
    client, _ = make_client(tmp_path)
    with client:
        body = client.get("/api/history", params={"room_id": ROOM, "limit": 4, "before_ts": 0}).json()
    assert len(body["items"]) == 4
    assert body["items"][-1]["id"] == 60, "应当作首屏（最新一页）"


def test_history_all_rooms_uses_fallback_window(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        body = client.get("/api/history", params={"limit": 5}).json()
    assert body["room_id"] is None
    assert body["window_s"] == 3600.0, "无房间过滤时必须启用兜底时间窗"
    assert body["items"], "锚点应落在数据上，而不是墙钟"


def test_history_limit_bounds(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        assert client.get("/api/history", params={"limit": 0}).status_code == 422
        assert client.get("/api/history", params={"limit": 100000}).status_code == 422


def test_history_type_filter(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        body = client.get(
            "/api/history", params={"room_id": ROOM, "type": "super_chat", "limit": 50}
        ).json()
    assert body["items"] and all(item["type"] == "super_chat" for item in body["items"])


# --------------------------------------------------------------------------- #
# /api/density
# --------------------------------------------------------------------------- #

def test_density_contract(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        body = client.get("/api/density", params={"room_id": ROOM}).json()

    assert set(body) == {"bucket_s", "buckets", "total", "by_type", "range"}
    assert body["bucket_s"] > 0, "缺省时必须自适应出一个可读步长"
    assert body["total"] == 60
    assert sum(bucket["count"] for bucket in body["buckets"]) == 60
    assert sum(body["by_type"].values()) == 60
    for bucket in body["buckets"]:
        assert set(bucket) == {"ts", "count", "by_type"}


def test_density_bucket_override_and_type_filter(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        body = client.get(
            "/api/density", params={"room_id": ROOM, "bucket_s": 1, "type": "danmaku"}
        ).json()
    assert body["bucket_s"] == 1
    assert set(body["by_type"]) <= {"danmaku"}
    assert body["total"] == sum(1 for i in range(1, 61) if i % 7)


def test_density_rejects_bad_bucket(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        assert client.get("/api/density", params={"bucket_s": 0}).status_code == 422
        assert client.get("/api/density", params={"bucket_s": 999999}).status_code == 422


# --------------------------------------------------------------------------- #
# /api/sessions
# --------------------------------------------------------------------------- #


def test_sessions_contract(tmp_path) -> None:
    client, _ = make_client(tmp_path, rows=12)
    with client:
        body = client.get("/api/sessions", params={"room_id": ROOM}).json()
    assert body["source"] == "sqlite"
    assert [item["id"] for item in body["items"]] == ["s1"]
    assert body["items"][0]["events"] == 12
    assert body["items"][0]["room_id"] == ROOM


def test_sessions_unknown_room_is_empty(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        body = client.get("/api/sessions", params={"room_id": 999999}).json()
    assert body["items"] == []


# --------------------------------------------------------------------------- #
# 未启用 sqlite：统一 409
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path", ["/api/history", "/api/density", "/api/sessions"]
)
def test_history_endpoints_need_sqlite_sink(tmp_path, path: str) -> None:
    """库文件存在但配置未启用 sqlite 时也必须 409。

    否则用户会看到"上次运行遗留的旧数据"，却以为在看这次的采集。
    """
    client, db = make_client(tmp_path, with_sqlite=False)
    assert db.is_file(), "前置条件：库文件确实存在"
    with client:
        response = client.get(path, params={"room_id": ROOM})
    assert response.status_code == 409
    assert "sqlite" in response.json()["detail"]


def test_open_store_or_409_helper(tmp_path) -> None:
    client, db = make_client(tmp_path, with_sqlite=False)
    del client
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"types": ["jsonl"], "sqlite_path": str(db)}},
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        _open_store_or_409(config)
    assert excinfo.value.status_code == 409

    enabled = Config.load(
        path="missing.toml",
        overrides={"sinks": {"types": ["sqlite"], "sqlite_path": str(db)}},
    )
    store = _open_store_or_409(enabled)
    try:
        assert store.count(room_id=ROOM) == 60
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 与 WS 的关系：历史端点不应影响实时推送
# --------------------------------------------------------------------------- #


def test_history_endpoints_do_not_publish_to_hub(tmp_path) -> None:
    """历史浏览是纯读：不该往 WS 广播任何东西（否则会污染订阅者）。"""
    db = build_db(tmp_path)
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"types": ["sqlite"], "sqlite_path": str(db)}},
    )
    hub = WebHub()
    app = create_app(config, None, hub, StubPanel())  # type: ignore[arg-type]
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        subscriber = None  # 无订阅者时只检查 published 计数
        before = hub.published
        client.get("/api/history", params={"room_id": ROOM})
        client.get("/api/density", params={"room_id": ROOM})
        client.get("/api/sessions", params={"room_id": ROOM})
        assert hub.published == before
        assert subscriber is None
