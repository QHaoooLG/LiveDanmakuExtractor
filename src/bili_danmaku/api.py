"""B 站 HTTP 接口封装。

关键设计（基于对房间 804456 的真实实测，见 TECH_DESIGN §1）：
    * `Danmu/getConf` 匿名可用，是保底路径（实测返回 token + 节点并成功握手）。
    * `getDanmuInfo` 匿名一律返回 -352 风控，因此仅在登录态下尝试，
      失败即降级且**不重试**（重试会加重风控）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Config
from .credentials import Credentials
from .models import HostInfo, RoomInfo, TokenBundle, TokenSource
from .utils import RateLimiter
from .wbi import WbiKeyCache, build_signed_query, parse_wbi_keys

log = logging.getLogger("bili_danmaku.api")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}

API_LIVE = "https://api.live.bilibili.com"
API_MAIN = "https://api.bilibili.com"
API_PASSPORT = "https://passport.bilibili.com"

#: 风控错误码：出现后本进程内不再对该房间尝试 getDanmuInfo
CODE_RISK_CONTROL = -352
CODE_NOT_LOGGED_IN = -101


@dataclass
class ApiResult:
    code: int
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    http_status: int = 0

    @property
    def ok(self) -> bool:
        return self.code == 0


class BiliApiError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _dedupe_hosts(raw_hosts: list[dict[str, Any]]) -> list[HostInfo]:
    """去重节点。实测 getConf 会返回 6 条完全重复的记录。"""
    seen: set[tuple[str, int | None]] = set()
    hosts: list[HostInfo] = []
    for item in raw_hosts:
        host = item.get("host")
        if not host:
            continue
        wss_port = item.get("wss_port")
        key = (str(host), wss_port if wss_port is None else int(wss_port))
        if key in seen:
            continue
        seen.add(key)
        hosts.append(
            HostInfo(
                host=str(host),
                wss_port=int(wss_port) if wss_port else None,
                ws_port=int(item["ws_port"]) if item.get("ws_port") else None,
                port=int(item["port"]) if item.get("port") else None,
            )
        )
    return hosts


class BiliApi:
    """异步接口客户端。请用 `async with` 使用。"""

    def __init__(self, config: Config, credentials: Credentials) -> None:
        self._config = config
        self._creds = credentials
        self._client: httpx.AsyncClient | None = None
        self._limiter = RateLimiter(config.api.min_request_interval)
        self._wbi_keys = WbiKeyCache()
        self._risk_blocked_rooms: set[int] = set()

    async def __aenter__(self) -> "BiliApi":
        self._client = httpx.AsyncClient(
            headers=BASE_HEADERS,
            timeout=self._config.api.request_timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        for name, value in self._creds.as_cookies().items():
            self._client.cookies.set(name, value, domain=".bilibili.com")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("BiliApi 必须在 async with 上下文中使用")
        return self._client

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        use_wbi: bool = False,
        retry: int | None = None,
    ) -> ApiResult:
        params = dict(params or {})
        if use_wbi:
            keys = await self._ensure_wbi_keys()
            if keys is not None:
                img_key, sub_key = keys
                signed = build_signed_query(params, img_key, sub_key)
                params = {}
                for pair in signed.split("&"):
                    key, _, value = pair.partition("=")
                    params[key] = value
        else:
            # 保持确定的参数顺序，便于日志比对
            params = {k: params[k] for k in sorted(params)}

        attempts = self._config.api.retry_max if retry is None else retry
        last_error: Exception | None = None

        for attempt in range(max(attempts, 1)):
            await self._limiter.acquire()
            try:
                response = await self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("请求失败(%d/%d) %s: %s", attempt + 1, attempts, url, exc)
                continue

            if response.status_code >= 500:
                last_error = BiliApiError(f"HTTP {response.status_code}", response.status_code)
                log.warning("服务端错误(%d/%d) %s", attempt + 1, attempts, response.status_code)
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                raise BiliApiError(f"响应不是合法 JSON: {url}") from exc

            return ApiResult(
                code=int(payload.get("code") or 0),
                message=str(payload.get("message") or ""),
                data=payload.get("data") or {},
                http_status=response.status_code,
            )

        raise BiliApiError(f"请求重试耗尽: {url} ({last_error})")

    async def _ensure_wbi_keys(self) -> tuple[str, str] | None:
        if self._wbi_keys.is_fresh():
            return self._wbi_keys.get()
        try:
            result = await self._get(f"{API_MAIN}/x/web-interface/nav", retry=1)
            keys = parse_wbi_keys({"data": result.data})
            self._wbi_keys.set(*keys)
            return keys
        except Exception as exc:  # noqa: BLE001
            log.warning("获取 WBI 密钥失败: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # 业务接口
    # ------------------------------------------------------------------ #

    async def room_init(self, room: int | str) -> RoomInfo:
        result = await self._get(f"{API_LIVE}/room/v1/Room/room_init", {"id": room})
        if not result.ok:
            raise BiliApiError(
                f"room_init 失败 code={result.code} message={result.message}", result.code
            )
        data = result.data
        room_id = int(data.get("room_id") or 0)
        return RoomInfo(
            room_id=room_id,
            short_id=int(data["short_id"]) if data.get("short_id") else None,
            anchor_uid=int(data["uid"]) if data.get("uid") else None,
            live_status=int(data.get("live_status") or 0),
            live_time=int(data["live_time"]) if data.get("live_time") else None,
        )

    async def anchor_info(self, room_id: int) -> dict[str, Any] | None:
        """房间主播的基础信息（昵称/头像/uid）。

        实测（2026-01）：`xlive/web-room/v1/index/getInfoByRoom` 匿名一律 -352 风控，
        而 `live_user/v1/UserInfo/get_anchor_in_room` **匿名可用**且直接给出昵称 ——
        面板右侧栏要显示的「XXX 的直播间」就靠它。

        失败时返回 None 而不是抛异常：主播昵称只是展示信息，
        取不到不该让「添加房间」这条主链路失败。
        """
        try:
            result = await self._get(
                f"{API_LIVE}/live_user/v1/UserInfo/get_anchor_in_room",
                {"roomid": room_id},
                retry=1,  # 风控场景不重试，加重风控得不偿失
            )
        except BiliApiError as exc:
            log.warning("获取主播信息失败（room_id=%s）: %s", room_id, exc)
            return None
        if not result.ok:
            log.warning(
                "获取主播信息失败（room_id=%s）code=%s message=%s",
                room_id,
                result.code,
                result.message,
            )
            return None
        info = result.data.get("info") or {}
        return info if isinstance(info, dict) else None

    async def room_info(self, room: int | str, *, with_anchor: bool = True) -> RoomInfo:
        """房间号 → 带主播昵称的房间信息（面板用）。

        短号与真实房间号都由 `room_init` 归一化；主播昵称取不到时为 None，
        调用方（面板）会退化成只显示房间号。
        """
        info = await self.room_init(room)
        if not with_anchor:
            return info
        anchor = await self.anchor_info(info.room_id)
        if anchor:
            return info.model_copy(
                update={
                    "anchor_name": anchor.get("uname") or None,
                    "anchor_face": anchor.get("face") or None,
                }
            )
        return info

    async def danmu_conf(self, room_id: int) -> TokenBundle:
        """保底路径：实测匿名可用。"""
        result = await self._get(
            f"{API_LIVE}/room/v1/Danmu/getConf",
            {"room_id": room_id, "platform": "pc", "player": "web"},
        )
        if not result.ok:
            raise BiliApiError(
                f"getConf 失败 code={result.code} message={result.message}", result.code
            )
        data = result.data
        hosts = _dedupe_hosts(data.get("host_server_list") or data.get("host_list") or [])
        token = str(data.get("token") or "")
        if not token or not hosts:
            raise BiliApiError("getConf 未返回有效 token 或节点列表")
        return TokenBundle(token=token, hosts=hosts, source=TokenSource.GET_CONF.value)

    async def danmu_info(self, room_id: int, *, use_wbi: bool = False) -> ApiResult:
        """登录态路径。匿名实测返回 -352。"""
        return await self._get(
            f"{API_LIVE}/xlive/web-room/v1/index/getDanmuInfo",
            {"id": room_id, "type": 0},
            use_wbi=use_wbi,
            retry=1,  # 风控场景不重试
        )

    async def acquire_token(self, room_id: int, *, force_getconf: bool = False) -> TokenBundle:
        """按实测结论选择 token 来源（V15）。

        实测：匿名一律 -352；**登录态下必须叠加 WBI 签名**才能成功。
        因此这里直接发签名请求，省掉一次注定失败的往返。
        失败则降级 getConf（匿名可用，是保底路径）。

        注意：只有当 getDanmuInfo **确实失败**时才把房间加入本进程黑名单，
        否则一次成功之后的重连会被错误地永久降级到 getConf。
        """
        cfg = self._config.api
        try_getdanmuinfo = (
            cfg.prefer_getdanmuinfo
            and self._creds.logged_in
            and room_id not in self._risk_blocked_rooms
            and not force_getconf
        )

        if try_getdanmuinfo:
            result = await self.danmu_info(room_id, use_wbi=True)
            if result.ok:
                try:
                    bundle = self._bundle_from_info(
                        result, TokenSource.GET_DANMU_INFO_WBI.value
                    )
                except BiliApiError as exc:
                    # code=0 但缺 token/节点：降级比抛异常更适合无人值守的采集
                    log.warning("getDanmuInfo 响应不完整（%s），降级 getConf", exc)
                else:
                    log.info("token 来源: getDanmuInfo + WBI 签名")
                    return bundle
            elif result.code == CODE_RISK_CONTROL:
                self._risk_blocked_rooms.add(room_id)
                log.warning("getDanmuInfo 触发风控(code=-352)，本进程内该房间改走 getConf")
            else:
                log.warning(
                    "getDanmuInfo 失败 code=%s message=%s，降级 getConf",
                    result.code,
                    result.message,
                )

        bundle = await self.danmu_conf(room_id)
        log.info("token 来源: getConf")
        return bundle

    def _bundle_from_info(self, result: ApiResult, source: str) -> TokenBundle:
        data = result.data
        hosts = _dedupe_hosts(data.get("host_list") or [])
        token = str(data.get("token") or "")
        if not token or not hosts:
            raise BiliApiError("getDanmuInfo 未返回有效 token 或节点列表")
        return TokenBundle(
            token=token,
            hosts=hosts,
            source=source,
            max_delay=int(data["max_delay"]) if data.get("max_delay") else None,
            refresh_rate=int(data["refresh_rate"]) if data.get("refresh_rate") else None,
        )

    async def fingerprint(self) -> tuple[str | None, str | None]:
        """获取设备指纹 buvid3 / buvid4。"""
        result = await self._get(f"{API_MAIN}/x/frontend/finger/spi", retry=1)
        if not result.ok:
            return None, None
        return result.data.get("b_3"), result.data.get("b_4")

    async def history(self, room_id: int) -> list[dict[str, Any]]:
        """最近历史弹幕（用于启动补种 / 离线夹具）。"""
        try:
            result = await self._get(
                f"{API_LIVE}/xlive/web-room/v1/dM/gethistory",
                {"roomid": room_id, "room_type": 0},
            )
        except BiliApiError as exc:
            log.warning("gethistory 失败: %s", exc)
            return []
        if not result.ok:
            return []
        return list(result.data.get("room") or [])

    async def nav(self) -> ApiResult:
        """登录态检查依据。code=0 且 isLogin=true 表示凭据有效。"""
        return await self._get(f"{API_MAIN}/x/web-interface/nav", retry=1)
