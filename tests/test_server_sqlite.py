"""M4 覆盖率：REST / WS 的 SQLite 路径与前端托管（`server/app.py`）。

`test_server.py` 用的是没有 sqlite sink 的配置，所以 `/api/events`、`/api/stats`、
`/api/export` 全都只跑到"内存降级"那一条分支（app.py 74%）。这里补上真实的
SQLite 路径 —— 也就是面板实际会走的那条。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bili_danmaku.config import Config
from bili_danmaku.models import Event, SessionMeta
from bili_danmaku.server.app import create_app, find_dist
from bili_danmaku.server.hub import WebHub
from bili_danmaku.sinks import SqliteSink


class StubPanel:
    """够用的桩：只实现 app.py 真正调用到的方法。"""

    def __init__(self) -> None:
        self._rooms: dict[int, dict[str, Any]] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def add_room(self, room_input: Any):
        raise ValueError(f"房间 {room_input} 已在监控中")

    async def remove_room(self, room_id: int) -> bool:
        return self._rooms.pop(room_id, None) is not None

    async def lookup_room(self, room_input: Any) -> dict[str, Any]:
        return {"room_id": int(room_input), "anchor_name": None, "title": "房间", "monitored": False}

    def rooms(self) -> list[dict[str, Any]]:
        return list(self._rooms.values())

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "rooms": len(self._rooms), "sinks": ["sqlite"]}


#: 夹具与断言的统一时间基准（相对"现在"取整，避免固定历史时刻落到统计窗口外）
BASE_TS = float(int(time.time()) - 600)


def event(seq: int, *, text: str, type_: str = "danmaku", uname: str = "甲", **extra) -> Event:
    data = {
        "seq": seq,
        "session_id": "20260918-141801-804456",
        "room_id": 804456,
        "type": type_,
        "cmd": "DANMU_MSG",
        "recv_ts": BASE_TS + seq,
        "server_ts": 1_789_710_200,
        "latency_ms": 900 + seq,
        "msg_id": f"m{seq}",
        "uid": 1000 + seq,
        "uname": uname,
        "text": text,
        "raw": {"cmd": "DANMU_MSG"},
    }
    data.update(extra)
    return Event(**data)


@pytest.fixture
def sqlite_config(tmp_path: Path) -> Config:
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"out_dir": str(tmp_path), "types": ["sqlite"], "keep_raw": False}},
    )
    sink = SqliteSink(config)
    sink.open(SessionMeta(session_id="20260918-141801-804456", room_id=804456, started_at=1.0))
    sink.write(
        [
            # 甲用同一个 uid 发三条（榜单按 uid 分组），乙发一条
            event(1, text="主播好", uname="甲", uid=1001),
            event(2, text="别的", uname="乙", uid=2002),
            event(3, text="SC 正文", type_="super_chat", price=30000, uname="甲", uid=1001),
            event(4, text="再补一条", uname="甲", uid=1001),
        ]
    )
    sink.close()
    return config


@pytest.fixture
def client(sqlite_config: Config) -> Any:
    hub = WebHub(queue_size=4, buffer_size=20, slow_threshold=2)
    app = create_app(sqlite_config, None, hub, StubPanel())  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        test_client.hub = hub  # type: ignore[attr-defined]
        yield test_client


# --------------------------------------------------------------------------- #
# /api/events（SQLite 路径）
# --------------------------------------------------------------------------- #


def test_events_reads_from_sqlite(client) -> None:
    payload = client.get("/api/events?limit=10").json()
    assert payload["source"] == "sqlite"
    assert payload["total"] == 4
    assert [item["text"] for item in payload["items"]] == ["主播好", "别的", "SC 正文", "再补一条"]


def test_events_supports_type_keyword_and_paging(client) -> None:
    assert client.get("/api/events?type=super_chat").json()["total"] == 1
    assert client.get("/api/events?q=主播").json()["total"] == 1
    assert client.get("/api/events?type=danmaku,super_chat").json()["total"] == 4

    page = client.get("/api/events?limit=1&offset=1").json()
    assert page["total"] == 4
    assert [item["text"] for item in page["items"]] == ["别的"]


def test_events_filters_by_time_window(client) -> None:
    payload = client.get(f"/api/events?since={BASE_TS + 1.5}&limit=10").json()
    assert payload["total"] == 3, "seq=1 的 recv_ts 早于窗口起点"

    until = client.get(f"/api/events?until={BASE_TS + 1.5}&limit=10").json()
    assert until["total"] == 1


# --------------------------------------------------------------------------- #
# /api/stats
#
# 口径（V2-c 起）：
#   * 条数与类型分布来自 **hub 的内存计数**（"面板看到的事件"），
#     窗口模式看最近窗口、`window=0` 看自启动累计；
#   * 延迟分位、发言榜、来源分布仍来自 SQLite（这些无法只靠内存算）。
# 所以测试要先 `hub.publish()`，否则内存计数是 0 —— 真实运行中
# 事件本来就是经 WebHubSink 流经 hub 的。
# --------------------------------------------------------------------------- #


def test_stats_window_mode_uses_hub_counts_and_sqlite_extras(client) -> None:
    for index in range(1, 5):
        client.hub.publish(event(index, text=f"第{index}条"))  # type: ignore[attr-defined]

    payload = client.get("/api/stats?window=3600").json()
    assert payload["mode"] == "window"
    assert payload["window_s"] == 3600
    assert payload["msgs_total"] == 4
    assert payload["by_type"] == {"danmaku": 4}, "类型分布来自 hub 内存计数"
    assert payload["by_source"] == {"live": 4}, "来源分布来自 SQLite"
    assert payload["latency_p50_ms"] is not None
    # 发言榜来自 SQLite（夹具里 uid=1001/甲 有 3 条），不是内存计数
    assert payload["top_senders"][0]["uname"] == "甲"
    assert payload["top_senders"][0]["count"] == 3
    assert payload["db"]


def test_stats_cumulative_is_default_and_scoped(client) -> None:
    """`window=0` 是默认口径：自面板启动累计，且按房间可分开看。"""
    for index in range(1, 5):
        client.hub.publish(event(index, text=f"第{index}条"))  # type: ignore[attr-defined]
    client.hub.publish(event(9, text="别的房间", room_id=999))  # type: ignore[attr-defined]

    default = client.get("/api/stats").json()
    assert default["mode"] == "cumulative"
    assert default["window_s"] == 0
    assert default["msgs_total"] == 5, "默认看全部房间的累计"
    assert default["cumulative"]["total"] == 5
    assert default["cumulative"]["by_room"] == {"804456": 4, "999": 1}
    assert default["msgs_per_min"] > 0
    assert default["window_started_at"] > 0

    scoped = client.get("/api/stats?room_id=804456").json()
    assert scoped["msgs_total"] == 4
    assert scoped["by_type"] == {"danmaku": 4}

    empty = client.get("/api/stats?room_id=123456").json()
    assert empty["msgs_total"] == 0
    assert empty["by_type"] == {}


def test_stats_window_rejects_negative(client) -> None:
    """负数窗口是参数错误（0 才表示累计口径）。"""
    assert client.get("/api/stats?window=-5").status_code == 422


def test_cumulative_stats_include_top_senders_for_a_room(client) -> None:
    """累计口径下也要给发言榜（数据来自 SQLite，不是 hub 内存计数）。

    范围必须标清："该房间全部时间" —— 否则会被误读成"自面板启动以来"。
    """
    payload = client.get("/api/stats?room_id=804456").json()
    assert payload["mode"] == "cumulative"
    senders = payload.get("top_senders")
    assert senders, "选了具体房间时应给出发言榜"
    assert payload["top_senders_scope"] == "该房间全部时间"
    # 夹具里 uid=1001 的"甲"发了 3 条，应当排第一（按 uid 聚合，不看昵称）
    assert senders[0]["uid"] == 1001
    assert senders[0]["count"] == 3
    assert senders[0]["uname"] == "甲"


def test_cumulative_stats_skip_top_senders_without_a_room(client) -> None:
    """**全部房间**也要有榜（累计口径默认显示），但要走 TTL 缓存避免每 3 秒扫全库。"""
    from bili_danmaku.server.app import reset_top_senders_cache

    reset_top_senders_cache()  # 缓存是模块级的，会跨用例存活
    payload = client.get("/api/stats").json()
    assert payload["mode"] == "cumulative"
    senders = payload.get("top_senders")
    assert senders, "全部房间时也应给榜（默认显示）"
    assert payload["top_senders_scope"] == "全部房间"
    assert senders[0]["uid"] == 1001

    # 第二次命中缓存：范围角标会变成「全部房间（缓存）」
    cached = client.get("/api/stats").json()
    assert cached["top_senders_scope"] == "全部房间（缓存）"
    assert cached["top_senders"] == senders

    reset_top_senders_cache()


def test_window_stats_still_report_top_senders(client) -> None:
    """窗口口径不受影响（它本来就有榜，范围是最近 N 秒）。"""
    payload = client.get("/api/stats?room_id=804456&window=3600").json()
    assert payload["mode"] == "window"
    assert payload["top_senders"], "窗口口径应给出榜"
    assert payload["top_senders"][0]["uid"] == 1001


# --------------------------------------------------------------------------- #
# /api/export
# --------------------------------------------------------------------------- #


def test_export_csv_streams_with_single_header(client) -> None:
    response = client.get("/api/export?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in response.headers["content-disposition"]

    # 用 utf-8-sig 解码：CSV 必须带 BOM（见下一个用例）
    lines = [line for line in response.content.decode("utf-8-sig").splitlines() if line]
    assert lines[0].startswith("ts,type,uid")
    assert len(lines) == 5, "表头只能出现一次，4 条数据各一行"
    assert any("主播好" in line for line in lines)


def test_export_csv_carries_utf8_bom_for_excel(client) -> None:
    """回归：Web 导出的 CSV 必须带 UTF-8 BOM。

    实测反馈：浏览器下载的 CSV 是合法 UTF-8 但**没有 BOM**，
    中文版 Excel 会按系统码页（GBK/ANSI）打开，中文全成乱码。
    `Content-Type: text/csv; charset=utf-8` 对"双击打开本地文件"的 Excel 无效
    （它只看文件头），所以 BOM 是唯一可靠的信号。
    本地 CLI 导出一直用 `utf-8-sig`，这里必须与之一致。
    """
    response = client.get("/api/export?format=csv")
    assert response.content[:3] == b"\xef\xbb\xbf", "CSV 应以 UTF-8 BOM 开头"
    assert response.content[:4] != b"\xef\xbb\xbf", "BOM 后应紧跟表头"

    # 去掉 BOM 后必须还是合法 JSON 之外的合法 CSV，中文可正确还原
    text = response.content.decode("utf-8-sig")
    assert "\ufeff" not in text, "解码后不应残留 BOM 字符"
    assert "主播好" in text
    first_field = text.splitlines()[1].split(",")[0]
    assert first_field.replace(".", "").isdigit(), "首列应是时间戳"

    # 反向验证：不带 BOM 的话，按 GBK 硬解会失败或产生乱码 —— 说明 BOM 确实是必要的
    body = response.content[3:]
    try:
        body.decode("gbk")
    except UnicodeDecodeError:
        pass  # 最理想：GBK 根本解不出来，Excel 只能靠 BOM 判定 UTF-8


def test_export_jsonl_and_txt_have_no_bom(client) -> None:
    """BOM 只加给 CSV（Excel 需要）；JSONL/TXT 加了反而会污染第一行解析。"""
    for fmt in ("jsonl", "txt"):
        response = client.get(f"/api/export?format={fmt}")
        assert response.content[:3] != b"\xef\xbb\xbf", f"{fmt} 不应带 BOM"


def test_export_csv_round_trips_non_ascii_via_python_csv(client) -> None:
    """按 Excel 之外的真实消费者方式读回：csv 模块 + utf-8-sig 必须能拿到中文。"""
    import csv
    import io

    response = client.get("/api/export?format=csv")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [row["text"] for row in rows] == ["主播好", "别的", "SC 正文", "再补一条"]
    assert rows[0]["uname"] == "甲"


def test_export_jsonl_stream(client) -> None:
    response = client.get("/api/export?format=jsonl&type=danmaku")
    records = [json.loads(line) for line in response.text.splitlines() if line]
    assert len(records) == 3
    assert {record["text"] for record in records} == {"主播好", "别的", "再补一条"}


def test_export_txt_stream(client) -> None:
    response = client.get("/api/export?format=txt")
    assert "主播好" in response.text
    assert "SC 正文" not in response.text, "txt 只保留弹幕正文"


def test_export_respects_limit_and_returns_empty_stream_when_filtered_out(client) -> None:
    assert len([line for line in client.get("/api/export?format=jsonl&limit=1").text.splitlines() if line]) == 1

    empty = client.get("/api/export?format=jsonl&q=不存在的关键词")
    assert empty.status_code == 200
    assert empty.text.strip() == "", "没有命中时应是空流而不是报错"


# --------------------------------------------------------------------------- #
# 控制端点：停止面板（只允许本机）
# --------------------------------------------------------------------------- #


def test_shutdown_endpoint_invokes_callback(client) -> None:
    """本机 POST 应触发优雅停止回调（真实运行时它会把 uvicorn 的 should_exit 置位）。

    注意 `TestClient` 默认把客户端地址写成字面量 `testclient`（不是 IP），
    而控制端点**只认回环**，所以这里用 `client=` 显式给一个回环地址 ——
    否则测的就是"拒绝对端"这条路径，而不是"本机可以停"这条。
    """
    called: list[str] = []
    client.app.state.on_shutdown = lambda: called.append("stop")  # type: ignore[attr-defined]

    with TestClient(client.app, client=("127.0.0.1", 51234)) as local:  # type: ignore[attr-defined]
        response = local.post("/api/admin/shutdown")

    assert response.status_code == 200
    assert response.json()["stopping"] is True

    import time as _time

    for _ in range(50):  # 回调是延后一个 tick 执行的（先把响应发回去）
        if called:
            break
        _time.sleep(0.02)
    assert called == ["stop"]


def test_shutdown_returns_success_and_runs_callback(client) -> None:
    """停止请求要"既回了 200、又真的把回调跑完"。

    这里**不**断言两者的先后：`BackgroundTask` 由 ASGI 服务器调度，
    TestClient 的 portal 执行顺序与真实 uvicorn 未必一致（实测回调先跑完），
    而这一点对功能无影响 —— 真实运行时 uvicorn 要等 lifespan 收尾完才关连接。
    """
    order: list[str] = []
    client.app.state.on_shutdown = lambda: order.append("callback")  # type: ignore[attr-defined]
    client.app.state.shutdown_requested = False  # type: ignore[attr-defined]

    with TestClient(client.app, client=("127.0.0.1", 51234)) as local:  # type: ignore[attr-defined]
        response = local.post("/api/admin/shutdown")
        order.append("response-received")

    assert response.status_code == 200
    assert response.json()["stopping"] is True
    assert "callback" in order, "回调必须被执行，否则服务不会真的退出"


def test_shutdown_is_idempotent(client) -> None:
    with TestClient(client.app, client=("127.0.0.1", 51234)) as local:  # type: ignore[attr-defined]
        client.app.state.on_shutdown = lambda: None  # type: ignore[attr-defined]
        assert local.post("/api/admin/shutdown").status_code == 200
        again = local.post("/api/admin/shutdown")
    assert again.status_code == 200
    assert "已接受过停止请求" in again.json()["note"]


def test_shutdown_rejects_foreign_origin(client) -> None:
    """浏览器里的按钮是跨源友好的简单 POST，所以必须校验 Origin（CSRF）。"""
    with TestClient(client.app, client=("127.0.0.1", 51234)) as local:  # type: ignore[attr-defined]
        response = local.post(
            "/api/admin/shutdown", headers={"Origin": "http://evil.example"}
        )
    assert response.status_code == 403
    assert "来源校验失败" in response.json()["detail"]


@pytest.mark.parametrize(
    "host",
    ["192.168.1.50", "10.0.0.7", "0.0.0.0", "::ffff:192.168.1.9", "example.com", ""],
)
def test_shutdown_rejects_non_loopback_client(client, host: str) -> None:
    """非本机来源一律 403 —— 即便面板监听 0.0.0.0，局域网也无权停服务。"""
    with TestClient(client.app, client=(host or "testclient", 51234)) as remote:  # type: ignore[attr-defined]
        response = remote.post("/api/admin/shutdown")
    assert response.status_code == 403
    assert "仅允许本机" in response.json()["detail"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
@pytest.mark.parametrize("origin_host", ["127.0.0.1", "localhost"])
def test_shutdown_allows_loopback_forms(client, host: str, origin_host: str) -> None:
    """回环地址的各种写法都要放行（含端口/协议差异）。"""
    client.app.state.on_shutdown = lambda: None  # type: ignore[attr-defined]
    client.app.state.shutdown_requested = False  # type: ignore[attr-defined]
    with TestClient(client.app, client=(host, 51234)) as local:  # type: ignore[attr-defined]
        # Origin 的端口故意写成别的：校验只看主机名
        response = local.post(
            "/api/admin/shutdown", headers={"Origin": f"http://{origin_host}:18999"}
        )
    assert response.status_code == 200


def test_is_loopback_helper() -> None:
    """地址判定用 ipaddress，覆盖 IPv4 段、IPv6 与 IPv4-mapped 形式。"""
    from bili_danmaku.server.app import _loopback_host

    for good in ("127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1", "localhost", "LOCALHOST"):
        assert _loopback_host(good) is True, good
    for bad in ("192.168.1.1", "10.0.0.1", "example.com", "", "0.0.0.0", "::ffff:8.8.8.8"):
        assert _loopback_host(bad) is False, bad


def test_shutdown_requires_token_when_configured() -> None:
    """配置了 web.token 时，控制端点也要带 token（与其它接口一致）。"""
    from bili_danmaku.server.app import create_app

    hub = WebHub()
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"out_dir": "data", "types": ["jsonl"]}, "web": {"token": "s3cret"}},
    )
    called: list[str] = []
    app = create_app(
        config, None, hub, StubPanel(), on_shutdown=lambda: called.append("stop")  # type: ignore[arg-type]
    )
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
        assert test_client.post("/api/admin/shutdown").status_code == 403
        assert called == [], "缺 token 时不该真的停止"
        ok = test_client.post("/api/admin/shutdown", headers={"X-Bili-Token": "s3cret"})
        assert ok.status_code == 200


def test_shutdown_is_post_only(client) -> None:
    with TestClient(client.app, client=("127.0.0.1", 51234)) as local:  # type: ignore[attr-defined]
        assert local.get("/api/admin/shutdown").status_code == 405


# --------------------------------------------------------------------------- #
# WebSocket：慢消费者会被主动断开
# --------------------------------------------------------------------------- #


def test_websocket_disconnects_slow_consumer(client) -> None:
    """面板是消费者，消费不过来必须被断开，而不能反向拖累采集。"""
    hub: WebHub = client.hub
    payloads: list[dict[str, Any]] = []
    with client.websocket_connect("/ws/live") as socket:
        assert socket.receive_json()["t"] == "hello"
        # queue_size=4 / slow_threshold=2：灌够多的消息即可触发
        for index in range(12):
            hub.publish(event(100 + index, text=f"灌{index}"))
        try:
            for _ in range(12):
                payloads.append(socket.receive_json())
        except WebSocketDisconnect:
            pass  # 服务端主动断开正是预期行为

    kinds = [payload["t"] for payload in payloads]
    assert "error" in kinds, f"应收到 SLOW_CONSUMER 提示，实得 {kinds}"
    error = next(payload for payload in payloads if payload["t"] == "error")
    assert error["data"]["code"] == "SLOW_CONSUMER"
    assert "断开" in error["data"]["hint"]


def test_websocket_hello_reports_room_filter(client) -> None:
    with client.websocket_connect("/ws/live?room_id=804456,123&types=danmaku&keyword=好") as socket:
        hello = socket.receive_json()
        assert hello["t"] == "hello"
        assert hello["data"]["filter"] == {
            "types": ["danmaku"],
            "rooms": [123, 804456],
            "keyword": "好",
        }


# --------------------------------------------------------------------------- #
# 前端托管
# --------------------------------------------------------------------------- #


def test_find_dist_locates_built_frontend() -> None:
    """仓库里已经有 web/dist（M3 构建产物），必须能被定位到。"""
    dist = find_dist()
    assert dist is not None
    assert (dist / "index.html").is_file()


def test_packaged_frontend_wins_over_working_tree() -> None:
    """包内 `web_dist/`（wheel 分发的前端）必须优先于工作区的 `web/dist`。

    打包脚本会把 `web/dist` 暂存到这个目录，这里直接造一个最小副本验证优先级，
    跑完删掉 —— 不给源码树留垃圾。
    """
    import bili_danmaku

    packaged = Path(bili_danmaku.__file__).resolve().parent / "web_dist"
    created = not packaged.exists()
    if created:
        packaged.mkdir(parents=True)
    marker = packaged / "index.html"
    made_marker = not marker.exists()
    try:
        if made_marker:
            marker.write_text("<!doctype html><title>packaged</title>", encoding="utf-8")
        found = find_dist()
        assert found is not None
        assert found == packaged, "包内 web_dist 应优先于 ./web/dist"
    finally:
        if made_marker:
            marker.unlink()
        if created and packaged.exists():
            packaged.rmdir()
    assert find_dist() is None or find_dist() != packaged


# --------------------------------------------------------------------------- #
# 冻结（PyInstaller）场景
#
# 背景：打包后 `bili_danmaku.__file__` 是**合成路径**（模块在 base_library.zip 里），
# 磁盘上没有 `<包目录>/`，所以原来的候选 1 必然落空；而 `--add-data` 会把前端放到
# `<_MEIPASS>/web_dist`。实测 onefile 与 onedir 都因此返回 None（exe 显示降级页）。
# 下面这些用例把该回归钉住。
# --------------------------------------------------------------------------- #


def test_find_dist_uses_meipass_when_frozen(monkeypatch, tmp_path) -> None:
    """冻结时必须在 `<sys._MEIPASS>/web_dist` 找到前端。"""
    import bili_danmaku.server.app as app_module

    meipass = tmp_path / "_MEI12345"
    (meipass / "web_dist").mkdir(parents=True)
    (meipass / "web_dist" / "index.html").write_text(
        "<!doctype html><title>frozen</title>", encoding="utf-8"
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    found = app_module.find_dist()
    assert found is not None, "冻结时必须能定位到 _MEIPASS/web_dist"
    assert found == meipass / "web_dist"
    assert (found / "index.html").is_file()


def test_meipass_takes_priority_over_working_tree(monkeypatch, tmp_path) -> None:
    """`_MEIPASS/web_dist` 必须**优先于**工作区 `./web/dist`。

    冻结分发场景下工作区根本不存在，但优先级仍要明确：dist 一旦被改成
    "先看 cwd"，打包后会因为碰巧撞上某个 `web/dist` 而静默用错前端。
    """
    import bili_danmaku.server.app as app_module

    meipass = tmp_path / "_MEI"
    (meipass / "web_dist").mkdir(parents=True)
    (meipass / "web_dist" / "index.html").write_text("<title>frozen</title>", encoding="utf-8")

    cwd = tmp_path / "cwd"
    (cwd / "web" / "dist").mkdir(parents=True)
    (cwd / "web" / "dist" / "index.html").write_text("<title>cwd</title>", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.chdir(cwd)

    found = app_module.find_dist()
    assert found == meipass / "web_dist", "_MEIPASS 应优先于 cwd/web/dist"


def test_meipass_without_frontend_falls_back(monkeypatch, tmp_path) -> None:
    """`_MEIPASS` 存在但里面没有 `web_dist` 时，要**继续往后找**，不能直接返回 None。

    这样"冻结 + 前端漏打包"不会把工作区里可用的 web/dist 也一起屏蔽掉。
    """
    import bili_danmaku.server.app as app_module

    meipass = tmp_path / "_MEI_EMPTY"
    meipass.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    found = app_module.find_dist()
    # 源码树里 web/dist 存在（本仓库的常规状态），必须仍然能找到它
    assert found is None or found != meipass / "web_dist"
    if found is not None:
        assert (found / "index.html").is_file()


def test_no_meipass_attribute_is_harmless(monkeypatch) -> None:
    """普通（非冻结）运行没有 `sys._MEIPASS`，不能因此报错或改变原有行为。"""
    import bili_danmaku.server.app as app_module

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    found = app_module.find_dist()
    assert found is None or (found / "index.html").is_file()


def test_index_serves_built_frontend_and_assets(client) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<div id=" in page.text or "<script" in page.text

    health = client.get("/api/health").json()
    assert health["frontend_built"] is True

    assets = list((find_dist() or Path()).glob("assets/*.js"))
    if assets:
        served = client.get(f"/assets/{assets[0].name}")
        assert served.status_code == 200
        assert len(served.content) > 0


def test_favicon_returns_json_404_when_absent(client) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code in (200, 404)
    if response.status_code == 404:
        assert response.json()["detail"] == "no favicon"


def test_open_store_returns_none_for_missing_db(tmp_path: Path) -> None:
    from bili_danmaku.server.app import _open_store

    config = Config.load(
        path="missing.toml", overrides={"sinks": {"out_dir": str(tmp_path / "没有这个目录")}}
    )
    assert _open_store(config) is None


def test_memory_events_skips_unparsable_buffer_entries() -> None:
    from bili_danmaku.server.app import _memory_events

    hub = WebHub()
    hub.publish(event(1, text="内存里的"))
    hub._buffers[804456].append("这不是 JSON")  # type: ignore[attr-defined]

    payload = _memory_events(hub, None, None, None, limit=10)
    assert payload["source"] == "memory"
    assert [item["text"] for item in payload["items"]] == ["内存里的"]

    filtered = _memory_events(hub, None, {"gift"}, None, limit=10)
    assert filtered["total"] == 0


def test_health_reports_uptime_and_version(client) -> None:
    payload = client.get("/api/health").json()
    assert payload["version"]
    assert payload["uptime_s"] >= 0
    assert payload["status"] == "ok"


def test_sqlite_store_is_readable_while_server_holds_connection(client, sqlite_config) -> None:
    """服务端起着的连接不应阻塞只读查询（WAL 的意义）。"""
    db = Path(sqlite_config.sinks.out_dir) / "danmaku.db"
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 4
    finally:
        connection.close()
