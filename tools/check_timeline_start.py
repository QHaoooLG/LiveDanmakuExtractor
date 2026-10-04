"""验证需求 3：时间轴只从**本次启动采集时刻**开始统计。

`/api/density` 的 `range.lo` 原来取库里该房间的**全部历史**（`time_range()`），
于是一次 10 分钟的采集、时间轴却横跨好几天，每格被压成看不见的细线。

修法是前端把 `hub.started_at` 当 `since` 传下来。本脚本直接对比"不传 since"
与"传 since"两种情况下的 `range` 与格数。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku.launcher import LauncherOptions, PanelController  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


controller = PanelController(LauncherOptions(port=0, out_dir="data"))
try:
    status = controller.start(wait=True, timeout=25)
    if not status.running:
        raise SystemExit(f"面板起不来：{status.error}")
    base = f"http://127.0.0.1:{status.port}"

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    print("=" * 84)
    print("1. 拿本次启动时刻（hub.started_at）")
    print("=" * 84)
    stats = get("/api/stats?window=0")
    started = stats.get("cumulative", {}).get("started_at")
    print(f"  mode={stats.get('mode')}  started_at={started}")
    check("能拿到 started_at", bool(started))
    if not started:
        raise SystemExit(1)

    print()
    print("=" * 84)
    print("2. 不传 since（旧行为）")
    print("=" * 84)
    old = get("/api/density?bucket_s=60")
    old_lo, old_hi = (old.get("range") or {}).get("lo"), (old.get("range") or {}).get("hi")
    print(f"  range.lo = {old_lo}  hi = {old_hi}")
    print(f"  格数 = {len(old.get('buckets') or [])}  total = {old.get('total')}")
    old_span_h = ((old_hi or 0) - (old_lo or 0)) / 3600 if old_lo else 0
    print(f"  跨度 = {old_span_h:.1f} 小时")

    print()
    print("=" * 84)
    print("3. 传 since=started_at（新行为）")
    print("=" * 84)
    new = get(f"/api/density?bucket_s=60&since={started}")
    new_lo, new_hi = (new.get("range") or {}).get("lo"), (new.get("range") or {}).get("hi")
    print(f"  range.lo = {new_lo}  hi = {new_hi}")
    print(f"  格数 = {len(new.get('buckets') or [])}  total = {new.get('total')}")
    new_span_h = ((new_hi or 0) - (new_lo or 0)) / 3600 if new_lo else 0
    print(f"  跨度 = {new_span_h:.2f} 小时")

    print()
    print("=" * 84)
    print("4. 判据")
    print("=" * 84)
    check("传 since 后 range.lo 不早于 started_at",
          new_lo is None or float(new_lo) >= float(started) - 1,
          f"lo={new_lo} vs started={started}")
    if old_lo is not None and new_lo is not None:
        check("时间轴跨度明显收窄（不再横跨全部历史）",
              new_span_h <= old_span_h + 0.01,
              f"{old_span_h:.1f}h → {new_span_h:.2f}h")
    else:
        print("  ℹ️ 本次库内没有该范围内的数据，跨度对比不适用")
finally:
    controller.stop()

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ 时间轴可按「本次启动时刻」收窄")
print("=" * 84)
