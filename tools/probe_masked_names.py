"""核实需求 2：弹幕区为什么仍会看到 `昵称***`。

关键问题：那些 `***` 是**本程序打的**，还是**B 站平台下发的**？
如果是平台下发的，那本地无论怎么改都变不出真名 —— 这一条必须先说清楚。

判据：
  * 库里 `uname` 含 `***` 的记录有多少；
  * 这些记录的 `uid` 是多少（**uid=0 = 平台打码**；有真实 uid 才有可能查到名字）；
  * `general.anonymize` 是否开着（那是本程序自己的哈希脱敏，与 `***` 不是一回事）。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.config import Config  # noqa: E402
from bili_danmaku.store import DanmakuStore  # noqa: E402

DB = ROOT / "data" / "danmaku.db"

print("=" * 84)
print("1. 本程序的脱敏开关")
print("=" * 84)
config = Config.load(None)
print(f"  general.anonymize = {config.general.anonymize}  （True 才会做哈希脱敏）")
print("  注意：哈希脱敏把昵称换成 `u_xxxx` 形式，**不会**产生 `***`")

print()
print("=" * 84)
print("2. 库里 `uname` 含 *** 的记录")
print("=" * 84)
store = DanmakuStore(DB)
try:
    rows = list(store.iter_events(limit=200000))
finally:
    store.close()

print(f"  扫描记录数 = {len(rows)}")

masked = [r for r in rows if r.uname and "***" in r.uname]
uid0 = [r for r in masked if not r.uid]
named = [r for r in masked if r.uid]
print(f"  含 *** 的   = {len(masked)}")
print(f"    其中 uid=0（平台打码，无身份）  = {len(uid0)}")
print(f"    其中 uid>0（有身份但名字被打码）= {len(named)}")

print()
print("  含 *** 的昵称样本（前 20 个不同值）：")
for name, count in Counter(r.uname for r in masked).most_common(20):
    print(f"    {name!r:<28} x{count}")

print()
print("=" * 84)
print("3. 有 uid 但名字被打码的记录（这些**理论上有机会**查到真名）")
print("=" * 84)
by_uid = Counter(r.uid for r in named)
print(f"  不同 uid 数 = {len(by_uid)}")
for uid, count in by_uid.most_common(10):
    sample = next(r for r in named if r.uid == uid)
    print(f"    uid={uid:<12} x{count:<4} uname={sample.uname!r}")

print()
print("=" * 84)
print("4. 正常（未打码）昵称的对照")
print("=" * 84)
normal = [r for r in rows if r.uname and "***" not in r.uname and not r.uname.startswith("u_")]
print(f"  正常昵称记录数 = {len(normal)}")
for name, count in Counter(r.uname for r in normal).most_common(8):
    print(f"    {name!r:<28} x{count}")

print()
print("=" * 84)
print("5. 结论")
print("=" * 84)
if masked and not named:
    print("  ⇒ `***` 全部来自 **B 站平台**，且连 uid 都没有 ⇒ **本地无法还原**")
elif masked:
    print("  ⇒ 有 uid 的那部分**理论上**可通过 user_info 接口查到昵称（但有风控与限频）")
else:
    print("  ⇒ 当前库里没有 `***` 昵称（可能是登录态采集，昵称本来就是全的）")
