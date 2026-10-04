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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


@dataclass
class StatsTable:
    """一张统计表：标题 + 列头 + 数据行。

    为什么要有这个中间结构：CSV 与 xlsx 两种输出**必须用同一份表定义** ——
    分开写两份的话，加一列就会漏掉另一边（这个项目已经在别处吃过"两处各写一份"的亏）。
    """

    title: str
    headers: list[str]
    rows: list[list[Any]]


def build_stats_tables(payload: dict[str, Any]) -> list[StatsTable]:
    """把统计 payload 整理成若干张表（**纯函数，不涉及任何输出格式**）。

    `payload` 的字段（都由调用方从 store/hub 汇总好，本函数不认识数据库）：

    * `exported_at` / `since` / `until` / `bucket_s` / `room_id` / `anchor_name`
    * `total` / `by_type`（`{type: count}`）/ `top_senders`（`[{uid, uname, count}]`）
    * `buckets`（`[{ts, count, by_type?}]`）

    缺字段一律当"没有"，**不抛异常**：导出一份缺了几节的报告，
    也远比整个导出失败要好。
    """
    by_type: dict[str, int] = dict(payload.get("by_type") or {})
    buckets: list[dict[str, Any]] = list(payload.get("buckets") or [])
    senders: list[dict[str, Any]] = list(payload.get("top_senders") or [])
    total = payload.get("total")
    if total is None:
        total = sum(int(v or 0) for v in by_type.values())

    room_id = payload.get("room_id")
    tables: list[StatsTable] = [
        StatsTable(
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
        ),
        StatsTable(
            TABLE_BY_TYPE,
            ["类型", "条数", "占比"],
            [
                [name, count, f"{(count / total * 100):.1f}%" if total else "0.0%"]
                for name, count in sorted(by_type.items(), key=lambda kv: -kv[1])
            ],
        ),
    ]

    # 每时段弹幕量（需求的主角）。类型列取所有桶里出现过的类型并集，
    # 保证每行列数一致（CSV 最怕列数参差，Excel 也读不成一张规整的表）。
    bucket_s = payload.get("bucket_s")
    type_columns: list[str] = []
    for bucket in buckets:
        for name in (bucket.get("by_type") or {}):
            if name not in type_columns:
                type_columns.append(name)

    tables.append(
        StatsTable(
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
    )

    tables.append(
        StatsTable(
            TABLE_SENDERS,
            ["排名", "昵称", "uid", "条数"],
            [
                [
                    index + 1,
                    item.get("uname") or "",
                    item.get("uid") or "",
                    int(item.get("count") or 0),
                ]
                for index, item in enumerate(senders)
            ],
        )
    )
    return tables


def build_stats_csv(payload: dict[str, Any]) -> str:
    """拼成 CSV 文本：多张表用 `# 表名` 分节 + 空行隔开。

    ⚠️ **CSV 没有工作表的概念**，所以"多张表"只能是竖向堆叠。
    想按表分开看/做透视，用 `build_stats_xlsx()`（真正多 sheet）。
    两者共用 `build_stats_tables()`，列不会漂移。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    for table in build_stats_tables(payload):
        writer.writerow([f"# {table.title}"])
        writer.writerow(table.headers)
        for row in table.rows:
            writer.writerow(["" if item is None else item for item in row])
        writer.writerow([])
    return buffer.getvalue()


#: xlsx 的 sheet 名有 31 字符上限，且不能含 `[]:*?/\`。
SHEET_NAME_LIMIT = 31


def sheet_name(title: str) -> str:
    """把表标题转成合法的 Excel sheet 名。

    去掉了 `#` 与非法字符；`TABLE_*` 常量里的 `·` 是合法字符，保留可读性。
    """
    cleaned = str(title).replace("#", "").strip()
    for char in "[]:*?/\\":
        cleaned = cleaned.replace(char, "")
    return cleaned[:SHEET_NAME_LIMIT] or "Sheet"


def build_stats_xlsx(payload: dict[str, Any]) -> bytes | None:
    """拼成**多 sheet xlsx** 字节；没有 openpyxl 时返回 None。

    为什么值得单独做：CSV 的"多张表"只是竖向堆叠的文字，
    在 Excel 里没法按 sheet 单独看、也没法直接做数据透视 ——
    而"每时段弹幕量"恰恰是最想做透视的那张表。

    **不引入硬依赖**：openpyxl 不在时返回 None，调用方回退 CSV。
    导出这个功能不能因为缺一个可选依赖就整个不可用。
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        return None

    workbook = Workbook()
    # `Workbook()` 自带一个空表：第一张表复用它，其余用 create_sheet，
    # 免得最后还要删掉那个多余的 "Sheet"。
    workbook.remove(workbook.active)

    bold = Font(bold=True)
    for table in build_stats_tables(payload):
        sheet = workbook.create_sheet(title=sheet_name(table.title))
        sheet.append(list(table.headers))
        for cell in sheet[1]:
            cell.font = bold  # 列头加粗：表格里"哪一行是表头"应该一眼看出来
        for row in table.rows:
            sheet.append(["" if item is None else item for item in row])
        # 冻结首行 + 按内容估个列宽：数字列（条数/uid）不会被挤成 `####`
        sheet.freeze_panes = "A2"
        for index, header in enumerate(table.headers, start=1):
            longest = len(str(header))
            for row in table.rows[:200]:  # 只看前 200 行估宽，几万行不必全量扫
                if index <= len(row):
                    longest = max(longest, len(str(row[index - 1])))
            sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(
                max(longest + 2, 8), 42
            )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
