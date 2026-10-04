"""去重缓存：有界 LRU + TTL。

用途：合并「实时流」与「历史补种」，并抵御服务端的重复下发。
实测 `DANMU_MSG` 的 `info[0][15].extra.id_str` 是全局唯一消息 ID，
因此主流路径的去重非常可靠。
"""

from __future__ import annotations

import time
from collections import OrderedDict


class DedupeCache:
    """记住最近见过的去重键。有界，超出容量时淘汰最旧的。"""

    def __init__(self, maxsize: int = 100_000, ttl: float = 120.0) -> None:
        self._maxsize = max(maxsize, 1)
        self._ttl = max(ttl, 0.0)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ #
    def _evict_expired(self, moment: float) -> None:
        if self._ttl <= 0:
            return
        # OrderedDict 按插入顺序排列，过期项必然聚集在头部
        while self._seen:
            key, stamp = next(iter(self._seen.items()))
            if moment - stamp <= self._ttl:
                break
            self._seen.pop(key, None)

    def check_and_add(self, key: str, moment: float | None = None) -> bool:
        """返回 True 表示**重复**（调用方应丢弃）。"""
        stamp_now = time.monotonic() if moment is None else moment
        self._evict_expired(stamp_now)

        if key in self._seen:
            self._seen.move_to_end(key)
            self._seen[key] = stamp_now
            self.hits += 1
            return True

        self._seen[key] = stamp_now
        self.misses += 1
        while len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False

    def prune(self, moment: float | None = None) -> int:
        """主动清理过期项，返回清理数量。长时间运行时定期调用。"""
        stamp_now = time.monotonic() if moment is None else moment
        before = len(self._seen)
        self._evict_expired(stamp_now)
        return before - len(self._seen)

    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._seen)

    @property
    def stats(self) -> dict[str, int]:
        return {"size": len(self._seen), "hits": self.hits, "misses": self.misses}
