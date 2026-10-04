"""过滤规则。

语义（与 TECH_DESIGN §11 一致，逐条都有测试固定）：

* `types`            —— 白名单，None 表示不限
* `keywords`         —— `or` 模式任一命中即通过；`and` 模式需全部命中；
                         `re:` 前缀走正则，其余按字面量（大小写不敏感）
* `include_uids`     —— 白名单；`uid` 为空（打码）的记录**永不**命中
* `exclude_uids`     —— 黑名单
* `min_price`        —— **只对带金额的类型生效**（礼物 / SC / 上舰），
                         其余类型视为不通过。用它就意味着"我只要高价值事件"
* `min_medal_level`  —— 无粉丝牌视为不通过
* `exclude_masked`   —— 丢弃被平台打码的身份（uid=0 或昵称含 `***`）

`match()` 是纯函数、无 IO；关键词预编译为单个 alternation 交给 re 的 C 层扫描。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Pattern

from .models import CONTENT_TYPES, Event, EventType
from .utils import is_masked_identity

log = logging.getLogger("bili_danmaku.filters")

#: 带金额的事件类型。`--min-price` 只对它们生效
PRICED_TYPES = frozenset(
    {
        EventType.GIFT.value,
        EventType.SUPER_CHAT.value,
        EventType.GUARD_BUY.value,
    }
)

#: 金瓜子换算：礼物/上舰的 `price` 是金瓜子，1000 金瓜子 = 1 元。
GOLD_BEAN_PER_YUAN = 1000

#: `--min-price` 的输入单位是**元**（文档与 `sc` 命令都以元为准）。
#: SC 的 `price` 本身就是元，直接比；礼物/上舰是金瓜子，要 ×1000 才能比 ——
#: 否则用 `--min-price 30` 会把"30 元 SC"和"3 分钱礼物"混为一谈。
_GOLD_BEAN_TYPES = frozenset({EventType.GIFT.value, EventType.GUARD_BUY.value})


def min_price_threshold(event_type: str | None, min_price: int) -> int:
    """把「元」门槛换算成该类型的原始 `price` 门槛。"""
    return min_price * GOLD_BEAN_PER_YUAN if event_type in _GOLD_BEAN_TYPES else min_price

#: 关键词前缀：`re:xxx` 表示把 xxx 当正则
REGEX_PREFIX = "re:"


class FilterError(ValueError):
    """过滤条件非法（如正则语法错误、模式取值非法）。"""


def _compile_keyword(keyword: str) -> Pattern[str]:
    if keyword.lower().startswith(REGEX_PREFIX):
        raw = keyword[len(REGEX_PREFIX) :]
    else:
        raw = re.escape(keyword)
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error as exc:
        raise FilterError(f"关键词 {keyword!r} 的正则语法错误: {exc}") from exc


class FilterSpec:
    """不可变过滤规则集。"""

    def __init__(
        self,
        *,
        types: Any = None,
        keywords: Any = (),
        keyword_mode: str = "or",
        include_uids: Any = None,
        exclude_uids: Any = (),
        min_price: int | None = None,
        min_medal_level: int | None = None,
        exclude_masked: bool = False,
        content_only: bool = True,
    ) -> None:
        mode = (keyword_mode or "or").lower()
        if mode not in ("or", "and"):
            raise FilterError(f"keyword_mode 只支持 'or' / 'and'，得到 {keyword_mode!r}")

        #: 只保留「有用户文字内容」的弹幕与 SC（默认开启，见 models.CONTENT_TYPES）。
        #: 进房 / 上舰 / 点赞 / 礼物 / 房间数据等无文字事件会被排除。
        self.content_only = content_only
        self.types = frozenset(types) if types else None
        self.keyword_mode = mode
        self.include_uids = frozenset(include_uids) if include_uids else None
        self.exclude_uids = frozenset(exclude_uids or ())
        self.min_price = min_price
        self.min_medal_level = min_medal_level
        self.exclude_masked = exclude_masked

        self.keywords = tuple(k for k in (keywords or ()) if k)
        self._patterns = tuple(_compile_keyword(k) for k in self.keywords)

        # or 模式：合并成单个 alternation，只扫一遍文本
        self._any_pattern: Pattern[str] | None = None
        if self._patterns and self.keyword_mode == "or":
            self._any_pattern = re.compile(
                "|".join(f"(?:{pattern.pattern})" for pattern in self._patterns),
                re.IGNORECASE,
            )

    # ------------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        """是否设置了任何条件（用于日志与「无过滤」快速路径）。"""
        return any(
            (
                self.content_only,
                self.types is not None,
                bool(self._patterns),
                self.include_uids is not None,
                bool(self.exclude_uids),
                self.min_price is not None,
                self.min_medal_level is not None,
                self.exclude_masked,
            )
        )

    def match(self, event: Event) -> bool:
        """返回 True 表示该事件应被保留。"""
        if self.content_only and not event.is_content_event():
            return False

        if self.types is not None and event.type not in self.types:
            return False

        if self.exclude_masked and is_masked_identity(event.uid, event.uname):
            return False

        if self.include_uids is not None and (event.uid or 0) not in self.include_uids:
            return False

        if event.uid and event.uid in self.exclude_uids:
            return False

        if self.min_price is not None:
            if event.type not in PRICED_TYPES:
                return False
            if event.price is None or event.price < min_price_threshold(event.type, self.min_price):
                return False

        if self.min_medal_level is not None:
            level = event.medal.level if event.medal else None
            if level is None or level < self.min_medal_level:
                return False

        if self._patterns:
            text = event.text or ""
            if self._any_pattern is not None:
                if not self._any_pattern.search(text):
                    return False
            elif not all(pattern.search(text) for pattern in self._patterns):
                return False

        return True

    # ------------------------------------------------------------------ #
    def describe(self) -> str:
        if not self.active:
            return "无过滤（采集全部事件类型）"
        parts: list[str] = []
        if self.content_only:
            parts.append("仅文字弹幕/SC")
        if self.types is not None:
            parts.append(f"类型={','.join(sorted(self.types))}")
        if self.keywords:
            joiner = " 且 " if self.keyword_mode == "and" else " 或 "
            parts.append(f"关键词[{joiner.join(self.keywords)}]")
        if self.include_uids is not None:
            parts.append(f"uid白名单={len(self.include_uids)}个")
        if self.exclude_uids:
            parts.append(f"uid黑名单={len(self.exclude_uids)}个")
        if self.min_price is not None:
            parts.append(f"金额>={self.min_price}元")
        if self.min_medal_level is not None:
            parts.append(f"粉丝牌>={self.min_medal_level}")
        if self.exclude_masked:
            parts.append("排除打码身份")
        return " ".join(parts)


def build_filter(
    *,
    types: Any = None,
    keywords: Any = (),
    keyword_mode: str = "or",
    include_uids: Any = None,
    exclude_uids: Any = (),
    min_price: int | None = None,
    min_medal_level: int | None = None,
    exclude_masked: bool = False,
    content_only: bool = True,
) -> FilterSpec:
    """构造过滤规则。

    **总是返回 FilterSpec**（不再返回 None）：因为 `content_only` 默认开启，
    "无其它条件"并不等于"不过滤"。调用方用 `spec.active` 判断是否有条件生效。
    """
    return FilterSpec(
        types=types,
        keywords=keywords,
        keyword_mode=keyword_mode,
        include_uids=include_uids,
        exclude_uids=exclude_uids,
        min_price=min_price,
        min_medal_level=min_medal_level,
        exclude_masked=exclude_masked,
        content_only=content_only,
    )
