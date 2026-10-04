"""房间号 / 直播间链接解析（`rooms.py`）。

重点是**不能猜**：用户从地址栏复制的东西五花八门，猜错会把数据抓进一个
完全无关的房间，而且不容易发现。所以这里既覆盖常见形式，也对
"看起来像但其实是别的路径"（如 `/blanc/8432038`）明确报错。
"""

from __future__ import annotations

import pytest

from bili_danmaku.rooms import extract_room_from_text, parse_room_input

#: 需求方给的真实样例
SAMPLE_LINK = (
    "https://live.bilibili.com/8432038?live_from=85001"
    "&spm_id_from=333.1365.live_users.item.click"
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 纯数字与短号
        ("8432038", 8432038),
        (8432038, 8432038),
        ("7878", 7878),
        ("  8432038  ", 8432038),
        # h5 链接（**需求方给的样例**）
        (SAMPLE_LINK, 8432038),
        # 无协议 / 无主机 / 带 index.html
        ("live.bilibili.com/8432038", 8432038),
        ("https://live.bilibili.com/8432038/index.html", 8432038),
        ("//live.bilibili.com/8432038", 8432038),
        # 显式参数
        ("https://live.bilibili.com/?room_id=8432038", 8432038),
        ("https://live.bilibili.com/?roomId=8432038&x=1", 8432038),
        # 移动端 / 其它域名
        ("https://m.live.bilibili.com/8432038", 8432038),
        ("https://www.bilibili.com/8432038", 8432038),
        # 结尾多余的斜杠与锚点
        ("https://live.bilibili.com/8432038/", 8432038),
        ("https://live.bilibili.com/8432038#anchor", 8432038),
    ],
)
def test_parses_common_forms(value, expected) -> None:
    assert parse_room_input(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "abc",
        "https://live.bilibili.com/",
        # 已知的非房间号路径段：**必须拒绝，不能把 8432038 当房间号**
        "https://live.bilibili.com/blanc/8432038",
        "https://live.bilibili.com/p/html/index.html",
        # 不是 B 站
        "https://example.com/8432038",
        "https://youtube.com/live/8432038",
        # 数字过大，明显不是房间号
        "https://live.bilibili.com/999999999999999",
    ],
)
def test_rejects_unrecognisable_input(value) -> None:
    with pytest.raises(ValueError):
        parse_room_input(value)


def test_blanc_path_is_rejected_not_guessed() -> None:
    """`/blanc/8432038` 里的数字不是房间号 —— 宁可报错也不猜。"""
    with pytest.raises(ValueError, match="没有房间号"):
        parse_room_input("https://live.bilibili.com/blanc/8432038")


def test_query_param_wins_over_path() -> None:
    """路径与查询串同时存在时，显式参数更可信。"""
    value = "https://live.bilibili.com/blanc/999?room_id=8432038"
    assert parse_room_input(value) == 8432038


def test_extract_from_free_text() -> None:
    assert extract_room_from_text(f"看这个 {SAMPLE_LINK} 很热闹") == 8432038
    assert extract_room_from_text("https://live.bilibili.com/7878") == 7878
    assert extract_room_from_text("没有链接") is None
    assert extract_room_from_text("") is None
