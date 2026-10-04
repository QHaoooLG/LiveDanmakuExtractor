"""M4 覆盖率：HTTP 接口层（`api.py`）。

此前 `test_api.py` 只锁死 token **选择策略**，`_get` / `danmu_conf` / `room_init` /
`history` / WBI 签名这些真正碰网络的代码是空白的（52%）。这里用
`httpx.MockTransport` 把 HTTP 层整个换掉：**不联网**，但走的是真实请求链路。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from bili_danmaku.api import ApiResult, BiliApi, BiliApiError, _dedupe_hosts
from bili_danmaku.config import Config
from bili_danmaku.credentials import Credentials
from bili_danmaku.models import TokenSource

ANONYMOUS = Credentials()


def fast_config(make_config, **api) -> Config:
    settings = {"min_request_interval": 0.0, "retry_max": 1, "request_timeout": 2.0}
    settings.update(api)
    return make_config(api=settings)


class Recorder:
    """记录每个请求的 URL 与参数，并可按路径给出响应。"""

    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, list[str]]]] = []
        self.headers: list[httpx.Headers] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.path, parse_qs(urlparse(str(request.url)).query)))
        self.headers.append(request.headers)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"code": -404, "message": "not found"})
        status, body = route
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]


async def make_api(config: Config, recorder: Recorder, credentials: Credentials = ANONYMOUS):
    api = BiliApi(config, credentials)
    await api.__aenter__()
    api._client = httpx.AsyncClient(  # 只在测试里替换 transport，其余链路保持真实
        transport=httpx.MockTransport(recorder.handler),
        headers={"User-Agent": "test"},
        timeout=config.api.request_timeout,
    )
    return api


# --------------------------------------------------------------------------- #
# _get：重试、限流、异常
# --------------------------------------------------------------------------- #


async def test_get_retries_5xx_then_succeeds(make_config) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="boom")
        return httpx.Response(200, json={"code": 0, "data": {"ok": 1}})

    api = BiliApi(fast_config(make_config, retry_max=2), ANONYMOUS)
    await api.__aenter__()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await api._get("https://api.example.com/x", {"a": 1})
    finally:
        await api.__aexit__(None, None, None)

    assert result.ok and result.data == {"ok": 1}
    assert calls["n"] == 2, "5xx 应重试"


async def test_get_gives_up_after_retries(make_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="always broken")

    api = BiliApi(fast_config(make_config, retry_max=2), ANONYMOUS)
    await api.__aenter__()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BiliApiError) as excinfo:
            await api._get("https://api.example.com/x")
    finally:
        await api.__aexit__(None, None, None)
    assert "重试耗尽" in str(excinfo.value)


async def test_get_retries_transport_error(make_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝", request=request)

    api = BiliApi(fast_config(make_config, retry_max=2), ANONYMOUS)
    await api.__aenter__()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BiliApiError):
            await api._get("https://api.example.com/x")
    finally:
        await api.__aexit__(None, None, None)


async def test_get_rejects_non_json_body(make_config) -> None:
    recorder = Recorder({"/x": (200, "<html>waf</html>")})
    api = await make_api(fast_config(make_config), recorder)
    try:
        with pytest.raises(BiliApiError) as excinfo:
            await api._get("https://api.example.com/x")
        assert "不是合法 JSON" in str(excinfo.value)
    finally:
        await api.__aexit__(None, None, None)


async def test_get_sorts_params_without_wbi(make_config) -> None:
    """不签名时保持参数有序，便于对比日志。"""
    recorder = Recorder({"/room/v1/Room/room_init": (200, {"code": 0, "data": {"room_id": 1}})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        await api._get("https://api.live.bilibili.com/room/v1/Room/room_init", {"id": 2, "a": 1})
    finally:
        await api.__aexit__(None, None, None)
    assert recorder.paths == ["/room/v1/Room/room_init"]
    assert list(recorder.requests[0][1]) == ["a", "id"], "参数顺序应稳定"


# --------------------------------------------------------------------------- #
# WBI 签名
# --------------------------------------------------------------------------- #


async def test_wbi_signing_adds_rid_and_wts(make_config) -> None:
    nav = {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            }
        },
    }
    recorder = Recorder(
        {
            "/x/web-interface/nav": (200, nav),
            "/xlive/web-room/v1/index/getDanmuInfo": (
                200,
                {"code": 0, "data": {"token": "t", "host_list": [{"host": "h", "wss_port": 2245}]}},
            ),
        }
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        result = await api.danmu_info(804456, use_wbi=True)
    finally:
        await api.__aexit__(None, None, None)

    assert result.ok
    assert recorder.paths == [
        "/x/web-interface/nav",
        "/xlive/web-room/v1/index/getDanmuInfo",
    ], "先取密钥再发签名请求"
    signed = recorder.requests[1][1]
    assert signed["w_rid"] and signed["wts"] and signed["id"] == ["804456"]
    # 第二次请求应命中密钥缓存，不再打 nav
    recorder.requests.clear()
    api2 = await make_api(fast_config(make_config), recorder)
    api2._wbi_keys = api._wbi_keys
    try:
        await api2.danmu_info(804456, use_wbi=True)
    finally:
        await api2.__aexit__(None, None, None)
    assert recorder.paths == ["/xlive/web-room/v1/index/getDanmuInfo"], "密钥应被缓存"


async def test_wbi_key_fetch_failure_falls_back_to_unsigned(make_config) -> None:
    recorder = Recorder(
        {
            "/x/web-interface/nav": (200, {"code": -101, "message": "未登录"}),
            "/x": (200, {"code": 0, "data": {}}),
        }
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        result = await api._get("https://api.example.com/x", {"a": 1}, use_wbi=True)
    finally:
        await api.__aexit__(None, None, None)

    assert result.ok
    assert "w_rid" not in recorder.requests[-1][1], "拿不到密钥时应退回未签名请求"


# --------------------------------------------------------------------------- #
# 业务接口
# --------------------------------------------------------------------------- #


async def test_room_init_parses_real_fields(make_config) -> None:
    payload = {
        "code": 0,
        "data": {
            "room_id": 804456,
            "short_id": 456,
            "uid": 8431064,
            "live_status": 1,
            "live_time": 1_789_000_000,
        },
    }
    recorder = Recorder({"/room/v1/Room/room_init": (200, payload)})
    api = await make_api(fast_config(make_config), recorder)
    try:
        room = await api.room_init("804456")
    finally:
        await api.__aexit__(None, None, None)

    assert room.room_id == 804456
    assert room.short_id == 456
    assert room.anchor_uid == 8431064
    assert room.is_living


async def test_room_init_raises_on_error_code(make_config) -> None:
    recorder = Recorder({"/room/v1/Room/room_init": (200, {"code": -400, "message": "房间不存在"})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        with pytest.raises(BiliApiError) as excinfo:
            await api.room_init(999999)
    finally:
        await api.__aexit__(None, None, None)
    assert excinfo.value.code == -400


async def test_room_init_tolerates_missing_optional_fields(make_config) -> None:
    recorder = Recorder({"/room/v1/Room/room_init": (200, {"code": 0, "data": {"room_id": 7}})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        room = await api.room_init(7)
    finally:
        await api.__aexit__(None, None, None)
    assert (room.room_id, room.short_id, room.anchor_uid, room.live_status) == (7, None, None, 0)


async def test_anchor_info_reads_uname_from_anchor_in_room(make_config) -> None:
    """主播昵称走 `get_anchor_in_room`（实测匿名可用），不是被风控的 getInfoByRoom。"""
    recorder = Recorder(
        {
            "/live_user/v1/UserInfo/get_anchor_in_room": (
                200,
                {"code": 0, "data": {"info": {"uid": 8431064, "uname": "六式zy", "face": "http://f/x.jpg"}}},
            )
        }
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        info = await api.anchor_info(804456)
    finally:
        await api.__aexit__(None, None, None)

    assert info is not None and info["uname"] == "六式zy"
    assert recorder.paths == ["/live_user/v1/UserInfo/get_anchor_in_room"]
    assert recorder.requests[0][1]["roomid"] == ["804456"]


async def test_anchor_info_returns_none_instead_of_raising(make_config) -> None:
    """昵称只是展示信息：接口挂掉也必须返回 None，不能让「添加房间」跟着失败。"""
    recorder = Recorder(
        {"/live_user/v1/UserInfo/get_anchor_in_room": (200, {"code": -352, "message": "风控"})}
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        assert await api.anchor_info(804456) is None
    finally:
        await api.__aexit__(None, None, None)


async def test_room_info_merges_anchor_name(make_config) -> None:
    recorder = Recorder(
        {
            "/room/v1/Room/room_init": (
                200,
                {"code": 0, "data": {"room_id": 804456, "short_id": 456, "uid": 8431064, "live_status": 1}},
            ),
            "/live_user/v1/UserInfo/get_anchor_in_room": (
                200,
                {"code": 0, "data": {"info": {"uname": "六式zy", "face": "http://f/x.jpg"}}},
            ),
        }
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        room = await api.room_info("804456")
    finally:
        await api.__aexit__(None, None, None)

    assert (room.room_id, room.short_id, room.anchor_name) == (804456, 456, "六式zy")
    assert room.anchor_face == "http://f/x.jpg"


async def test_room_info_skips_anchor_lookup_when_disabled(make_config) -> None:
    recorder = Recorder(
        {
            "/room/v1/Room/room_init": (
                200,
                {"code": 0, "data": {"room_id": 804456, "uid": 8431064, "live_status": 0}},
            )
        }
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        room = await api.room_info("804456", with_anchor=False)
    finally:
        await api.__aexit__(None, None, None)

    assert room.anchor_name is None
    assert recorder.paths == ["/room/v1/Room/room_init"]


async def test_danmu_conf_dedupes_hosts(make_config) -> None:
    """实测 getConf 会返回 6 条重复节点，必须去重。"""
    host = {"host": "conf.host", "wss_port": 443, "ws_port": 2244, "port": 2243}
    recorder = Recorder(
        {
            "/room/v1/Danmu/getConf": (
                200,
                {
                    "code": 0,
                    "data": {"token": "conf-token", "host_server_list": [host, host, dict(host)]},
                },
            )
        }
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        bundle = await api.danmu_conf(804456)
    finally:
        await api.__aexit__(None, None, None)

    assert bundle.token == "conf-token"
    assert len(bundle.hosts) == 1, "重复节点必须去重"
    assert bundle.source == TokenSource.GET_CONF.value


async def test_danmu_conf_rejects_incomplete_payload(make_config) -> None:
    recorder = Recorder(
        {"/room/v1/Danmu/getConf": (200, {"code": 0, "data": {"token": "", "host_server_list": []}})}
    )
    api = await make_api(fast_config(make_config), recorder)
    try:
        with pytest.raises(BiliApiError) as excinfo:
            await api.danmu_conf(804456)
    finally:
        await api.__aexit__(None, None, None)
    assert "未返回有效 token 或节点列表" in str(excinfo.value)


async def test_bundle_from_info_maps_extra_fields(make_config) -> None:
    info = ApiResult(
        code=0,
        data={
            "token": "tok",
            "host_list": [{"host": "a", "wss_port": 2245}, {"host": "a", "wss_port": 2245}],
            "max_delay": 5000,
            "refresh_rate": 60,
        },
    )
    api = BiliApi(fast_config(make_config), ANONYMOUS)
    bundle = api._bundle_from_info(info, TokenSource.GET_DANMU_INFO_WBI.value)

    assert bundle.source == TokenSource.GET_DANMU_INFO_WBI.value
    assert bundle.max_delay == 5000
    assert bundle.refresh_rate == 60
    assert len(bundle.hosts) == 1


async def test_fingerprint_returns_none_on_failure(make_config) -> None:
    recorder = Recorder({"/x/frontend/finger/spi": (200, {"code": -352, "message": "-352"})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        assert await api.fingerprint() == (None, None)
    finally:
        await api.__aexit__(None, None, None)


async def test_fingerprint_returns_keys(make_config) -> None:
    recorder = Recorder({"/x/frontend/finger/spi": (200, {"code": 0, "data": {"b_3": "B3", "b_4": "B4"}})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        assert await api.fingerprint() == ("B3", "B4")
    finally:
        await api.__aexit__(None, None, None)


async def test_history_returns_room_records(make_config) -> None:
    records = [{"text": "历史1"}, {"text": "历史2"}]
    recorder = Recorder({"/xlive/web-room/v1/dM/gethistory": (200, {"code": 0, "data": {"room": records}})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        assert await api.history(804456) == records
    finally:
        await api.__aexit__(None, None, None)


async def test_history_swallows_failures(make_config) -> None:
    """补种历史失败只能降级为空列表 —— 不能因此让采集起不来。"""
    recorder = Recorder({"/xlive/web-room/v1/dM/gethistory": (200, {"code": -352, "message": "-352"})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        assert await api.history(804456) == []
    finally:
        await api.__aexit__(None, None, None)


async def test_history_returns_empty_on_network_error(make_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    api = BiliApi(fast_config(make_config), ANONYMOUS)
    await api.__aenter__()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await api.history(804456) == []
    finally:
        await api.__aexit__(None, None, None)


async def test_nav_reports_login_state(make_config) -> None:
    recorder = Recorder({"/x/web-interface/nav": (200, {"code": 0, "data": {"isLogin": True}})})
    api = await make_api(fast_config(make_config), recorder)
    try:
        result = await api.nav()
    finally:
        await api.__aexit__(None, None, None)
    assert result.ok and result.data["isLogin"] is True


async def test_client_requires_context_manager(make_config) -> None:
    api = BiliApi(fast_config(make_config), ANONYMOUS)
    with pytest.raises(RuntimeError):
        _ = api.client


async def test_cookies_are_attached_from_credentials(make_config) -> None:
    """凭据里的 cookie 必须在 __aenter__ 时挂到 httpx 客户端上。"""
    api = BiliApi(fast_config(make_config), Credentials(cookies={"SESSDATA": "s" * 24}))
    async with api:
        assert api.client.cookies.get("SESSDATA") == "s" * 24


# --------------------------------------------------------------------------- #
# 节点去重辅助
# --------------------------------------------------------------------------- #


def test_dedupe_hosts_keeps_first_and_skips_invalid() -> None:
    hosts = _dedupe_hosts(
        [
            {"host": "a", "wss_port": 2245},
            {"host": "a", "wss_port": 2245},
            {"host": "a", "wss_port": 443},
            {"wss_port": 2245},  # 没有 host
            {"host": "b", "ws_port": 2244},
        ]
    )
    assert [(h.host, h.wss_port, h.ws_port) for h in hosts] == [
        ("a", 2245, None),
        ("a", 443, None),
        ("b", None, 2244),
    ]
