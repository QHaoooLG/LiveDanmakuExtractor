"""去重缓存单测。"""

from __future__ import annotations

from bili_danmaku.dedupe import DedupeCache


def test_first_sight_is_not_duplicate() -> None:
    cache = DedupeCache()
    assert cache.check_and_add("k1") is False
    assert cache.check_and_add("k1") is True


def test_different_keys_are_independent() -> None:
    cache = DedupeCache()
    assert cache.check_and_add("a") is False
    assert cache.check_and_add("b") is False
    assert cache.check_and_add("a") is True
    assert cache.check_and_add("b") is True


def test_ttl_expiry_allows_resend() -> None:
    """TTL 从**最后一次命中**开始计算（命中会刷新时间戳）。"""
    cache = DedupeCache(ttl=10.0)
    assert cache.check_and_add("k", moment=0.0) is False
    assert cache.check_and_add("k", moment=5.0) is True  # 命中刷新到 5.0
    # 直接跳到 20.0：距最后一次命中已 15 秒 > TTL，视为新事件
    assert cache.check_and_add("k", moment=20.0) is False


def test_zero_ttl_means_never_expire() -> None:
    cache = DedupeCache(ttl=0.0)
    assert cache.check_and_add("k", moment=0.0) is False
    assert cache.check_and_add("k", moment=10_000.0) is True


def test_maxsize_evicts_oldest() -> None:
    cache = DedupeCache(maxsize=3, ttl=0.0)
    for index in range(3):
        cache.check_and_add(f"k{index}")
    assert len(cache) == 3

    cache.check_and_add("k3")
    assert len(cache) == 3
    # k0 已被淘汰，因此再次出现时不算重复
    assert cache.check_and_add("k0") is False


def test_expired_entries_are_pruned_by_ttl() -> None:
    cache = DedupeCache(maxsize=100, ttl=1.0)
    for index in range(5):
        cache.check_and_add(f"k{index}", moment=0.0)
    assert len(cache) == 5
    # 触发一次清理
    cache.check_and_add("new", moment=10.0)
    assert len(cache) == 1


def test_prune_returns_removed_count() -> None:
    cache = DedupeCache(ttl=1.0)
    for index in range(4):
        cache.check_and_add(f"k{index}", moment=0.0)
    assert cache.prune(moment=10.0) == 4
    assert len(cache) == 0


def test_stats_track_hits_and_misses() -> None:
    cache = DedupeCache()
    cache.check_and_add("k")
    cache.check_and_add("k")
    assert cache.stats["hits"] == 1
    assert cache.stats["misses"] == 1
    assert cache.stats["size"] == 1
