"""凭据管理：扫码登录、加载、清除、掩码。

隐私约束 —— 本模块**只**保存建立连接所必需的 cookie 值，
绝不保存账号资料（昵称/mid/头像/大会员状态等），且任何路径都不打印明文。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .utils import harden_file_permissions, mask, register_secrets

log = logging.getLogger("bili_danmaku.credentials")

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

#: 扫码状态码（实测文档值）
QR_OK = 0
QR_EXPIRED = 86038
QR_SCANNED_NOT_CONFIRMED = 86090
QR_NOT_SCANNED = 86101

#: 只保留这些字段；其余一律丢弃，避免意外留存账号信息
ALLOWED_COOKIE_KEYS = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "buvid4",
)

DEFAULT_STORE_PATH = ".secrets/cookies.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


class LoginError(Exception):
    """登录流程失败。"""


@dataclass
class Credentials:
    """运行期凭据。只承载 cookie，不承载任何账号资料。"""

    cookies: dict[str, str] = field(default_factory=dict)
    source: str = "none"  # none | env | file | inline

    def __post_init__(self) -> None:
        # 把凭据字面量登记进脱敏表，确保它们哪怕被裸打印也不会出现在日志里
        register_secrets(self.cookies)

    # ---------------------------------------------------------------- #
    @property
    def sessdata(self) -> str:
        return self.cookies.get("SESSDATA", "")

    @property
    def buvid3(self) -> str | None:
        return self.cookies.get("buvid3") or None

    @property
    def dede_user_id(self) -> int:
        try:
            return int(self.cookies.get("DedeUserID") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def logged_in(self) -> bool:
        return bool(self.sessdata)

    def as_cookies(self) -> dict[str, str]:
        return {k: v for k, v in self.cookies.items() if v}

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.as_cookies().items())

    def describe(self) -> str:
        """安全摘要，用于日志/输出。永不含明文。"""
        if not self.logged_in:
            return f"未登录 (source={self.source}, buvid3={'有' if self.buvid3 else '无'})"
        return (
            f"已登录 (source={self.source}, SESSDATA={mask(self.sessdata)}, "
            f"buvid3={'有' if self.buvid3 else '无'})"
        )

    def with_buvid(self, buvid3: str | None, buvid4: str | None = None) -> "Credentials":
        merged = dict(self.cookies)
        if buvid3:
            merged["buvid3"] = buvid3
        if buvid4:
            merged["buvid4"] = buvid4
        return Credentials(cookies=merged, source=self.source)

    # ---------------------------------------------------------------- #
    @classmethod
    def from_cookie_string(cls, raw: str, source: str = "inline") -> "Credentials":
        cookies: dict[str, str] = {}
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key and value:
                cookies[key.strip()] = value.strip()
        return cls(cookies={k: v for k, v in cookies.items() if k in ALLOWED_COOKIE_KEYS},
                   source=source)

    @classmethod
    def from_mapping(cls, mapping: Any, source: str = "file") -> "Credentials":
        if not isinstance(mapping, dict):
            return cls(source="none")
        return cls(
            cookies={
                k: str(v)
                for k, v in mapping.items()
                if k in ALLOWED_COOKIE_KEYS and v not in (None, "")
            },
            source=source,
        )


# --------------------------------------------------------------------------- #
# 加载 / 保存 / 清除
# --------------------------------------------------------------------------- #


def load_credentials(
    cookie_file: str | None = None,
    inline_cookie: str = "",
) -> Credentials:
    """按优先级加载凭据：环境变量 > 凭据文件 > 配置内联值。

    环境变量模式（BILI_SESSDATA）下凭据只存在于进程内存，**完全不落盘**。
    """
    env_cookies = {
        key: os.environ[f"BILI_{key.upper()}"]
        for key in ALLOWED_COOKIE_KEYS
        if os.environ.get(f"BILI_{key.upper()}")
    }
    if os.environ.get("BILI_SESSDATA"):
        return Credentials(cookies=env_cookies, source="env")

    path = Path(cookie_file or DEFAULT_STORE_PATH)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            creds = Credentials.from_mapping(payload.get("cookies") or {}, source="file")
            if creds.cookies:
                return creds
        except (OSError, ValueError) as exc:
            log.warning("凭据文件读取失败(%s): %s", path, exc)

    if inline_cookie:
        return Credentials.from_cookie_string(inline_cookie, source="inline")

    return Credentials(source="none")


def save_credentials(creds: Credentials, path: str | Path) -> Path:
    """把凭据写入本机文件（仅限白名单字段），并尽力收紧权限。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "created_at": time.time(),
        "note": "仅保存连接所需 cookie；不含账号资料。删除请执行 bili-danmaku purge",
        "cookies": {k: v for k, v in creds.cookies.items() if k in ALLOWED_COOKIE_KEYS},
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    harden_file_permissions(str(target))
    return target


def purge_credentials(cookie_file: str | None = None) -> list[str]:
    """删除凭据文件，返回被删除的路径列表。

    只删除显式指定（或默认）的那一个文件，避免误删其他位置的凭据。
    """
    removed: list[str] = []
    path = Path(cookie_file or DEFAULT_STORE_PATH)
    try:
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    except OSError as exc:
        log.warning("删除失败 %s: %s", path, exc)
    return removed


# --------------------------------------------------------------------------- #
# 扫码登录
# --------------------------------------------------------------------------- #


def render_qr(url: str) -> bool:
    """在终端渲染二维码。返回是否渲染成功。"""
    try:
        import qrcode  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return False
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:  # noqa: BLE001
        return False


#: PNG 文件头里 IHDR 的固定布局：8 字节签名 + 4 字节块长 + 4 字节 `IHDR`，
#: 接下来就是宽（4 字节，大端）与高（4 字节，大端）。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_WIDTH_AT = 16
_PNG_HEIGHT_AT = 20
_PNG_HEADER_BYTES = 24


def png_size(data: bytes | None) -> tuple[int, int] | None:
    """从 PNG 字节里读出 `(宽, 高)`；不是 PNG 或太短就返回 None。

    **只查文件头，不解码图片** —— 登录弹窗要用它来保证"窗口一定装得下二维码"，
    而在窗口创建那一刻去解码图片是没必要的开销（也会多引一个依赖）。

    为什么需要它：弹窗尺寸若靠 `pack` 自适应，在**高 DPI** 下会被 Tk 的缩放
    放大到超出窗口，二维码就被裁掉一角 —— 而二维码**缺一个角就扫不出来**，
    这正是这次要修的缺陷。所以尺寸必须能提前算出来。
    """
    if not data or len(data) < _PNG_HEADER_BYTES or not data.startswith(_PNG_MAGIC):
        return None
    width = int.from_bytes(data[_PNG_WIDTH_AT : _PNG_WIDTH_AT + 4], "big")
    height = int.from_bytes(data[_PNG_HEIGHT_AT : _PNG_HEIGHT_AT + 4], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def render_qr_png(url: str, *, box_size: int = 8, border: int = 2) -> bytes | None:
    """把二维码渲染成 **PNG 字节**；失败返回 None。

    图形界面用它（tkinter 里显示图片最省事的是 PNG/PhotoImage）。
    需要 `qrcode` + `Pillow`；**两者缺一就返回 None**，由调用方退回
    "显示链接让用户自己打开" —— 登录功能不能因为缺一个可选依赖就整个不可用。
    """
    try:
        import io

        import qrcode  # type: ignore[import-not-found]

        qr = qrcode.QRCode(border=border, box_size=box_size)
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - 渲染失败不该影响登录流程
        return None


#: 扫码轮询的返回码 → 人话（GUI 与终端共用同一套文案，避免两处说法不一致）
QR_STATUS_TEXT: dict[int, str] = {
    QR_NOT_SCANNED: "等待扫码…",
    QR_SCANNED_NOT_CONFIRMED: "已扫码，请在手机上确认登录",
    QR_EXPIRED: "二维码已失效，正在重新申请…",
    QR_OK: "登录成功",
}


def qr_status_text(code: int) -> str:
    """把扫码轮询的返回码翻成人话（未知码也给出可读的回退）。"""
    return QR_STATUS_TEXT.get(int(code), f"扫码状态未知（code={code}）")


def classify_qr_status(code: int) -> str:
    """把轮询码归类成 `waiting` / `scanned` / `expired` / `ok` / `unknown`。

    抽成**纯函数**是为了让 GUI 的分支判断能离线单测 —— 扫码流程里最容易写错的就是
    "哪个码该继续等、哪个该换一张码"，而真机扫码在测试环境里做不到。
    """
    code = int(code)
    if code == QR_OK:
        return "ok"
    if code == QR_EXPIRED:
        return "expired"
    if code == QR_SCANNED_NOT_CONFIRMED:
        return "scanned"
    if code == QR_NOT_SCANNED:
        return "waiting"
    return "unknown"


async def request_qr(client: httpx.AsyncClient) -> tuple[str, str]:
    """申请一张登录二维码，返回 `(扫码链接, qrcode_key)`。

    抽出来是给**图形界面**用的：GUI 需要"先拿到码渲染出来、再轮询"，
    而不是像 `qr_login()` 那样在一个函数里一路阻塞到底。
    """
    response = await client.get(QR_GENERATE_URL)
    payload = response.json()
    if payload.get("code") != 0:
        raise LoginError(f"申请二维码失败: code={payload.get('code')}")

    data = payload.get("data") or {}
    url = str(data.get("url") or "")
    qrcode_key = str(data.get("qrcode_key") or "")
    if not url or not qrcode_key:
        raise LoginError("申请二维码返回缺少 url 或 qrcode_key")
    return url, qrcode_key


async def poll_qr_once(client: httpx.AsyncClient, qrcode_key: str) -> int:
    """轮询一次扫码状态，返回原始 code。"""
    poll = await client.get(QR_POLL_URL, params={"qrcode_key": qrcode_key})
    body = poll.json()
    return int((body.get("data") or {}).get("code") or 0)


def credentials_from_client(client: httpx.AsyncClient) -> Credentials:
    """把客户端 cookie 里的登录态转成 `Credentials`；没拿到 SESSDATA 就报错。"""
    creds = Credentials.from_mapping(
        {k: v for k, v in client.cookies.items() if k in ALLOWED_COOKIE_KEYS},
        source="file",
    )
    if not creds.logged_in:
        raise LoginError("登录回调成功但未取到 SESSDATA")
    return creds


async def qr_login(
    *,
    timeout: float = 180.0,
    interval: float = 2.0,
    notify: Callable[[str], None] = print,
    max_qr_refresh: int = 3,
) -> Credentials:
    """扫码登录并返回凭据。凭据不落盘，由调用方决定是否 save。

    终端形态：一路阻塞到成功/失败。**图形界面不要用它** ——
    用 `request_qr()` + `poll_qr_once()` 自己驱动（见 `gui/panel_window.py` 的登录条）。
    """
    async with httpx.AsyncClient(headers=_HEADERS, timeout=10.0, follow_redirects=True) as client:
        for attempt in range(1, max_qr_refresh + 1):
            url, qrcode_key = await request_qr(client)

            notify(f"\n请用 B 站手机客户端扫描二维码（第 {attempt}/{max_qr_refresh} 次申请，有效期 180 秒）：\n")
            if not render_qr(url):
                notify(
                    "[提示] 未安装 qrcode 库，无法在终端渲染二维码。\n"
                    f"请在浏览器中打开以下链接后用手机 B 站 App 扫码：\n{url}\n"
                )

            deadline = time.monotonic() + timeout
            last_status: int | None = None
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                status = await poll_qr_once(client, qrcode_key)

                if status != last_status:
                    last_status = status
                    if status in QR_STATUS_TEXT:
                        notify(qr_status_text(status))
                    if status == QR_EXPIRED:
                        break

                if status == QR_OK:
                    creds = credentials_from_client(client)
                    notify("登录成功。")
                    return creds
            else:
                raise LoginError("扫码超时，请重新执行 bili-danmaku login")

            # 二维码失效 → 重新申请
        raise LoginError("二维码多次失效，已放弃。请检查网络后重试")


async def fetch_buvid(client: httpx.AsyncClient) -> tuple[str | None, str | None]:
    """获取设备指纹。用于提升连接稳定性（实测不能单独解决 -352 风控）。"""
    try:
        response = await client.get("https://api.bilibili.com/x/frontend/finger/spi")
        data = response.json().get("data") or {}
        return data.get("b_3"), data.get("b_4")
    except Exception as exc:  # noqa: BLE001
        log.warning("获取 buvid 失败: %s", exc)
        return None, None
