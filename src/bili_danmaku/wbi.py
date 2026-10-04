"""WBI 签名。

B 站自 2023-03 起对部分 Web 接口启用 WBI 签名鉴权（query 中的 w_rid + wts）。
本模块只负责纯函数签名与密钥缓存，不做网络请求（由 api.py 注入 client）。

注意：按实测结论，WBI 用于 api.bilibili.com 域接口是确认的；
是否对 api.live.bilibili.com 的 getDanmuInfo 生效尚未验证，
因此调用方把它当作「-352 风控时的可选重试手段」，而非主路径。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

_KEY_CACHE_TTL = 3600.0


def get_mixin_key(raw_wbi_key: str) -> str:
    """把 img_key + sub_key 按重排表打乱，取前 32 位。"""
    if len(raw_wbi_key) < 64:
        raise ValueError(f"raw_wbi_key 长度不足 64，得到 {len(raw_wbi_key)}")
    return "".join(raw_wbi_key[i] for i in MIXIN_KEY_ENC_TAB[:32])


def encode_uri_component(value: Any) -> str:
    """按 ECMAScript encodeURIComponent 语义编码。

    与 urllib.parse.quote 的差异：过滤掉 !'()* 字符，
    百分号用大写、空格编码为 %20（而非 application/x-www-form-urlencoded 的 +）。
    """
    text = str(value)
    filtered = "".join(ch for ch in text if ch not in "!'()*")
    return urllib.parse.quote(filtered, safe="")


def build_signed_query(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    wts: int | None = None,
) -> str:
    """生成带 wts 与 w_rid 的查询串。"""
    mixin_key = get_mixin_key(img_key + sub_key)
    timestamp = int(wts if wts is not None else time.time())

    signed = {str(k): v for k, v in params.items()}
    signed["wts"] = timestamp

    query = "&".join(
        f"{encode_uri_component(k)}={encode_uri_component(signed[k])}" for k in sorted(signed)
    )
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={w_rid}"


def sign_params(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    wts: int | None = None,
) -> dict[str, Any]:
    """返回叠加了 wts / w_rid 的参数副本。"""
    signed_query = build_signed_query(params, img_key, sub_key, wts)
    out: dict[str, Any] = dict(params)
    for pair in signed_query.split("&"):
        key, _, value = pair.partition("=")
        out[urllib.parse.unquote(key)] = urllib.parse.unquote(value)
    return out


def parse_wbi_keys(nav_payload: dict[str, Any]) -> tuple[str, str]:
    """从 nav 响应里取出 img_key / sub_key。

    实测：未登录（code=-101）时 wbi_img 依然存在，因此无需登录即可签名。
    """

    def _filename(url: str) -> str:
        return url.rsplit("/", 1)[-1].split(".")[0]

    wbi_img = ((nav_payload.get("data") or {}).get("wbi_img")) or {}
    img_url = wbi_img.get("img_url") or ""
    sub_url = wbi_img.get("sub_url") or ""
    if not img_url or not sub_url:
        raise ValueError("nav 响应中缺少 wbi_img.img_url / sub_url")
    return _filename(img_url), _filename(sub_url)


class WbiKeyCache:
    """img_key / sub_key 缓存。观测到密钥按日更替，这里保守地 1 小时刷新。"""

    def __init__(self, ttl: float = _KEY_CACHE_TTL) -> None:
        self._ttl = ttl
        self._keys: tuple[str, str] | None = None
        self._fetched_at = 0.0

    def is_fresh(self) -> bool:
        return self._keys is not None and (time.time() - self._fetched_at) < self._ttl

    def get(self) -> tuple[str, str] | None:
        return self._keys

    def set(self, img_key: str, sub_key: str) -> None:
        self._keys = (img_key, sub_key)
        self._fetched_at = time.time()

    def invalidate(self) -> None:
        self._keys = None
        self._fetched_at = 0.0
