"""验证真实 `data/danmaku.db` 在下次开库时**自动补上新索引**，并实测提速。

不动原库内容：只调用 `SqliteSink.open()`（它会 `executescript(SCHEMA)`），
然后跑一次发言榜查询对比。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DB = ROOT / "data" / "danmaku.db"
QUERY = (
    "SELECT uid, uname, COUNT(*) AS c FROM event "
    "WHERE uid IS NOT NULL GROUP BY uid ORDER BY c DESC LIMIT 10"
)


def indexes() -> set[str]:
    con = sqlite3.connect(DB)
    try:
        return {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='event'"
            )
        }
    finally:
        con.close()


def timed() -> tuple[float, str]:
    con = sqlite3.connect(DB)
    try:
        con.execute(QUERY).fetchall()  # 预热
        best = min(
            (lambda t0=time.perf_counter(): (con.execute(QUERY).fetchall(), time.perf_counter() - t0)[1])()
            for _ in range(5)
        )
        plan = " ".join(str(r[3]) for r in con.execute(f"EXPLAIN QUERY PLAN {QUERY}"))
        return best * 1000, plan
    finally:
        con.close()


print("=" * 84)
print("开库前")
print("=" * 84)
before = indexes()
print(f"  有 idx_event_uid_uname = {'idx_event_uid_uname' in before}")
ms, plan = timed()
print(f"  全库发言榜 = {ms:.1f} ms")
print(f"  查询计划   = {plan[:90]}")

print()
print("=" * 84)
print("用 SqliteSink 开一次库（真实采集启动时就会做）")
print("=" * 84)
import sys

sys.path.insert(0, str(ROOT / "src"))
from bili_danmaku.config import Config  # noqa: E402
from bili_danmaku.models import SessionMeta  # noqa: E402
from bili_danmaku.sinks import SqliteSink  # noqa: E402

config = Config.load(None)
sink = SqliteSink(config)
sink.open(SessionMeta(session_id="index-selfheal-probe", room_id=0, started_at=time.time()))
sink.close()
print("  已 open + close")

print()
print("=" * 84)
print("开库后")
print("=" * 84)
after = indexes()
print(f"  有 idx_event_uid_uname = {'idx_event_uid_uname' in after}")
ms2, plan2 = timed()
print(f"  全库发言榜 = {ms2:.1f} ms")
print(f"  查询计划   = {plan2[:90]}")

print()
print("=" * 84)
ok = "idx_event_uid_uname" in after and "COVERING INDEX" in plan2
print(f"{'✅' if ok else '❌'} 索引已自愈 = {'idx_event_uid_uname' in after}；"
      f"提速 {ms / ms2 if ms2 else 0:.1f} 倍")
print("=" * 84)
raise SystemExit(0 if ok else 1)
