"""过滤规则单测。每条语义都在 TECH_DESIGN §11 写明，这里逐条固定。"""

from __future__ import annotations

import pytest

from bili_danmaku.filters import FilterError, FilterSpec, build_filter
from bili_danmaku.models import Medal


def test_default_filter_is_content_only() -> None:
    """默认开启 content_only ——「无其它条件」并不等于「不过滤」。"""
    spec = build_filter()
    assert spec.content_only is True
    assert spec.active is True


def test_all_events_mode_is_inactive(make_event) -> None:
    """显式关闭 content_only 且无其它条件时，规则退化为无操作。"""
    spec = build_filter(content_only=False)
    assert spec.active is False
    assert spec.match(make_event(type="interact", text=None)) is True


# --------------------------------------------------------------------------- #
# 内容规则（本次方向调整的核心）
# --------------------------------------------------------------------------- #


def test_content_only_keeps_text_danmaku(make_event) -> None:
    spec = build_filter()
    assert spec.match(make_event(type="danmaku", text="你好")) is True


def test_content_only_keeps_super_chat(make_event) -> None:
    spec = build_filter()
    assert spec.match(make_event(type="super_chat", text="加油", price=30)) is True


@pytest.mark.parametrize(
    "event_type", ["interact", "guard_buy", "like", "room_stats", "notice", "dm_interaction", "other"]
)
def test_content_only_drops_non_text_events(make_event, event_type: str) -> None:
    """进房 / 上舰 / 点赞 / 房间数据等不产生用户文字，全部排除。"""
    spec = build_filter()
    assert spec.match(make_event(type=event_type, text="点什么")) is False


def test_content_only_drops_gift(make_event) -> None:
    """礼物带金额但没有用户文字，按需求同样排除（可用 --all-events 恢复）。"""
    spec = build_filter()
    assert spec.match(make_event(type="gift", text="辣条", price=500)) is False


def test_content_only_drops_empty_text(make_event) -> None:
    spec = build_filter()
    assert spec.match(make_event(type="danmaku", text=None)) is False
    assert spec.match(make_event(type="danmaku", text="")) is False
    assert spec.match(make_event(type="danmaku", text="   ")) is False


def test_content_only_keeps_emoticon_only_danmaku(make_event) -> None:
    """纯表情、text 为空的弹幕仍算有内容（表情占位符本身就是内容）。"""
    spec = build_filter()
    assert spec.match(make_event(type="danmaku", text=None, extra={"emoticon_unique": "room_1_2"})) is True
    assert spec.match(make_event(type="danmaku", text=None, extra={"emots": {"[dog]": {}}})) is True


def test_all_events_mode_keeps_everything(make_event) -> None:
    spec = build_filter(content_only=False)
    assert spec.match(make_event(type="interact", text=None)) is True
    assert spec.match(make_event(type="gift", text="辣条")) is True
    assert spec.match(make_event(type="danmaku", text=None)) is True


def test_types_whitelist(make_event) -> None:
    spec = build_filter(types=["danmaku"])
    assert spec is not None
    assert spec.match(make_event(type="danmaku")) is True
    assert spec.match(make_event(type="gift")) is False


def test_keyword_or_mode(make_event) -> None:
    spec = build_filter(keywords=["哈", "草"])
    assert spec.match(make_event(text="哈哈哈哈")) is True
    assert spec.match(make_event(text="草")) is True
    assert spec.match(make_event(text="你好")) is False


def test_keyword_and_mode(make_event) -> None:
    spec = build_filter(keywords=["哈", "主播"], keyword_mode="and")
    assert spec.match(make_event(text="哈哈 主播好")) is True
    assert spec.match(make_event(text="哈哈哈哈")) is False


def test_keyword_is_case_insensitive(make_event) -> None:
    spec = build_filter(keywords=["SC"])
    assert spec.match(make_event(text="好 sc 啊")) is True


def test_keyword_regex_prefix(make_event) -> None:
    spec = build_filter(keywords=[r"re:^\d{3}$"])
    assert spec.match(make_event(text="123")) is True
    assert spec.match(make_event(text="12")) is False


def test_invalid_regex_raises() -> None:
    with pytest.raises(FilterError, match="正则语法错误"):
        build_filter(keywords=["re:[unclosed"])


def test_invalid_keyword_mode_raises() -> None:
    with pytest.raises(FilterError, match="keyword_mode"):
        FilterSpec(keywords=["a"], keyword_mode="xor")


def test_keyword_ignores_none_text(make_event) -> None:
    spec = build_filter(keywords=["哈"])
    assert spec.match(make_event(text=None)) is False


def test_include_uids_excludes_masked(make_event) -> None:
    """打码记录 uid=0，永远不该命中白名单。"""
    spec = build_filter(include_uids=[1001])
    assert spec.match(make_event(uid=1001)) is True
    assert spec.match(make_event(uid=0, uname="宅***")) is False
    assert spec.match(make_event(uid=2002)) is False


def test_exclude_uids(make_event) -> None:
    spec = build_filter(exclude_uids=[1001])
    assert spec.match(make_event(uid=1001)) is False
    assert spec.match(make_event(uid=2002)) is True
    # uid 为空时不应被误伤
    assert spec.match(make_event(uid=None)) is True


def test_min_price_only_applies_to_priced_types(make_event) -> None:
    """`--min-price` 意味着"只要高价值事件"；这里用 content_only=False 隔离金额规则本身。

    注意默认的 content_only=True 会把礼物/上舰先挡掉（它们没有用户文字），
    因此实际使用中金额规则主要对 SC 生效。
    """
    spec = build_filter(min_price=100, content_only=False)
    # SC 的 price 就是元
    assert spec.match(make_event(type="super_chat", price=100)) is True
    assert spec.match(make_event(type="super_chat", price=99)) is False
    # 礼物/上舰的 price 是金瓜子（1000 = 1 元），门槛要按 ×1000 比
    assert spec.match(make_event(type="gift", price=100_000)) is True
    assert spec.match(make_event(type="guard_buy", price=100_000)) is True
    assert spec.match(make_event(type="gift", price=500)) is False, "500 金瓜子 = 0.5 元，不该过 100 元门槛"
    assert spec.match(make_event(type="danmaku", text="你好")) is False


def test_min_price_threshold_converts_yuan_to_gold_bean(make_event) -> None:
    """回归：`--min-price` 的单位是元，不能拿金瓜子直接比（否则门槛差 1000 倍）。"""
    from bili_danmaku.filters import GOLD_BEAN_PER_YUAN, min_price_threshold

    assert GOLD_BEAN_PER_YUAN == 1000
    assert min_price_threshold("super_chat", 30) == 30, "SC 不需要换算"
    assert min_price_threshold("gift", 30) == 30_000, "礼物要换算成金瓜子"
    assert min_price_threshold("guard_buy", 1) == 1_000
    assert min_price_threshold("danmaku", 30) == 30

    # 端到端：30 元的礼物应该是 30000 金瓜子
    spec = build_filter(min_price=30, content_only=False)
    assert spec.match(make_event(type="gift", price=30_000)) is True
    assert spec.match(make_event(type="gift", price=29_999)) is False


def test_min_price_with_missing_price(make_event) -> None:
    spec = build_filter(min_price=1, content_only=False)
    assert spec.match(make_event(type="gift", price=None)) is False


def test_min_medal_level(make_event) -> None:
    spec = build_filter(min_medal_level=10)
    assert spec.match(make_event(medal=Medal(level=12))) is True
    assert spec.match(make_event(medal=Medal(level=9))) is False
    assert spec.match(make_event(medal=None)) is False


def test_exclude_masked(make_event) -> None:
    spec = build_filter(exclude_masked=True)
    assert spec.match(make_event(uid=0, uname="宅***")) is False
    assert spec.match(make_event(uid=1001, uname="逸***")) is False
    assert spec.match(make_event(uid=1001, uname="正常昵称")) is True


def test_conditions_combine_with_and(make_event) -> None:
    spec = build_filter(types=["danmaku"], keywords=["哈"])
    assert spec.match(make_event(type="danmaku", text="哈哈")) is True
    assert spec.match(make_event(type="danmaku", text="你好")) is False
    assert spec.match(make_event(type="gift", text="哈哈")) is False


def test_describe_reports_active_conditions() -> None:
    assert "仅文字弹幕/SC" in build_filter().describe()
    assert build_filter(content_only=False).describe() == "无过滤（采集全部事件类型）"
    spec = build_filter(keywords=["a", "b"], keyword_mode="or", exclude_masked=True)
    described = spec.describe()
    assert "关键词[a 或 b]" in described
    assert "排除打码身份" in described


def test_many_keywords_use_single_alternation(make_event) -> None:
    keywords = [f"词{i}" for i in range(40)]
    spec = build_filter(keywords=keywords)
    assert spec is not None
    assert spec._any_pattern is not None, "or 模式应合并为单个 alternation"
    assert spec.match(make_event(text="这里有个词7")) is True
    assert spec.match(make_event(text="都不匹配")) is False


def test_plain_keywords_are_treated_literally(make_event) -> None:
    """非 `re:` 前缀的关键词按字面量处理，正则元字符不应生效。"""
    spec = build_filter(keywords=["a.c"])
    assert spec.match(make_event(text="a.c")) is True
    assert spec.match(make_event(text="abc")) is False
