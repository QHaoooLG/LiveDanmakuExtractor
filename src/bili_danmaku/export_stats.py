"""把统计结果拼成**一个 CSV 文件、多张表**（需求：导出统计 CSV）。

## 为什么是"多张表挤在一个 CSV 里"

需求方明确要求"每时段弹幕量统计和弹幕统计合在同一 csv 文件，单独新建表即可"。
CSV 本身没有工作表的概念，所以这里的做法是：

* 每张表前面一行 `# 表名`；
* 接着一行列头；
* 再接着数据行；
* 表与表之间留一个空行。

这样**人直接看**是一份分节报告，**Excel 打开**也能正常分行读（不会因为表头不同而错位，
因为错位本来就是"多表同文件"的固有限制，用分节标题把它显式化反而更清楚）。

## 为什么单独成模块

这一层是**纯函数**（没有 IO、没有数据库、没有时间），所以能完全离线单测 ——
导出格式一旦错位，用户拿到的是"看着像坏了"的文件，比崩溃更难发现。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Iterable

#: 表名（用户直接看到的一行，用中文）
TABLE_SUMMARY = "弹幕统计 · 概要"
TABLE_BY_TYPE = "弹幕统计 · 类型分布"
TABLE_BUCKETS = "每时段弹幕量统计"
TABLE_SENDERS = "弹幕统计 · 发言 TOP"

#: 每时段弹幕量的默认格宽（秒）—— 需求："若没有选择默认按照 5min 格式导出"
DEFAULT_BUCKET_S = 300.0


def _stamp(ts: float | None) -> str:
    """时间戳 → `YYYY-MM-DD HH:MM:SS`（本地时区）。

    导出是给**人**看的，所以要本地时间而不是 epoch；
    写成 ISO 风格是为了 Excel 能识别成时间（`2026/10/2 0:16` 这种本地化格式反而难解析）。
    """
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _bucket_label(seconds: float | None) -> str:
    """把格宽写成人类可读的文本（`5 分钟` / `90 秒`）。"""
    if not seconds:
        return ""
    value = float(seconds)
    if value >= 60 and value % 60 == 0:
        return f"{int(value // 60)} 分钟"
    return f"{value:g} 秒"


def _write_table(
    writer: Any,
    title: str,
    headers: list[str],
    rows: Iterable[Iterable[Any]],
) -> None:
    """写一张表：`# 表名` + 列头 + 数据；表和表之间空一行。"""
    writer.writerow([f"# {title}"])
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if item is None else item for item in row])
    writer.writerow([])


def build_stats_csv(payload: dict[str, Any]) -> str:
    """把统计 payload 拼成 CSV 文本。

    `payload` 的字段（都由调用方从 store/hub 汇总好，本函数不认识数据库）：

    * `exported_at` / `since` / `until` / `bucket_s` / `room_id` / `anchor_name`
    * `total` / `by_type`（`{type: count}`）/ `top_senders`（`[{uid, uname, count}]`）
    * `buckets`（`[{ts, count, by_type?}]`）

    缺字段一律当"没有"，**不抛异常**：导出一份缺了几节的报告，
    也远比整个导出失败要好。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    by_type: dict[str, int] = dict(payload.get("by_type") or {})
    buckets: list[dict[str, Any]] = list(payload.get("buckets") or [])
    senders: list[dict[str, Any]] = list(payload.get("top_senders") or [])
    total = payload.get("total")
    if total is None:
        total = sum(int(v or 0) for v in by_type.values())

    # ---- 表 1：概要 ---------------------------------------------------- #
    room_id = payload.get("room_id")
    _write_table(
        writer,
        TABLE_SUMMARY,
        ["项目", "值"],
        [
            ["导出时间", _stamp(payload.get("exported_at"))],
            ["主播", payload.get("anchor_name") or ""],
            ["房间号", room_id if room_id is not None else "（全部房间）"],
            ["统计起点", _stamp(payload.get("since"))],
            ["统计终点", _stamp(payload.get("until"))],
            ["时段长度", _bucket_label(payload.get("bucket_s"))],
            ["弹幕总数", total],
            ["有弹幕的时段数", len(buckets)],
            ["时段峰值", max((int(b.get("count") or 0) for b in buckets), default=0)],
            ["参与发言人数", len(senders)],
        ],
    )

    # ---- 表 2：类型分布 ------------------------------------------------ #
    _write_table(
        writer,
        TABLE_BY_TYPE,
        ["类型", "条数", "占比"],
        [
            [name, count, f"{(count / total * 100):.1f}%" if total else "0.0%"]
            for name, count in sorted(by_type.items(), key=lambda kv: -kv[1])
        ],
    )

    # ---- 表 3：每时段弹幕量（需求 1 的主角） ---------------------------- #
    bucket_s = payload.get("bucket_s")
    # 类型列取所有桶里出现过的类型并集，保证每行列数一致（CSV 最怕列数参差）
    type_columns: list[str] = []
    for bucket in buckets:
        for name in (bucket.get("by_type") or {}):
            if name not in type_columns:
                type_columns.append(name)

    _write_table(
        writer,
        TABLE_BUCKETS,
        ["时段起点", "时段终点", "条数", *type_columns],
        [
            [
                _stamp(bucket.get("ts")),
                _stamp(
                    (float(bucket.get("ts")) + float(bucket_s))
                    if bucket.get("ts") is not None and bucket_s
                    else None
                ),
                int(bucket.get("count") or 0),
                *[
                    int((bucket.get("by_type") or {}).get(name, 0))
                    for name in type_columns
                ],
            ]
            for bucket in buckets
        ],
    )

    # ---- 表 4：发言 TOP ------------------------------------------------ #
    _write_table(
        writer,
        TABLE_SENDERS,
        ["排名", "昵称", "uid", "条数"],
        [
            [index + 1, item.get("uname") or "", item.get("uid") or "", int(item.get("count") or 0)]
            for index, item in enumerate(senders)
        ],
    )

    return buffer.getvalue()
