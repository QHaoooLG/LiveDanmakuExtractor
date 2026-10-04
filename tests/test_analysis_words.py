"""词频"更像人话"的三件事（需求方反馈"统计的词有一部分不符合日常表达"）。

A. **按说过的人数排序** —— 把"一个人刷 20 遍"压下去；
B. **自定义停用词** —— 把用户不想要的词剔掉；
C. **jieba 用户词典** —— 主播名/房间专名不再被切碎。

这三件都是"零新增依赖"的改动，比引入语言模型便宜得多（见 HANDOVER §5.14.2）。
"""

from __future__ import annotations

from collections import Counter

import pytest

from bili_danmaku import text as text_module
from bili_danmaku.analysis import (
    MAX_USERS_PER_WORD,
    WordBucket,
    words_from_buckets,
)
from bili_danmaku.server.app import _drop_stopwords


# --------------------------------------------------------------------------- #
# A. 按人数排序
# --------------------------------------------------------------------------- #

def _bucket(start: float, counts: dict[str, int], users: dict[str, list[int]]) -> WordBucket:
    bucket = WordBucket(start=start, counts=Counter(counts))
    for word, uids in users.items():
        for uid in uids:
            bucket.add_user(word, uid)
    return bucket


def test_words_from_buckets_ranks_by_users_when_asked() -> None:
    """按人数排序：一个人刷 20 遍的词，要排在 5 个人各说 1 遍的词**后面**。"""
    bucket = _bucket(
        start=0.0,
        counts={"刷屏词": 20, "真热点": 5},
        users={"刷屏词": [1000], "真热点": [2001, 2002, 2003, 2004, 2005]},
    )
    payload = words_from_buckets([bucket], by="users")
    words = [item["word"] for item in payload["items"]]
    assert words[0] == "真热点"
    assert payload["ranked_by"] == "users"


def test_words_from_buckets_keeps_count_ranking_by_default() -> None:
    """缺省口径仍是按次数（向后兼容，别悄悄改掉老行为）。"""
    bucket = _bucket(
        start=0.0,
        counts={"刷屏词": 20, "真热点": 5},
        users={"刷屏词": [1000], "真热点": [2001, 2002, 2003, 2004, 2005]},
    )
    payload = words_from_buckets([bucket])
    assert payload["items"][0]["word"] == "刷屏词"
    assert payload["ranked_by"] == "count"


def test_words_from_buckets_merges_users_across_buckets() -> None:
    """跨片取并集：同一个人在多个片里说同一个词，只算一个人。"""
    first = _bucket(0.0, {"词": 1}, {"词": [1000]})
    second = _bucket(60.0, {"词": 2}, {"词": [1000, 2000]})
    payload = words_from_buckets([first, second], by="users")
    item = payload["items"][0]
    assert item["count"] == 3
    assert item["users"] == 2, "同一 uid 跨片重复出现不该被重复计数"


def test_users_are_reported_not_zero() -> None:
    """回归：老实现把 `users` 恒置 0（因为只从计数合并），榜单无法按人数排。"""
    bucket = _bucket(0.0, {"词": 3}, {"词": [1000, 2000, 3000]})
    payload = words_from_buckets([bucket])
    assert payload["items"][0]["users"] == 3


def test_ranking_is_stable_on_ties() -> None:
    """人数相同时用次数兜底，再相同按词序 —— 否则榜单每次刷新都会"抖"。"""
    bucket = _bucket(
        start=0.0,
        counts={"乙": 2, "甲": 2, "丙": 2},
        users={"乙": [1], "甲": [1], "丙": [1]},
    )
    once = [i["word"] for i in words_from_buckets([bucket], by="users")["items"]]
    twice = [i["word"] for i in words_from_buckets([bucket], by="users")["items"]]
    assert once == twice


# --------------------------------------------------------------------------- #
# WordBucket.add_user 的边界
# --------------------------------------------------------------------------- #

def test_add_user_ignores_missing_and_zero_uid() -> None:
    """uid 为 None/0 不计入 —— uid=0 是"被打码的匿名用户"，
    把成千上万个匿名并成一个会让词看起来"很多人说"。"""
    bucket = WordBucket(start=0.0)
    bucket.add_user("词", None)
    bucket.add_user("词", 0)
    assert bucket.users.get("词") in (None, set())
    bucket.add_user("词", 1234)
    assert bucket.users["词"] == {1234}


def test_add_user_is_bounded() -> None:
    """每个词的 uid 集合必须有上限 —— 这是放在 `LiveWindow.add()` 热路径上的。"""
    bucket = WordBucket(start=0.0)
    for uid in range(1, MAX_USERS_PER_WORD + 200):
        bucket.add_user("词", uid)
    assert len(bucket.users["词"]) == MAX_USERS_PER_WORD


# --------------------------------------------------------------------------- #
# B. 自定义停用词
# --------------------------------------------------------------------------- #

def test_drop_stopwords_removes_matching_items() -> None:
    words = {
        "items": [
            {"word": "电梯", "count": 5, "users": 3},
            {"word": "呼呼", "count": 4, "users": 4},
        ],
        "total_words": 9,
    }
    result = _drop_stopwords(words, {"呼呼"})
    assert [item["word"] for item in result["items"]] == ["电梯"]
    # 原对象不该被就地改掉（调用方可能还在用）
    assert len(words["items"]) == 2


def test_drop_stopwords_is_case_insensitive() -> None:
    words = {"items": [{"word": "Fumo", "count": 1}], "total_words": 1}
    assert _drop_stopwords(words, {"fumo"})["items"] == []


def test_drop_stopwords_with_empty_set_is_noop() -> None:
    words = {"items": [{"word": "词", "count": 1}], "total_words": 1}
    assert _drop_stopwords(words, set()) == words


def test_load_stopwords_reads_file_and_ignores_comments(tmp_path) -> None:
    path = tmp_path / "stop.txt"
    path.write_text("# 注释\n呼呼\n  FUMO  \n\n电梯\n", encoding="utf-8")
    loaded = text_module.load_stopwords(path)
    assert loaded == {"呼呼", "fumo", "电梯"}


# --------------------------------------------------------------------------- #
# C. jieba 用户词典
# --------------------------------------------------------------------------- #

def test_add_custom_words_is_idempotent() -> None:
    """重复提交同一批词，第二次应该报 0 条新增（避免每次分析都去动 jieba）。"""
    words = ["六式zy", "测试专用词"]
    first = text_module.add_custom_words(words)
    second = text_module.add_custom_words(words)
    assert second == 0, "同一批词不该被反复提交"
    assert first >= 0  # 没有 jieba 时 first 也可能是 0（静默跳过）


def test_add_custom_words_skips_single_chars_and_blanks() -> None:
    """单字与空白不提交：单字会显著改变切分行为，而且本来被 min_length 过滤。"""
    assert text_module.add_custom_words(["", "  ", "甲"]) == 0


def test_add_custom_words_without_jieba_is_silent(monkeypatch) -> None:
    """没有 jieba 时必须静默降级，不能让分析失败。"""
    monkeypatch.setattr(text_module, "jieba_available", lambda: False)
    # 用一个没提交过的词，确保走的是"探测 jieba"这条分支
    assert text_module.add_custom_words(["全新未提交的词条"]) == 0
