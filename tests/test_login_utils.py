"""M4 覆盖率：扫码登录流程与通用工具（`credentials.py` / `utils.py`）。

扫码登录此前完全没有测试（`credentials.py` 62%），而它偏偏是**唯一会接触明文
凭据**的代码路径，值得单独锁死：解析出的凭据只能落在白名单字段上，
失败路径必须抛 `LoginError` 而不是静默返回半成品。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
import pytest

from bili_danmaku import utils
from bili_danmaku.credentials import (
    ALLOWED_COOKIE_KEYS,
    QR_EXPIRED,
    QR_NOT_SCANNED,
    QR_OK,
    QR_SCANNED_NOT_CONFIRMED,
    Credentials,
    LoginError,
    fetch_buvid,
    qr_login,
    render_qr,
)

SESSDATA = "login-sessdata-value-0123456789"


class QrServer:
    """按脚本回应二维码申请与轮询的假 passport 服务。"""

    def __init__(
        self,
        *,
        generate: list[dict] | None = None,
        statuses: list[int] | None = None,
        set_cookie: bool = True,
    ) -> None:
        self.generate = list(generate or [{"url": "https://qr.example/x", "qrcode_key": "KEY"}])
        self.statuses = list(statuses or [QR_OK])
        self.set_cookie = set_cookie
        self.generate_calls = 0
        self.poll_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "generate" in request.url.path:
            self.generate_calls += 1
            data = self.generate[min(self.generate_calls - 1, len(self.generate) - 1)]
            return httpx.Response(200, json={"code": 0, "data": data})

        self.poll_calls += 1
        status = self.statuses[min(self.poll_calls - 1, len(self.statuses) - 1)]
        if status == QR_OK and self.set_cookie:
            headers = [
                ("set-cookie", f"SESSDATA={SESSDATA}; Path=/; Domain=.bilibili.com"),
                ("set-cookie", "DedeUserID=8431064; Path=/; Domain=.bilibili.com"),
                ("set-cookie", "tracker=evil; Path=/; Domain=.bilibili.com"),
            ]
            return httpx.Response(200, json={"code": 0, "data": {"code": status}}, headers=headers)
        return httpx.Response(200, json={"code": 0, "data": {"code": status}})


def patch_transport(monkeypatch: pytest.MonkeyPatch, server: QrServer) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(server.handler), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_qr_login_returns_whitelisted_credentials(monkeypatch) -> None:
    server = QrServer(statuses=[QR_NOT_SCANNED, QR_SCANNED_NOT_CONFIRMED, QR_OK])
    patch_transport(monkeypatch, server)
    monkeypatch.setattr("bili_danmaku.credentials.render_qr", lambda url: True)
    notes: list[str] = []

    creds = await qr_login(timeout=5.0, interval=0.0, notify=notes.append)

    assert creds.logged_in and creds.sessdata == SESSDATA
    assert creds.dede_user_id == 8431064
    assert set(creds.cookies) <= set(ALLOWED_COOKIE_KEYS)
    assert "tracker" not in creds.cookies, "非白名单 cookie 一律丢弃"
    assert server.generate_calls == 1
    assert any("等待扫码" in note for note in notes)
    assert any("已扫码" in note for note in notes)
    assert any("登录成功" in note for note in notes)


async def test_qr_login_warns_when_qr_cannot_be_rendered(monkeypatch) -> None:
    server = QrServer(statuses=[QR_OK])
    patch_transport(monkeypatch, server)
    monkeypatch.setattr("bili_danmaku.credentials.render_qr", lambda url: False)
    notes: list[str] = []

    await qr_login(timeout=5.0, interval=0.0, notify=notes.append)
    assert any("未安装 qrcode" in note for note in notes), "渲染失败要给出可复制的链接"
    assert any("qr.example" in note for note in notes)


async def test_qr_login_refreshes_expired_qr(monkeypatch) -> None:
    server = QrServer(statuses=[QR_EXPIRED, QR_OK])
    patch_transport(monkeypatch, server)
    monkeypatch.setattr("bili_danmaku.credentials.render_qr", lambda url: True)
    notes: list[str] = []

    creds = await qr_login(timeout=5.0, interval=0.0, notify=notes.append, max_qr_refresh=2)

    assert creds.logged_in
    assert server.generate_calls == 2, "失效后应重新申请二维码"
    assert any("已失效" in note for note in notes)


async def test_qr_login_gives_up_after_max_refresh(monkeypatch) -> None:
    server = QrServer(statuses=[QR_EXPIRED])
    patch_transport(monkeypatch, server)
    monkeypatch.setattr("bili_danmaku.credentials.render_qr", lambda url: True)

    with pytest.raises(LoginError, match="多次失效"):
        await qr_login(timeout=5.0, interval=0.0, notify=lambda _: None, max_qr_refresh=2)
    assert server.generate_calls == 2


async def test_qr_login_times_out(monkeypatch) -> None:
    server = QrServer(statuses=[QR_NOT_SCANNED])
    patch_transport(monkeypatch, server)
    monkeypatch.setattr("bili_danmaku.credentials.render_qr", lambda url: True)

    with pytest.raises(LoginError, match="超时"):
        await qr_login(timeout=0.01, interval=0.0, notify=lambda _: None)


async def test_qr_login_reports_generate_failure(monkeypatch) -> None:
    server = QrServer()
    server.handler = lambda request: httpx.Response(200, json={"code": -412, "message": "风控"})  # type: ignore[method-assign]
    patch_transport(monkeypatch, server)

    with pytest.raises(LoginError, match="申请二维码失败"):
        await qr_login(timeout=1.0, interval=0.0, notify=lambda _: None)


async def test_qr_login_rejects_incomplete_generate_payload(monkeypatch) -> None:
    server = QrServer(generate=[{"url": "", "qrcode_key": ""}])
    patch_transport(monkeypatch, server)

    with pytest.raises(LoginError, match="缺少 url 或 qrcode_key"):
        await qr_login(timeout=1.0, interval=0.0, notify=lambda _: None)


async def test_qr_login_requires_sessdata_in_callback(monkeypatch) -> None:
    """回调 code=0 但没拿到 SESSDATA 时必须失败，不能返回空凭据。"""
    server = QrServer(statuses=[QR_OK], set_cookie=False)
    patch_transport(monkeypatch, server)
    monkeypatch.setattr("bili_danmaku.credentials.render_qr", lambda url: True)

    with pytest.raises(LoginError, match="未取到 SESSDATA"):
        await qr_login(timeout=5.0, interval=0.0, notify=lambda _: None)


# --------------------------------------------------------------------------- #
# 设备指纹
# --------------------------------------------------------------------------- #


async def test_fetch_buvid_returns_pair() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 0, "data": {"b_3": "B3", "b_4": "B4"}})
        )
    ) as client:
        assert await fetch_buvid(client) == ("B3", "B4")


async def test_fetch_buvid_swallows_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_buvid(client) == (None, None)


def test_render_qr_returns_bool() -> None:
    """渲染成功与否只返回布尔值 —— 终端能力差异不该让登录流程崩掉。"""
    assert isinstance(render_qr("https://qr.example/x"), bool)


# --------------------------------------------------------------------------- #
# utils：脱敏 / 退避 / 限流 / 权限
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    utils.clear_registered_secrets()
    yield
    utils.clear_registered_secrets()


def test_register_secrets_accepts_dict_and_iterable() -> None:
    utils.register_secrets({"SESSDATA": "dict-secret-value"})
    assert "dict-secret-value" not in utils.redact("v=dict-secret-value")

    utils.register_secrets(["list-secret-value"])
    assert "list-secret-value" not in utils.redact("v=list-secret-value")


def test_register_secrets_ignores_odd_input() -> None:
    utils.register_secrets(42)  # 不可迭代 → 静默返回，不抛
    utils.register_secret(None)
    assert utils.redact("原样") == "原样"


def test_redact_handles_literal_secret_without_field_name() -> None:
    """D5 回归：裸凭据值（不带字段名）也必须被替换掉。"""
    utils.register_secret(SESSDATA)
    cleaned = utils.redact(f"请求里带了 {SESSDATA} 这串东西")
    assert SESSDATA not in cleaned
    assert "<redacted>" in cleaned


def test_redacting_filter_survives_broken_record() -> None:
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="%d", args=("不是数字",),
        exc_info=None,
    )
    assert utils.RedactingFilter().filter(record) is True, "日志格式化失败也不能抛"


def test_setup_logging_installs_redacting_handler() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        utils.setup_logging("DEBUG")
        assert root.level == logging.DEBUG
        assert any(
            isinstance(item, utils.RedactingFilter)
            for handler in root.handlers
            for item in handler.filters
        )
        assert logging.getLogger("httpx").level == logging.WARNING, "httpx 日志会带 query，需压低"
        # 未知级别名退回 INFO 而不是抛异常
        utils.setup_logging("不是级别")
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_backoff_grows_and_is_capped() -> None:
    assert utils.backoff_delay(0, base=1.0, cap=60.0, jitter=False) == 1.0
    assert utils.backoff_delay(3, base=1.0, cap=60.0, jitter=False) == 8.0
    assert utils.backoff_delay(99, base=1.0, cap=60.0, jitter=False) == 60.0
    assert utils.backoff_delay(-5, base=1.0, cap=60.0, jitter=False) == 1.0

    jittered = utils.backoff_delay(0, base=1.0, cap=60.0)
    assert 1.0 <= jittered <= 2.0, "抖动限定在 1 秒内"


async def test_rate_limiter_enforces_min_interval() -> None:
    limiter = utils.RateLimiter(0.12)
    await limiter.acquire()
    started = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - started >= 0.1


async def test_rate_limiter_serializes_concurrent_callers() -> None:
    limiter = utils.RateLimiter(0.05)
    await asyncio.gather(*(limiter.acquire() for _ in range(3)))
    assert limiter._min_interval == 0.05


async def test_rate_limiter_zero_interval_is_noop() -> None:
    limiter = utils.RateLimiter(0.0)
    await asyncio.gather(*(limiter.acquire() for _ in range(5)))


def test_ensure_dir_creates_and_returns_path(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    assert utils.ensure_dir(str(target)) == str(target)
    assert target.is_dir()


def test_harden_file_permissions_is_best_effort(tmp_path: Path) -> None:
    secret = tmp_path / "cookies.json"
    secret.write_text("{}", encoding="utf-8")
    utils.harden_file_permissions(str(secret))  # 不抛即通过
    utils.harden_file_permissions(str(tmp_path / "不存在.json"))


def test_is_masked_identity() -> None:
    assert utils.is_masked_identity(0, "某人") is True
    assert utils.is_masked_identity(None, None) is True
    assert utils.is_masked_identity(1001, "宅***") is True
    assert utils.is_masked_identity(1001, "正常昵称") is False
    assert utils.is_masked_identity(1001, None) is False


def test_anonymize_uid_is_deterministic_per_salt() -> None:
    assert utils.anonymize_uid(1001, "s") == utils.anonymize_uid(1001, "s")
    assert utils.anonymize_uid(1001, "s") != utils.anonymize_uid(1001, "t")
    assert utils.anonymize_uid(None, "s") is None


def test_now_returns_wall_clock() -> None:
    assert abs(utils.now() - time.time()) < 1.0


def test_credentials_cookie_header_round_trip() -> None:
    creds = Credentials(cookies={"SESSDATA": SESSDATA, "bili_jct": "jct"})
    header = creds.cookie_header()
    assert f"SESSDATA={SESSDATA}" in header and "bili_jct=jct" in header
    assert Credentials(cookies={"SESSDATA": SESSDATA, "empty": ""}).as_cookies() == {
        "SESSDATA": SESSDATA
    }
