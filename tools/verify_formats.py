"""产物一致性校验：确认四种落盘格式的内容互相吻合。

用途：验收与回归。读取 `--dir` 下的 jsonl / csv / txt / sqlite，
比对条数、弹幕条数、以及 JSONL 与 SQLite 的 msg_id 集合是否一致。

用法: python tools/verify_formats.py --dir data
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="校验四种落盘格式的一致性")
    parser.add_argument("--dir", default="data", help="采集输出目录")
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"[FAIL] 目录不存在: {directory}")
        return 1

    jsonl_files = sorted(directory.glob("*.jsonl"))
    csv_files = sorted(directory.glob("*.csv"))
    txt_files = sorted(directory.glob("*.txt"))
    db_files = sorted(directory.glob("*.db"))

    print(f"jsonl={len(jsonl_files)} csv={len(csv_files)} txt={len(txt_files)} db={len(db_files)}")

    records = []
    for path in jsonl_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))

    csv_rows = []
    for path in csv_files:
        reader = csv.reader(path.read_text(encoding="utf-8-sig").splitlines())
        rows = list(reader)
        csv_rows.extend(rows[1:])  # 去掉表头

    txt_lines = []
    for path in txt_files:
        txt_lines.extend(line for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    db_total = db_danmaku = 0
    db_msg_ids: set[str] = set()
    if db_files:
        connection = sqlite3.connect(db_files[0])
        try:
            db_total = connection.execute("SELECT COUNT(1) FROM event").fetchone()[0]
            db_danmaku = connection.execute(
                "SELECT COUNT(1) FROM event WHERE type = 'danmaku'"
            ).fetchone()[0]
            db_msg_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT msg_id FROM event WHERE msg_id IS NOT NULL"
                )
            }
        finally:
            connection.close()

    jsonl_danmaku = sum(1 for record in records if record.get("type") == "danmaku")
    jsonl_msg_ids = {record["msg_id"] for record in records if record.get("msg_id")}

    print(f"\nJSONL   {len(records):6d} 条（弹幕 {jsonl_danmaku}）" if jsonl_files else "\nJSONL   (未落盘)")
    print(f"CSV     {len(csv_rows):6d} 条" if csv_files else "CSV     (未落盘)")
    print(f"SQLite  {db_total:6d} 条（弹幕 {db_danmaku}）" if db_files else "SQLite  (未落盘)")
    print(f"TXT     {len(txt_lines):6d} 条（仅弹幕）" if txt_files else "TXT     (未落盘)")

    # 只比对**实际落盘**的格式 —— 只请求 jsonl+sqlite 时不该拿 csv 的 0 去比
    total_counts: dict[str, int] = {}
    danmaku_counts: dict[str, int] = {}
    if jsonl_files:
        total_counts["JSONL"] = len(records)
        danmaku_counts["JSONL"] = jsonl_danmaku
    if csv_files:
        total_counts["CSV"] = len(csv_rows)
    if db_files:
        total_counts["SQLite"] = db_total
        danmaku_counts["SQLite"] = db_danmaku
    if txt_files:
        danmaku_counts["TXT"] = len(txt_lines)

    checks: list[tuple[str, bool, str]] = []
    if len(total_counts) >= 2:
        detail = " / ".join(f"{name} {count}" for name, count in total_counts.items())
        checks.append(("已落盘格式的总数一致", len(set(total_counts.values())) == 1, detail))
    if len(danmaku_counts) >= 2:
        detail = " / ".join(f"{name} {count}" for name, count in danmaku_counts.items())
        checks.append(("各格式弹幕条数一致", len(set(danmaku_counts.values())) == 1, detail))
    if jsonl_files and db_files:
        checks.append(
            (
                "JSONL 与 SQLite 的 msg_id 集合一致",
                jsonl_msg_ids == db_msg_ids,
                f"{len(jsonl_msg_ids)} / {len(db_msg_ids)}",
            )
        )
    if jsonl_files:
        checks.append(
            (
                "JSONL 序列号无重复",
                len({record.get("seq") for record in records}) == len(records),
                f"{len(records)} 条",
            )
        )

    if not total_counts:
        print("\n[FAIL] 没有找到任何落盘产物")
        return 1

    print()
    failed = 0
    for name, ok, detail in checks:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}  ({detail})")
        failed += 0 if ok else 1

    print("\n全部一致 ✅" if not failed else f"\n{failed} 项不一致 ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
