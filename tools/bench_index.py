"""基准：新增哪种索引最适合 `top_senders`（全库发言榜）。

对照三种：
  0. 现状：只有 `idx_event_uid(uid)`
  1. `idx_event_uname(uname)`        —— 单列，让 uid→uname 不用回表
  2. `idx_event_uid_uname(uid,uname)` —— 真覆盖索引

用**副本**跑，绝不动 `data/danmaku.db`。
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "danmaku.db"
WORK = ROOT / "build" / "_idxbench"

QUERY = (
    "SELECT uid, uname, COUNT(*) AS c FROM event "
    "WHERE uid IS NOT NULL GROUP BY uid ORDER BY c DESC LIMIT 10"
)
QUERY_ONE_ROOM = (
    "SELECT uid, uname, COUNT(*) AS c FROM event "
    "WHERE uid IS NOT NULL AND room_id = ? GROUP BY uid ORDER BY c DESC LIMIT 10"
)

if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True, exist_ok=True)
db = WORK / "bench.db"
shutil.copy2(SRC, db)

CASES = [
    ("0. 现状（只有 idx_event_uid）", []),
    ("1. + idx_event_uname(uname)", ["CREATE INDEX idx_event_uname ON event(uname)"]),
    ("2. + idx_event_uid_uname(uid,uname)", ["CREATE INDEX idx_event_uid_uname ON event(uid, uname)"]),
]

print(f"库大小 = {db.stat().st_size / 1024 / 1024:.1f} MB")
print()
print(f"{'方案':<38}{'全库(ms)':>10}{'单房间(ms)':>12}{'库增量(MB)':>12}")
print("-" * 72)

base_size = db.stat().st_size
for label, statements in CASES:
    con = sqlite3.connect(db)
    try:
        for sql in statements:
            con.execute(sql)
        con.commit()
        # 预热
        con.execute(QUERY).fetchall()
        con.execute(QUERY_ONE_ROOM, (8432038,)).fetchall()

        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            con.execute(QUERY).fetchall()
            times.append((time.perf_counter() - t0) * 1000)
        all_ms = min(times)

        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            con.execute(QUERY_ONE_ROOM, (8432038,)).fetchall()
            times.append((time.perf_counter() - t0) * 1000)
        room_ms = min(times)

        plan = con.execute(f"EXPLAIN QUERY PLAN {QUERY}").fetchall()
        growth = (db.stat().st_size - base_size) / 1024 / 1024
        print(f"{label:<38}{all_ms:>10.1f}{room_ms:>12.1f}{growth:>12.1f}")
        print(f"    计划: {plan[0][3][:88]}")
    finally:
        con.close()
        # 让下一个方案回到"只有现状索引"的起点
        for _label, stmts in CASES[1:]:
            for sql in stmts:
                name = sql.split()[2]
                c2 = sqlite3.connect(db)
                try:
                    c2.execute(f"DROP INDEX IF EXISTS {name}")
                    c2.commit()
                finally:
                    c2.close()

shutil.rmtree(WORK, ignore_errors=True)
print()
print("（已清理临时副本，data/danmaku.db 未被改动）")
