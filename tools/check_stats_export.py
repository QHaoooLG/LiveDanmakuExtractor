"""验证 `/api/stats/export`：同一个 CSV 里有多张表，含每时段弹幕量。

需求：
  * 每时段弹幕量统计与弹幕统计**合在同一 CSV**（单独新建表即可）；
  * 时段长度按 web 端选择，未选择则默认 5 分钟；
  * 文件名由前端拼（见 web/src/preferences.js）。
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
    print(f"面板已起 = {status.running}  port={status.port}")
    if not status.running:
        raise SystemExit(f"起不来：{status.error}")

    def get(path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{status.port}{path}", timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    print()
    print("=" * 84)
    print("1. 默认调用（不给 bucket_s ⇒ 应默认 5 分钟 = 300s）")
    print("=" * 84)
    payload = get("/api/stats/export")
    check("返回 bucket_s = 300（默认 5 分钟）", float(payload.get("bucket_s") or 0) == 300.0,
          f"实际 {payload.get('bucket_s')}")
    check("带 room_id（文件名需要）", payload.get("room_id") is not None,
          f"room_id={payload.get('room_id')} anchor={payload.get('anchor_name')!r}")
    check("带 by_type", bool(payload.get("by_type")), f"{len(payload.get('by_type') or {})} 种类型")
    check("带 buckets", bool(payload.get("buckets")), f"{len(payload.get('buckets') or [])} 个时段")
    check("带 top_senders", bool(payload.get("top_senders")),
          f"{len(payload.get('top_senders') or [])} 人")

    csv_text = payload.get("csv") or ""
    check("返回了 csv 文本", bool(csv_text), f"{len(csv_text)} 字符")

    print()
    print("=" * 84)
    print("2. CSV 里必须有多张表（`# 表名` 分节）")
    print("=" * 84)
    sections = [line for line in csv_text.splitlines() if line.startswith("# ")]
    for name in sections:
        print(f"    {name}")
    check("含「弹幕统计 · 概要」", any("概要" in s for s in sections))
    check("含「弹幕统计 · 类型分布」", any("类型分布" in s for s in sections))
    check("含「每时段弹幕量统计」", any("每时段" in s for s in sections))
    check("含「发言 TOP」", any("发言" in s for s in sections))

    print()
    print("=" * 84)
    print("3. 每时段表的列头与行（列数必须一致）")
    print("=" * 84)
    lines = csv_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("# 每时段"))
    except StopIteration:
        start = -1
    if start >= 0:
        header = lines[start + 1]
        print(f"    列头: {header}")
        check("列头含 时段起点/时段终点/条数", all(
            k in header for k in ("时段起点", "时段终点", "条数")))
        # 取该表的数据行（到下一个空行为止）
        rows = []
        for line in lines[start + 2:]:
            if not line.strip():
                break
            rows.append(line)
        print(f"    {len(rows)} 行数据；首行: {rows[0] if rows else '（无）'}")
        widths = {len(r.split(",")) for r in rows}
        check("数据行列数一致", len(widths) <= 1, f"出现列数: {sorted(widths)}")
        check("行数与 buckets 数一致", len(rows) == len(payload.get("buckets") or []),
              f"{len(rows)} vs {len(payload.get('buckets') or [])}")

    print()
    print("=" * 84)
    print("4. 指定其它粒度（按 web 端选择导出）")
    print("=" * 84)
    for span in (60, 180, 900):
        p = get(f"/api/stats/export?bucket_s={span}")
        got = float(p.get("bucket_s") or 0)
        check(f"bucket_s={span} 被采纳", got == float(span), f"实际 {got}")

    print()
    print("=" * 84)
    print("5. 按房间过滤")
    print("=" * 84)
    room = payload.get("room_id")
    if room:
        p = get(f"/api/stats/export?room_id={room}&bucket_s=300")
        check("指定房间后 room_id 原样保留", int(p.get("room_id") or 0) == int(room))
        types_in = set((p.get("by_type") or {}).keys())
        check("指定房间的数据非空", bool(p.get("by_type")), f"类型 {sorted(types_in)}")
finally:
    controller.stop()

print()
print("=" * 84)
if failures:
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("✅ /api/stats/export 符合需求")
print("=" * 84)
