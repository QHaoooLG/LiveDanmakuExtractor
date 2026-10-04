"""`login.py` 的用例 —— 登录态与扫码流程，**与界面无关**。

这个模块是从 `launcher.py` 拆出来的：之前登录状态机想单测就得构造一个
`PanelController`（要 config、端口、凭据文件），于是自然少测、覆盖率掉到 85%。
拆开之后这些函数是**纯的 + 依赖注入**的，可以直接测。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bili_danmaku import credentials as cred_module
from bili_danmaku.credentials import (
    QR_EXPIRED,
    QR_NOT_SCANNED,
    QR_OK,
    QR_SCANNED_NOT_CONFIRMED,
)
from bili_danmaku.login import (
    DEFAULT_COOKIE_FILE,
    LoginStatus,
    QrLoginSession,
    check_status,
    load_creds,
    local_status,
    resolve_cookie_file,
)


def _creds(**cookies):
    return cred_module.Credentials.from_mapping(cookies, source="file") if cookies else cred_module.Credentials()


# --------------------------------------------------------------------------- #
# 凭据文件路径解析：三处都要算它，所以只留一处实现
# --------------------------------------------------------------------------- #

def test_resolve_cookie_file_prefers_explicit() -> None:
    assert resolve_cookie_file(".secrets/x.json", config=None) == ".secrets/x.json"


def test_resolve_cookie_file_falls_back_to_config() -> None:
    class _Api:
        cookie_file = "custom/cookies.json"

    class _Config:
        api = _Api()

    assert resolve_cookie_file(None, _Config()) == "custom/cookies.json"


def test_resolve_cookie_file_defaults_when_nothing_given() -> None:
    assert resolve_cookie_file(None, None) == DEFAULT_COOKIE_FILE


# --------------------------------------------------------------------------- #
# 读凭据失败也不能让上层崩
# --------------------------------------------------------------------------- #

def test_load_creds_degrades_to_empty_on_error(monkeypatch) -> None:
    """凭据文件读坏了 ⇒ 当成"未登录"，而不是抛异常。

    登录态检查是辅助功能，不能因为它让面板起不来或让托盘窗口崩掉。
    """
    def boom(*args, **kwargs):
        raise OSError("磁盘坏了")

    monkeypatch.setattr(cred_module, "load_credentials", boom)
    creds = load_creds("whatever.json")
    assert creds.logged_in is False


# --------------------------------------------------------------------------- #
# 离线判断
# --------------------------------------------------------------------------- #

def test_local_status_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(cred_module, "load_credentials", lambda *a, **k: _creds())
    status = local_status()
    assert status.logged_in is False
    assert status.has_sessdata is False
    assert status.checked is False, "离线判断不该声称已校验"
    assert "未登录" in status.message


def test_local_status_with_sessdata(monkeypatch) -> None:
    monkeypatch.setattr(
        cred_module, "load_credentials", lambda *a, **k: _creds(SESSDATA="abc", DedeUserID="1")
    )
    status = local_status()
    assert status.logged_in is True
    assert status.has_sessdata is True
    assert status.checked is False
    assert status.message == ""


# --------------------------------------------------------------------------- #
# 联网判断
# --------------------------------------------------------------------------- #

def _stub_api(monkeypatch, *, is_login: bool, uname: str = "", boom: Exception | None = None):
    """把 `probe_login` 换成桩，避开真网络。"""
    async def fake_probe(config, creds, *, timeout=10.0):
        if boom is not None:
            raise boom
        return is_login, {"uname": uname, "isLogin": is_login}

    monkeypatch.setattr("bili_danmaku.login.probe_login", fake_probe)


def test_check_status_ok(monkeypatch) -> None:
    monkeypatch.setattr(cred_module, "load_credentials", lambda *a, **k: _creds(SESSDATA="ok"))
    _stub_api(monkeypatch, is_login=True, uname="歪比巴布325")
    status = check_status(config=object())
    assert status.checked is True
    assert status.logged_in is True
    assert status.uname == "歪比巴布325"
    assert status.message == ""
    assert status.needs_relogin is False


def test_check_status_expired(monkeypatch) -> None:
    monkeypatch.setattr(cred_module, "load_credentials", lambda *a, **k: _creds(SESSDATA="stale"))
    _stub_api(monkeypatch, is_login=False)
    status = check_status(config=object())
    assert status.checked is True
    assert status.logged_in is False
    assert "失效" in status.message
    assert "扫码" in status.message or "登录" in status.message
    assert status.needs_relogin is True, "确认失效才该提示重新登录"


def test_check_status_network_error_is_not_relogin(monkeypatch) -> None:
    """**核心区分**：网络异常 ≠ 凭据失效。

    第一版把两者混在一起，于是网络不通也显示"凭据失效，请重新扫码"，
    把用户赶去白扫一次码。
    """
    monkeypatch.setattr(cred_module, "load_credentials", lambda *a, **k: _creds(SESSDATA="ok"))
    _stub_api(monkeypatch, is_login=False, boom=OSError("network down"))
    status = check_status(config=object())
    assert status.checked is False, "网络失败不该标记成已校验"
    assert "网络" in status.message
    assert "失效" not in status.message
    assert status.needs_relogin is False, "网络问题不该说需要重新登录"


def test_needs_relogin_false_without_any_credentials() -> None:
    """**没配过凭据**也不该说"需要重新登录"（那是引导，不是修故障）。"""
    assert LoginStatus(has_sessdata=False, checked=True, logged_in=False).needs_relogin is False


# --------------------------------------------------------------------------- #
# QrLoginSession 状态机（假 httpx 客户端，不打网络）
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """按"第几张码 + 该码的第几次轮询"回答，复刻真实服务端的粘滞行为。"""

    def __init__(self, polls: list[list[int]]) -> None:
        self._polls = [list(seq) for seq in polls]
        self._generation = 0
        self._index = 0
        self.cookies: dict[str, str] = {}
        self.generated = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        if "generate" in url:
            self.generated += 1
            return _FakeResponse(
                {"code": 0, "data": {"url": "https://example.com/qr", "qrcode_key": "k"}}
            )
        sequence = self._polls[min(self._generation, len(self._polls) - 1)]
        code = sequence[min(self._index, len(sequence) - 1)]
        self._index += 1
        if code == QR_EXPIRED:
            self._generation += 1
            self._index = 0
        if code == QR_OK:
            self.cookies["SESSDATA"] = "fresh"
        return _FakeResponse({"data": {"code": code}})


def _run_session(monkeypatch, tmp_path: Path, polls: list[list[int]], **kwargs):
    client = _FakeClient(polls)
    monkeypatch.setattr(cred_module, "QR_POLL_URL", "https://x/poll")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: client)
    saved: list[tuple] = []
    monkeypatch.setattr(
        cred_module, "save_credentials", lambda creds, path: saved.append((creds, path)) or Path(path)
    )
    monkeypatch.setattr(QrLoginSession, "POLL_INTERVAL", 0.01)

    session = QrLoginSession(cookie_file=str(tmp_path / "cookies.json"), **kwargs)
    asyncio.run(session._login())
    return session, saved


def _states(session: QrLoginSession) -> list[str]:
    """抽干队列拿**完整**序列。

    `session.poll()` 刻意只返回最新一条（UI 只关心现在是什么状态），
    所以需要序列的用例直接读队列。
    """
    out: list[str] = []
    while True:
        try:
            out.append(str(session._events.get_nowait().get("state")))
        except Exception:
            return out


def test_session_happy_path(monkeypatch, tmp_path: Path) -> None:
    session, saved = _run_session(
        monkeypatch, tmp_path, [[QR_NOT_SCANNED, QR_SCANNED_NOT_CONFIRMED, QR_OK]]
    )
    states = _states(session)
    assert states[0] == "qr"
    assert "waiting" in states and "scanned" in states
    assert states[-1] == "ok"
    assert session.credentials is not None
    assert len(saved) == 1, "登录成功后必须落盘一次"
    assert saved[0][1] == tmp_path / "cookies.json"


def test_session_expired_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    session, _ = _run_session(monkeypatch, tmp_path, [[QR_EXPIRED], [QR_OK]])
    states = _states(session)
    assert "expired" in states
    assert states.count("qr") == 2, "失效后应当重新申请二维码"
    assert states[-1] == "ok"


def test_session_gives_up_after_repeated_expiry(monkeypatch, tmp_path: Path) -> None:
    session, saved = _run_session(monkeypatch, tmp_path, [[QR_EXPIRED]])
    states = _states(session)
    assert states[-1] == "failed"
    assert not saved, "失败时不该落盘任何凭据"


def test_session_renders_qr_png(monkeypatch, tmp_path: Path) -> None:
    session, _ = _run_session(monkeypatch, tmp_path, [[QR_OK]])
    assert session.qr_png and session.qr_png[:8] == b"\x89PNG\r\n\x1a\n"
    assert session.qr_url == "https://example.com/qr"


def test_session_on_done_callback_receives_credentials(monkeypatch, tmp_path: Path) -> None:
    got: list = []
    session, _ = _run_session(monkeypatch, tmp_path, [[QR_OK]], on_done=got.append)
    assert len(got) == 1
    assert got[0] is session.credentials


def test_session_on_done_failure_does_not_break_login(monkeypatch, tmp_path: Path) -> None:
    """回调抛异常不该把"登录成功"变成"失败"。"""
    def boom(_creds):
        raise RuntimeError("回调炸了")

    session, saved = _run_session(monkeypatch, tmp_path, [[QR_OK]], on_done=boom)
    assert _states(session)[-1] == "ok"
    assert len(saved) == 1


def test_session_cancel_before_start_stops_immediately(monkeypatch, tmp_path: Path) -> None:
    """`cancel()` 之后流程要发 cancelled，而不是继续轮询。

    刻意**先置 stop 再跑**：这正是 `cancel()` 的语义（用户点了取消）。
    不要在"跑完一次正常流程之后再置 stop 重跑" —— 那会踩到
    `asyncio.run()` 在同一个 session 上第二次调用时的坑（Python 3.11 上实测挂住）。
    """
    client = _FakeClient([[QR_NOT_SCANNED]])
    monkeypatch.setattr(cred_module, "QR_POLL_URL", "https://x/poll")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: client)
    monkeypatch.setattr(QrLoginSession, "POLL_INTERVAL", 0.01)

    session = QrLoginSession(cookie_file=str(tmp_path / "c.json"))
    session.cancel()
    asyncio.run(session._login())

    states = _states(session)
    assert states == ["cancelled"], f"取消后不该继续轮询：{states}"
    assert client.generated == 0, "取消后不该还去申请二维码"


def test_session_cancel_midway_stops_polling(monkeypatch, tmp_path: Path) -> None:
    """轮询过程中被取消：下一次检查就退出。"""
    client = _FakeClient([[QR_NOT_SCANNED]])
    monkeypatch.setattr(cred_module, "QR_POLL_URL", "https://x/poll")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: client)
    monkeypatch.setattr(QrLoginSession, "POLL_INTERVAL", 0.01)

    session = QrLoginSession(cookie_file=str(tmp_path / "c.json"))

    async def scenario() -> None:
        # 起一个"稍后取消"的任务，然后跑登录流程
        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            session.cancel()

        task = asyncio.ensure_future(cancel_soon())
        await session._login()
        await task

    asyncio.run(scenario())
    assert "cancelled" in _states(session)


def test_session_poll_returns_only_latest(monkeypatch, tmp_path: Path) -> None:
    session, _ = _run_session(
        monkeypatch, tmp_path, [[QR_NOT_SCANNED, QR_SCANNED_NOT_CONFIRMED, QR_OK]]
    )
    latest = session.poll()
    assert latest is not None and latest["state"] == "ok"
    assert session.poll() is None, "抽干后应当返回 None"


def test_session_start_is_not_reentrant(monkeypatch, tmp_path: Path) -> None:
    session = QrLoginSession(cookie_file=str(tmp_path / "c.json"))
    monkeypatch.setattr(session, "_run", lambda: None)
    assert session.start() is True
    session._thread.join(timeout=5)
    assert session.start() is True, "线程已结束就能再次启动"


def test_session_status_text_matches_terminal(monkeypatch) -> None:
    """GUI 与终端必须用同一套文案（两处写两遍必然有一天不一致）。"""
    assert QrLoginSession.status_text(QR_NOT_SCANNED) == cred_module.qr_status_text(QR_NOT_SCANNED)
    assert "失效" in QrLoginSession.status_text(QR_EXPIRED)


@pytest.mark.parametrize("code", [QR_OK, QR_EXPIRED, QR_SCANNED_NOT_CONFIRMED, QR_NOT_SCANNED, 12345])
def test_session_status_text_never_raises(code: int) -> None:
    assert isinstance(QrLoginSession.status_text(code), str)
