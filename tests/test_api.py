"""token 获取策略单测（对应 TECH_DESIGN V15 与缺陷 D6）。

实测结论：匿名访问 `getDanmuInfo` 一律 -352；登录态下**必须叠加 WBI 签名**才成功。
因此策略是「登录态直接发签名请求 → 失败降级 getConf」。
"""

from __future__ import annotations

import pytest

from bili_danmaku.api import ApiResult, BiliApi
from bili_danmaku.credentials import Credentials
from bili_danmaku.models import HostInfo, TokenBundle, TokenSource

LOGGED_IN = Credentials(cookies={"SESSDATA": "x" * 24})
ANONYMOUS = Credentials()


def _ok_info() -> ApiResult:
    return ApiResult(
        code=0,
        data={"token": "info-token", "host_list": [{"host": "info.host", "wss_port": 2245}]},
    )


class StubApi(BiliApi):
    """只覆盖两个网络方法，专注验证选择策略。"""

    def __init__(self, config, credentials, info_results):
        super().__init__(config, credentials)
        self._info_results = list(info_results)
        self.info_calls: list[bool] = []
        self.conf_calls = 0

    async def danmu_info(self, room_id: int, *, use_wbi: bool = False) -> ApiResult:  # type: ignore[override]
        self.info_calls.append(use_wbi)
        if self._info_results:
            return self._info_results.pop(0)
        return ApiResult(code=-352, message="-352")

    async def danmu_conf(self, room_id: int) -> TokenBundle:  # type: ignore[override]
        self.conf_calls += 1
        return TokenBundle(
            token="conf-token",
            hosts=[HostInfo(host="conf.host", wss_port=443)],
            source=TokenSource.GET_CONF.value,
        )


async def test_logged_in_uses_wbi_signed_request_directly(make_config) -> None:
    """登录态直接发签名请求，不做那次注定 -352 的匿名往返。"""
    api = StubApi(make_config(), LOGGED_IN, [_ok_info()])
    bundle = await api.acquire_token(804456)

    assert api.info_calls == [True], "应直接使用 WBI 签名，且只请求一次"
    assert api.conf_calls == 0
    assert bundle.token == "info-token"
    assert bundle.source == TokenSource.GET_DANMU_INFO_WBI.value


async def test_success_does_not_block_room_for_later_reconnects(make_config) -> None:
    """缺陷 D6 回归：成功之后的重连**不能**被降级到 getConf。"""
    api = StubApi(make_config(), LOGGED_IN, [_ok_info(), _ok_info()])

    first = await api.acquire_token(804456)
    second = await api.acquire_token(804456)  # 模拟断线重连

    assert first.source == TokenSource.GET_DANMU_INFO_WBI.value
    assert second.source == TokenSource.GET_DANMU_INFO_WBI.value
    assert api.conf_calls == 0
    assert api.info_calls == [True, True]


async def test_risk_control_blocks_room_within_process(make_config) -> None:
    """真的 -352 时才进黑名单，并在本进程内不再重试 getDanmuInfo。"""
    api = StubApi(make_config(), LOGGED_IN, [ApiResult(code=-352, message="-352")])

    first = await api.acquire_token(804456)
    second = await api.acquire_token(804456)

    assert first.source == TokenSource.GET_CONF.value
    assert second.source == TokenSource.GET_CONF.value
    assert api.info_calls == [True], "第二次不应再尝试 getDanmuInfo"
    assert api.conf_calls == 2


async def test_non_risk_error_does_not_block_room(make_config) -> None:
    """非风控错误（如 60009）不应永久拉黑，下次仍要尝试主路径。"""
    api = StubApi(
        make_config(),
        LOGGED_IN,
        [ApiResult(code=60009, message="分区不存在"), _ok_info()],
    )

    first = await api.acquire_token(804456)
    second = await api.acquire_token(804456)

    assert first.source == TokenSource.GET_CONF.value
    assert second.source == TokenSource.GET_DANMU_INFO_WBI.value
    assert api.info_calls == [True, True]


async def test_anonymous_skips_getdanmuinfo_entirely(make_config) -> None:
    """匿名态直接走 getConf，不浪费请求。"""
    api = StubApi(make_config(), ANONYMOUS, [_ok_info()])
    bundle = await api.acquire_token(804456)

    assert api.info_calls == []
    assert api.conf_calls == 1
    assert bundle.source == TokenSource.GET_CONF.value


async def test_force_getconf_bypasses_primary_path(make_config) -> None:
    api = StubApi(make_config(), LOGGED_IN, [_ok_info()])
    bundle = await api.acquire_token(804456, force_getconf=True)

    assert api.info_calls == []
    assert bundle.source == TokenSource.GET_CONF.value


async def test_incomplete_info_response_falls_back_to_conf(make_config) -> None:
    """code=0 但缺节点时降级而不是抛异常 —— 无人值守采集不能因此中断。"""
    api = StubApi(
        make_config(), LOGGED_IN, [ApiResult(code=0, data={"token": "t", "host_list": []})]
    )
    bundle = await api.acquire_token(804456)

    assert bundle.source == TokenSource.GET_CONF.value
    assert api.conf_calls == 1
