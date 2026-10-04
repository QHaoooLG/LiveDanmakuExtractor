"""房间号 / 直播间链接的解析。

**为什么单独一个模块**：面板与 CLI 都要接受"用户从浏览器地址栏复制过来的东西"，
而"链接里哪一段是房间号"这件事有若干坑（见下），必须只在一处解决。

支持的输入：

| 形式 | 例子 |
|------|------|
| 纯房间号 | `8432038` |
| 短号 | `7878` |
| 带查询串的直播间链接 | `https://live.bilibili.com/8432038?live_from=85001&spm_id_from=...` |
| 协议/主机省略 | `live.bilibili.com/8432038` |
| 带 `index.html` 的历史形式 | `https://live.bilibili.com/8432038/index.html` |
| 显式参数 | `https://live.bilibili.com/?room_id=8432038` |
| 移动端域名 | `https://m.live.bilibili.com/8432038` |

不支持的输入要**明确报错**（`ValueError`），而不是猜一个数字出来：
`https://live.bilibili.com/blanc/8432038` 里的 `8432038` 就不是房间号（它是
"直播间白名单/分区页"的路径段），猜错会把用户带到一个完全无关的房间。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

#: 只认 B 站直播域名；其它域名一律拒绝（避免把任意链接里的数字当房间号）
KNOWN_HOSTS = ("live.bilibili.com", "bilibili.com", "b23.tv")

#: 房间号上限（超过这个位数的一律认为不是房间号）
ROOM_ID_MAX = 10**12
_UNSET = object()


def _first_int(values: list[str] | None) -> object:
    for value in values or []:
        text = str(value).strip()
        if text.isdigit():
            return int(text)
    return _UNSET


def parse_room_input(value: object) -> int | str:
    """把用户输入归一成"可以喂给 `room_init` 的东西"。

    返回 `int`（解析出的房间号）或原始的纯数字字符串。
    **不查网络**：短号与真实号的换算仍然由 `room_init` 负责。

    Raises:
        ValueError: 输入为空、或是无法识别出房间号的链接。
    """
    if value is None:
        raise ValueError("房间号或直播间链接不能为空")

    text = str(value).strip()
    if not text:
        raise ValueError("房间号或直播间链接不能为空")

    # 纯数字（房间号 / 短号）直接放行
    if text.isdigit():
        return int(text)

    # 既不像链接、也不是数字 → 明确拒绝，不要猜
    if "://" not in text and "/" not in text and "." not in text:
        raise ValueError(f"无法识别的房间号或链接：{text}")

    if "://" not in text:
        # 协议相对写法（`//live.bilibili.com/123`）只补 scheme，否则会拼出 `https:////...`
        text = f"https:{text}" if text.startswith("//") else f"https://{text}"

    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host and not any(host == known or host.endswith(f".{known}") for known in KNOWN_HOSTS):
        raise ValueError(f"不是 B 站直播链接：{host}")

    # 1) 显式查询参数优先（`?room_id=123` 最没有歧义）
    query = parse_qs(parsed.query)
    for key in ("room_id", "roomId", "roomid"):
        found = _first_int(query.get(key))
        if found is not _UNSET:
            return found  # type: ignore[return-value]

    # 2) **只看路径的第一段**。
    #    直播间链接的形状是 `/<房间号>[/index.html]`，所以第一段是数字就是房间号；
    #    而 `/blanc/8432038`、`/p/xxx` 这类"具名分区"的第一段不是数字，直接拒绝 ——
    #    实测 `/p/html/index.html` 是 404，`/blanc/` 的语义也不明确，
    #    与其猜那个 8432038 是不是房间号，不如让用户粘一条规范的直播间链接。
    first = _first_path_segment(parsed.path)
    if first is not None and first.isdigit():
        number = int(first)
        # 极端大的数不是房间号（`/1234567890123456` 之类），拒绝而不是照发
        if 0 < number < ROOM_ID_MAX:
            return number

    raise ValueError(f"链接里没有房间号：{text}")


def _first_path_segment(path: str) -> str | None:
    """路径里第一个非空段（`/8432038/index.html` → `8432038`）。"""
    for raw in (path or "").split("/"):
        segment = raw.strip()
        if segment:
            return segment
    return None


#: 从任意文本里"找出"房间号（用于错误提示或宽松场景；面板走上面的严格解析）
_URL_HINT = re.compile(r"(?:live\.bilibili\.com/)(\d{1,12})")


def extract_room_from_text(text: str) -> int | None:
    """宽松地从一段文本里找直播间链接并取出房间号（找不到返回 None）。"""
    if not text:
        return None
    match = _URL_HINT.search(text)
    if not match:
        return None
    return int(match.group(1))
