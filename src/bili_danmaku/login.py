"""登录态：检查、扫码登录、失效判定 —— **与界面无关**的独立模块。

## 为什么单独拆出来

这块逻辑原本长在 `launcher.py` 里（`LoginStatus` / `QrLoginSession` /
`PanelController.check_login`），而 `launcher` 是"托盘应用的核心"。结果是：

* `launcher.py` 一路涨到 668 行，职责开始互相纠缠；
* 登录状态机要验证就得构造一个完整的 `PanelController`（要 config、要端口、
  要凭据文件），耦合太重；
* 后面还要给"**运行中**凭据失效"做提示（面板与托盘都要用），
  再堆在 launcher 里就会变成第二个上帝对象。

拆开之后依赖关系是干净的：

    credentials.py   ← 只管凭据本身（读写、扫码接口）
    login.py         ← 只管"当前登录态是什么" + 扫码流程驱动（标题）
    gui/panel_window ← 只管把状态渲染成提示条
    launcher.py      ← 只管托管面板进程

## 两类判断，别混

* `local_status()`：**不联网**，只看本地凭据文件有没有 SESSDATA。
  首屏要立刻有说法，而且"没凭据"时不必发请求。
* `check_status()`：**联网**打一次 `nav`，只有它返回的 `isLogin` 说了算 ——
  本地有 SESSDATA **不等于**它还有效（会过期、会被踢）。

⚠️ **网络异常 ≠ 凭据失效**。`check_status()` 遇到网络问题时返回
`checked=False` + 一条"无法校验"的说明，**不会**说"凭据失效" ——
否则会把用户赶去白扫一次码（这个坑已经踩过，见 GUI 侧 `describe_login`）。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("bili_danmaku.login")

#: 默认凭据文件（与 `config.api.cookie_file` 的默认值一致）
DEFAULT_COOKIE_FILE = ".secrets/cookies.json"


@dataclass
class LoginStatus:
    """登录态快照。"""

    logged_in: bool = False
    #: 凭据来源：`file` / `env` / `inline` / `none`
    source: str = "none"
    has_sessdata: bool = False
    uname: str = ""
    #: 面向用户的说明（成功时为空）
    message: str = ""
    #: 是否已经**联网确认**过。区分"没配过"、"没校验成"、"确认失效"的关键字段
    checked: bool = False

    @property
    def needs_relogin(self) -> bool:
        """是否**确认**需要重新登录（网络问题不算）。"""
        return self.checked and not self.logged_in and self.has_sessdata


def resolve_cookie_file(explicit: str | None, config: Any = None) -> str:
    """决定用哪个凭据文件：显式参数 > 配置 > 默认。

    抽出来是因为三处（launcher / login / GUI）都要算它，
    各写一遍必然有一天不一致。
    """
    if explicit:
        return str(explicit)
    from_config = getattr(getattr(config, "api", None), "cookie_file", None) if config else None
    return str(from_config or DEFAULT_COOKIE_FILE)


def load_creds(cookie_file: str | None) -> Any:
    """读凭据；**任何**异常都退化成"空凭据"。

    登录态检查是**辅助功能**，绝不能因为它让面板起不来或让窗口崩掉。
    """
    from .credentials import Credentials, load_credentials

    try:
        return load_credentials(cookie_file, None)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取凭据失败（按未登录处理）：%s: %s", type(exc).__name__, exc)
        return Credentials()


def local_status(cookie_file: str | None = None, *, config: Any = None) -> LoginStatus:
    """**不联网**的登录态：只看本地凭据文件。"""
    creds = load_creds(resolve_cookie_file(cookie_file, config))
    has = bool(getattr(creds, "sessdata", ""))
    status = LoginStatus(
        logged_in=has,
        source=str(getattr(creds, "source", "none")),
        has_sessdata=has,
        checked=False,
    )
    if not has:
        status.message = "未登录（未找到凭据，部分昵称会被平台打码）"
    return status


async def probe_login(config: Any, creds: Any, *, timeout: float = 10.0) -> tuple[bool, dict[str, Any]]:
    """打一次 `nav` 判断登录态；返回 `(是否登录, 账号数据)`。

    这是**协程**，由调用方决定怎么跑（`check_status()` 用 `asyncio.run`，
    面板的异步上下文可以直接 await）。
    """
    from .api import BiliApi

    async def _probe() -> tuple[bool, dict[str, Any]]:
        async with BiliApi(config, creds) as api:
            result = await api.nav()
        data = result.data or {}
        return result.code == 0 and bool(data.get("isLogin")), data

    return await asyncio.wait_for(_probe(), timeout=timeout)


def check_status(
    cookie_file: str | None = None,
    *,
    config: Any = None,
    timeout: float = 10.0,
) -> LoginStatus:
    """**联网**校验登录态；这是判断"凭证失效"的权威判据。

    网络异常单独走一条分支（`checked=False`），**不**声称凭据失效。
    """
    status = local_status(cookie_file, config=config)
    creds = load_creds(resolve_cookie_file(cookie_file, config))

    from .api import BiliApiError

    try:
        ok, data = asyncio.run(probe_login(config, creds, timeout=timeout))
    except (BiliApiError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
        # 网络问题：**不要**把用户赶去重新登录
        status.checked = False
        status.message = f"无法校验登录态（网络问题）：{type(exc).__name__}"
        return status

    status.checked = True
    status.logged_in = ok
    if ok:
        status.uname = str(data.get("uname") or "")
        status.message = ""
    else:
        status.message = "登录已失效（凭据过期或被踢），请重新扫码登录"
    return status


class QrLoginSession:
    """一次"扫码登录"的**后台**驱动，供 GUI 轮询。

    为什么需要它：扫码要"申请二维码 → 每 2 秒轮询 → 成功后保存"，
    全程要几十秒且**必须在后台**（tkinter 主线程一旦阻塞，窗口就假死了）。
    这里把它做成一个小状态机：

        session = QrLoginSession()
        session.start()                 # 起后台线程
        event = session.poll()          # {"state": "qr"|"scanned"|"ok"|"failed"|...}
        png = session.qr_png            # 拿到二维码后渲染好的 PNG

    **线程安全**：后台线程只往 `queue.Queue` 里放事件，`poll()` 在主线程消费。
    """

    #: 轮询间隔（秒）—— 与服务端约定一致
    POLL_INTERVAL = 2.0
    #: 单张二维码的有效期（秒）
    QR_TTL = 180.0
    #: 最多换几张二维码
    MAX_QR_REFRESH = 3

    def __init__(
        self,
        *,
        cookie_file: str | None = None,
        on_done: Callable[[Any], None] | None = None,
    ) -> None:
        self.cookie_file = resolve_cookie_file(cookie_file)
        #: 登录成功后的回调（收到 `Credentials`）；GUI 用它刷新提示条
        self.on_done = on_done
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.qr_png: bytes | None = None
        self.qr_url: str = ""
        self.credentials: Any = None

    # -- 状态码文案（与终端共用一套，避免两处说法不一致） --------------- #

    @staticmethod
    def status_text(code: int) -> str:
        from .credentials import qr_status_text

        return qr_status_text(code)

    # -- 生命周期 ------------------------------------------------------ #

    def start(self) -> bool:
        """起后台线程开始登录流程。已在跑时返回 False。"""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="qr-login", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        """请求停止（线程是 daemon，最坏情况随进程一起退出）。"""
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll(self) -> dict[str, Any] | None:
        """取**最新**事件；没有新事件返回 None。

        ⚠️ 刻意**只返回最新一条**并丢掉中间的（队列被抽干）：UI 只关心"现在是什么状态"，
        中间那些 `waiting` 没有渲染价值，留着只会让窗口在刷新时有延迟感。
        代价是调用方**看不到完整序列** —— 需要序列的场合（用例）直接读 `_events`。
        """
        latest = None
        while True:
            try:
                latest = self._events.get_nowait()
            except queue.Empty:
                return latest

    def _emit(self, state: str, **extra: Any) -> None:
        self._events.put({"state": state, **extra})

    # -- 实际流程（在后台线程里跑） ------------------------------------ #

    def _run(self) -> None:
        try:
            asyncio.run(self._login())
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成一条事件
            log.exception("扫码登录失败")
            self._emit("failed", message=f"{type(exc).__name__}: {exc}")

    async def _login(self) -> None:
        """申请二维码 → 轮询 → 成功后**落盘**。

        落盘走 `credentials.save_credentials()`（与 `bili-danmaku login`
        同一条路径、同一份文件），这样 CLI 与面板看到的是同一份凭据。
        """
        import httpx

        from .credentials import (
            QR_EXPIRED,
            QR_OK,
            _HEADERS,
            classify_qr_status,
            credentials_from_client,
            poll_qr_once,
            request_qr,
            render_qr_png,
            save_credentials,
        )

        async with httpx.AsyncClient(headers=_HEADERS, timeout=10.0, follow_redirects=True) as client:
            for attempt in range(1, self.MAX_QR_REFRESH + 1):
                if self._stop.is_set():
                    self._emit("cancelled")
                    return
                url, key = await request_qr(client)
                self.qr_url = url
                self.qr_png = render_qr_png(url)
                self._emit("qr", url=url, png=bool(self.qr_png), attempt=attempt)

                deadline = time.monotonic() + self.QR_TTL
                last = None
                while time.monotonic() < deadline:
                    if self._stop.is_set():
                        self._emit("cancelled")
                        return
                    await asyncio.sleep(self.POLL_INTERVAL)
                    code = await poll_qr_once(client, key)
                    if code != last:
                        last = code
                        self._emit(
                            classify_qr_status(code),
                            code=code,
                            text=self.status_text(code),
                        )
                    if code == QR_OK:
                        creds = credentials_from_client(client)
                        target = Path(self.cookie_file)
                        save_credentials(creds, target)
                        self.credentials = creds
                        if callable(self.on_done):
                            try:
                                self.on_done(creds)
                            except Exception:  # noqa: BLE001 - 回调失败不该影响登录结果
                                log.exception("登录成功回调执行失败")
                        self._emit("ok", cookie_file=str(target))
                        return
                    if code == QR_EXPIRED:
                        break  # 换一张码
            self._emit("failed", message="二维码多次失效，请重试")
