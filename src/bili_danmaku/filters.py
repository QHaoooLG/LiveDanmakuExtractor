"""过滤规则。

语义（各条规则的边界都在代码里写死，不做模糊判断）：

* `types`            —— 白名单，None 表示不限
* `keywords`         —— `or` 模式任一命中即通过；`and` 模式需全部命中；
                         `re:` 前缀走正则，其余按字面量（大小写不敏感）
* `include_uids`     —— 白名单；`uid` 为空（打码）的记录**永不**命中
* `exclude_uids`     —— 黑名单
* `min_medal_level`  —— 无粉丝牌视为不通过
* `exclude_masked`   —— 丢弃被平台打码的身份（uid=0 或昵称含 `***`）

> **没有"按金额筛"这一条**（需求 2026-10-07 #4）：原来的 `min_price`
> （连同 `min_price_threshold` / `GOLD_BEAN_PER_YUAN` / `--price-unit`）
> 已随"SC 金额过滤功能"整体删除。它当年是为了 `sc --min-price` 报表存在的，
> 元 / 金瓜子两种单位混用很容易引出 1000 倍的偏差，所以这里必须换算清楚；
> 需求方明确不要这个功能之后，**留着它只会继续带来单位歧义**。
> 要做"只看高额事件"请用 `types=super_chat` 之类的类型过滤。

`match()` 是纯函数、无 IO；关键词预编译为单个 alternation 交给 re 的 C 层扫描。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Pattern

from .models import Event
from .utils import is_masked_identity

log = logging.getLogger("bili_danmaku.filters")

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
        min_medal_level=min_medal_level,
        exclude_masked=exclude_masked,
        content_only=content_only,
    )
