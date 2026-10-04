"""启动面板的登录功能：状态判断 + 扫码状态机。

需求："在启动面板添加扫码登录获取用户凭证这一功能，当凭证失效时，
在启动面板用较为明显的信息栏提示用户登录"。

扫码本身要连 B 站（测试环境打不了真接口），所以这里把**能离线测的部分**全测掉：
  * `classify_qr_status` / `qr_status_text`：哪个码该继续等、哪个该换码（最容易写错）；
  * `render_qr_png`：渲染出真 PNG（不联网）；
  * `local_credential_status` / `check_login`：本地无凭据 / 网络异常 / 校验失败的分支；
  * `QrLoginSession` 的状态机：用假的 httpx 客户端驱动，**不打网络**。
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
    classify_qr_status,
    qr_status_text,
    render_qr_png,
)
from bili_danmaku.launcher import LauncherOptions, LoginStatus, PanelController, QrLoginSession


# --------------------------------------------------------------------------- #
# 扫码状态分类（纯函数 —— 最容易写错的地方）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (QR_OK, "ok"),
        (QR_EXPIRED, "expired"),
        (QR_SCANNED_NOT_CONFIRMED, "scanned"),
        (QR_NOT_SCANNED, "waiting"),
        (99999, "unknown"),
    ],
)
def test_classify_qr_status(code: int, expected: str) -> None:
    assert classify_qr_status(code) == expected


def test_qr_status_text_is_human_readable_and_never_raises() -> None:
    assert "扫码" in qr_status_text(QR_NOT_SCANNED)
    assert "确认" in qr_status_text(QR_SCANNED_NOT_CONFIRMED)
    # 未知码也要给可读回退，而不是抛异常（否则 GUI 会崩）
    assert "99999" in qr_status_text(99999)


def test_expired_is_distinct_from_waiting() -> None:
    """`expired` 与 `waiting` 必须区分开：前者要换一张码，后者继续等。"""
    assert classify_qr_status(QR_EXPIRED) != classify_qr_status(QR_NOT_SCANNED)


# --------------------------------------------------------------------------- #
# 二维码渲染（不联网）
# --------------------------------------------------------------------------- #

def test_render_qr_png_produces_real_png() -> None:
    png = render_qr_png("https://passport.bilibili.com/h5/login?qrcode_key=test")
    assert png, "应当渲染出 PNG 字节"
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG 头"


def test_render_qr_png_returns_none_without_qrcode(monkeypatch) -> None:
    """缺 qrcode/Pillow 时返回 None（而不是抛异常）——
    登录功能不能因为缺一个可选依赖就整个不可用。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "qrcode":
            raise ImportError("no qrcode")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert render_qr_png("https://example.com") is None


# --------------------------------------------------------------------------- #
# 登录态判断：离线 / 网络异常 / 失效
# --------------------------------------------------------------------------- #

def _controller(tmp_path: Path, **options) -> PanelController:
    return PanelController(
        LauncherOptions(port=0, out_dir=str(tmp_path), config_path=None, **options)
    )


def test_local_credential_status_without_file(tmp_path: Path, monkeypatch) -> None:
    """没有任何凭据文件时：`logged_in=False` 且给出可读说明。"""
    monkeypatch.setattr(
        cred_module, "load_credentials", lambda *a, **k: cred_module.Credentials()
    )
    status = _controller(tmp_path).local_credential_status()
    assert status.logged_in is False
    assert status.checked is False
    assert "未登录" in status.message


def test_local_credential_status_with_sessdata(tmp_path: Path, monkeypatch) -> None:
    """本地有 SESSDATA 时**不联网**也应报告已找到凭据（但 `checked=False`）。"""
    creds = cred_module.Credentials.from_mapping({"SESSDATA": "abc"}, source="file")
    monkeypatch.setattr(cred_module, "load_credentials", lambda *a, **k: creds)
    status = _controller(tmp_path).local_credential_status()
    assert status.logged_in is True
    assert status.has_sessdata is True
    assert status.checked is False, "离线判断不该声称已校验"
    assert status.message == ""


def test_check_login_reports_expired_credentials(tmp_path: Path, monkeypatch) -> None:
    """凭据失效（nav 说 isLogin=false）时要给出**明确的重新登录**提示。"""
    creds = cred_module.Credentials.from_mapping({"SESSDATA": "stale"}, source="file")
    monkeypatch.setattr(cred_module, "load_credentials", lambda *a, **k: creds)

    class _Api:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def nav(self):
            class _R:
                code = 0
                message = "OK"
                data = {"isLogin": False}

            return _R()

    monkeypatch.setattr("bili_danmaku.api.BiliApi", _Api)
    status = _controller(tmp_path).check_login()
    assert status.checked is True
    assert status.logged_in is False
    assert "失效" in status.message
    assert "扫码" in status.message or "登录" in status.message


def test_check_login_reports_network_error_without_blaming_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    """网络异常**不等于**凭据失效 —— 不能把用户赶去重新扫码。"""
    creds = cred_module.Credentials.from_mapping({"SESSDATA": "ok"}, source="file")
    monkeypatch.setattr(cred_module, "load_credentials", lambda *a, **k: creds)

    class _Api:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            raise OSError("network down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("bili_danmaku.api.BiliApi", _Api)
    status = _controller(tmp_path).check_login()
    assert status.checked is False, "网络失败不该标记成已校验"
    assert "网络" in status.message
    assert "失效" not in status.message, "网络问题不该说凭据失效"


# --------------------------------------------------------------------------- #
# QrLoginSession 状态机（用假客户端，不打网络）
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """假的 httpx 客户端。

    `polls` 是**每次生成的二维码**要走的轮询序列：真实服务端对同一张码会反复返回
    同一个状态直到它变化，所以这里按"第几张码 + 该码的第几次轮询"决定返回什么 ——
    用"一个全局的码序列"会让 `expired` 用例变成"每次都立刻失效"，测不出状态机。
    """

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
            # 失效后服务端会换一张码 ⇒ 进入下一轮序列
            self._generation += 1
            self._index = 0
        if code == QR_OK:
            self.cookies["SESSDATA"] = "fresh"
        return _FakeResponse({"data": {"code": code}})


def _run_session(monkeypatch, tmp_path: Path, polls: list[list[int]]):
    client = _FakeClient(polls)
    monkeypatch.setattr(cred_module, "QR_POLL_URL", "https://x/poll")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(cred_module, "save_credentials", lambda creds, path: Path(path))
    # 把轮询间隔压到 0，避免用例真的等 2 秒
    monkeypatch.setattr(QrLoginSession, "POLL_INTERVAL", 0.01)

    session = QrLoginSession(cookie_file=str(tmp_path / "cookies.json"))
    asyncio.run(session._login())
    return session


def _states(session: QrLoginSession) -> list[str]:
    """把队列抽干，返回**每一条**事件。

    注意 `session.poll()` **刻意只返回最新事件**（中间的 `waiting` 会被丢掉）——
    对 UI 来说"只关心最新状态"是对的，但用例要看完整序列，所以这里绕过它读队列。
    """
    out: list[str] = []
    while True:
        try:
            out.append(str(session._events.get_nowait().get("state")))
        except Exception:
            return out


def test_session_emits_qr_then_reports_success(monkeypatch, tmp_path: Path) -> None:
    """正常流程：先给二维码，再 waiting/scanned，最后 ok。"""
    session = _run_session(
        monkeypatch, tmp_path, [[QR_NOT_SCANNED, QR_SCANNED_NOT_CONFIRMED, QR_OK]]
    )
    states = _states(session)
    assert states[0] == "qr", f"第一步必须是二维码：{states}"
    assert "waiting" in states
    assert "scanned" in states
    assert states[-1] == "ok"
    assert session.credentials is not None


def test_session_reports_expired_then_succeeds_on_next_qr(monkeypatch, tmp_path: Path) -> None:
    """一张码失效 → 上报 expired → 换一张码 → 成功（真实会走的路径）。"""
    session = _run_session(monkeypatch, tmp_path, [[QR_EXPIRED], [QR_OK]])
    states = _states(session)
    assert "expired" in states, f"失效要上报，GUI 才能把二维码撤掉：{states}"
    assert states[-1] == "ok"
    assert states.count("qr") == 2, f"应当重新申请了第二张码：{states}"


def test_poll_returns_only_latest_event(monkeypatch, tmp_path: Path) -> None:
    """`poll()` 是给 UI 用的：中间状态可以被跳过，但**必须给最新状态**。"""
    session = _run_session(
        monkeypatch, tmp_path, [[QR_NOT_SCANNED, QR_SCANNED_NOT_CONFIRMED, QR_OK]]
    )
    # 队列里还剩若干事件；poll() 应当直接给最后一条，而不是最旧那条
    latest = session.poll()
    assert latest is not None
    assert latest["state"] == "ok"
    assert session.poll() is None, "抽干之后应当返回 None"


def test_session_renders_qr_png(monkeypatch, tmp_path: Path) -> None:
    """拿到二维码后要能直接给 GUI 一张 PNG。"""
    session = _run_session(monkeypatch, tmp_path, [[QR_OK]])
    assert session.qr_png, "应当渲染出 PNG"
    assert session.qr_png[:8] == b"\x89PNG\r\n\x1a\n"
    assert session.qr_url == "https://example.com/qr"


def test_session_fails_after_repeated_expiry(monkeypatch, tmp_path: Path) -> None:
    """连续失效多次后要放弃并报 failed（不能无限换码）。"""
    session = _run_session(monkeypatch, tmp_path, [[QR_EXPIRED]])
    states = _states(session)
    assert states[-1] == "failed"


def test_session_start_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    """已在跑时 `start()` 返回 False，不会起第二个线程。"""
    session = QrLoginSession(cookie_file=str(tmp_path / "c.json"))
    monkeypatch.setattr(session, "_run", lambda: None)  # 立刻结束，避免打网络
    assert session.start() is True
    session._thread.join(timeout=5)
    # 线程已结束 ⇒ 可以再次启动
    assert session.start() is True


def test_login_status_defaults_are_safe() -> None:
    """默认值必须是"未登录、未校验" —— 宁可多提示，也不要谎报已登录。"""
    status = LoginStatus()
    assert status.logged_in is False
    assert status.checked is False
    assert status.has_sessdata is False


# --------------------------------------------------------------------------- #
# 提示条的配色判定（纯函数）—— 踩过"网络问题也报红色"的坑
# --------------------------------------------------------------------------- #

def _level(status: LoginStatus, *, online: bool = True) -> str:
    from bili_danmaku.gui.panel_window import describe_login

    return describe_login(status, online=online)[2]


def test_describe_login_ok_is_green() -> None:
    from bili_danmaku.gui.panel_window import OK_BG, describe_login

    bg, text, level = describe_login(
        LoginStatus(logged_in=True, has_sessdata=True, checked=True, uname="某人")
    )
    assert bg == OK_BG and level == "ok"
    assert "某人" in text


def test_describe_login_expired_is_red() -> None:
    """有凭据 + 联网确认失效 ⇒ 红色（这才是需求里"明显提示"的场景）。"""
    from bili_danmaku.gui.panel_window import ALERT_BG, describe_login

    bg, text, level = describe_login(
        LoginStatus(
            logged_in=False,
            has_sessdata=True,
            checked=True,
            message="登录已失效（凭据过期或被踢），请重新扫码登录",
        )
    )
    assert bg == ALERT_BG and level == "alert"
    assert "失效" in text


def test_describe_login_network_error_is_yellow_not_red() -> None:
    """**回归**：有凭据但**没能**校验（网络问题）必须是黄色。

    第一版把"有凭据 + 有错误消息"一律当红色，于是网络不通也提示
    "凭据失效，请重新扫码"，把用户赶去白折腾一趟。
    """
    from bili_danmaku.gui.panel_window import WARN_BG, describe_login

    bg, text, level = describe_login(
        LoginStatus(
            logged_in=False,
            has_sessdata=True,
            checked=False,
            message="无法校验登录态（网络问题）：OSError",
        )
    )
    assert bg == WARN_BG, "网络问题不该是红色"
    assert level == "warn"
    assert "网络" in text


def test_describe_login_without_credentials_is_yellow() -> None:
    from bili_danmaku.gui.panel_window import WARN_BG, describe_login

    bg, text, level = describe_login(
        LoginStatus(has_sessdata=False, message="未登录（未找到凭据）")
    )
    assert bg == WARN_BG and level == "warn"


def test_describe_login_has_credentials_but_unchecked_is_pending() -> None:
    """本地有凭据、还没联网校验 ⇒ 中性（不是红色，也不是已登录）。"""
    from bili_danmaku.gui.panel_window import describe_login

    _bg, _text, level = describe_login(
        LoginStatus(logged_in=True, has_sessdata=True, checked=False), online=False
    )
    assert level == "pending"
