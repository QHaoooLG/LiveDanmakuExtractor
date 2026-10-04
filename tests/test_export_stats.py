"""`export_stats.build_stats_csv` 的离线用例。

这一层是纯函数，但**格式错位比崩溃更隐蔽** —— 用户拿到的是"看着像坏了"的文件，
所以列数一致性、分节标题、取整这些都要钉住。
"""

from __future__ import annotations

import csv
import io

import pytest

from bili_danmaku.export_stats import (
    DEFAULT_BUCKET_S,
    TABLE_BUCKETS,
    TABLE_BY_TYPE,
    TABLE_SENDERS,
    TABLE_SUMMARY,
    build_stats_csv,
)


def payload(**overrides):
    base = {
        "exported_at": 1790918400.0,
        "since": 1790910000.0,
        "until": 1790913600.0,
        "bucket_s": 300.0,
        "room_id": 804456,
        "anchor_name": "六式zy",
        "total": 3,
        "by_type": {"danmaku": 2, "super_chat": 1},
        "buckets": [
            {"ts": 1790910000.0, "count": 2, "by_type": {"danmaku": 2}},
            {"ts": 1790910300.0, "count": 1, "by_type": {"super_chat": 1}},
        ],
        "top_senders": [
            {"uid": 1000, "uname": "甲", "count": 2},
            {"uid": 2000, "uname": "乙", "count": 1},
        ],
    }
    base.update(overrides)
    return base


def rows_of(csv_text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text)))


def section_rows(csv_text: str, title: str) -> list[list[str]]:
    """取某张表的数据行（分节标题之后、下一个空行之前，跳过列头）。"""
    rows = rows_of(csv_text)
    start = next(i for i, row in enumerate(rows) if row and row[0] == f"# {title}")
    out: list[list[str]] = []
    for row in rows[start + 2:]:  # +2 跳过列头
        if not row:
            break
        out.append(row)
    return out


def test_contains_all_four_tables() -> None:
    """需求：弹幕统计 + 每时段弹幕量合在同一 CSV，单独新建表。"""
    text = build_stats_csv(payload())
    for title in (TABLE_SUMMARY, TABLE_BY_TYPE, TABLE_BUCKETS, TABLE_SENDERS):
        assert f"# {title}" in text, f"缺少表：{title}"


def test_bucket_table_has_bounds_and_counts() -> None:
    """每时段表要有起止时间与条数（这是需求 1 的主角）。

    期望值用 `datetime.fromtimestamp` **现算**而不是写死字符串：
    时间戳转本地时间依赖运行机器的时区，写死会让用例在别的时区直接失败。
    """
    from datetime import datetime

    text = build_stats_csv(payload())
    rows = section_rows(text, TABLE_BUCKETS)
    assert len(rows) == 2
    first = rows[0]
    start = datetime.fromtimestamp(1790910000.0).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(1790910300.0).strftime("%Y-%m-%d %H:%M:%S")
    assert first[0] == start
    assert first[1] == end  # 起点 + bucket_s（5 分钟）
    assert first[2] == "2"  # 条数
    # 类型列是并集：danmaku 与 super_chat 两列都在
    all_rows = rows_of(text)
    header_index = next(
        i for i, r in enumerate(all_rows) if r and r[0] == f"# {TABLE_BUCKETS}"
    )
    header = all_rows[header_index + 1]
    assert "danmaku" in header and "super_chat" in header


def test_all_rows_in_bucket_table_have_same_width() -> None:
    """列数必须一致 —— 参差不齐的 CSV 会被 Excel 读错列。"""
    text = build_stats_csv(payload())
    rows = rows_of(text)
    start = next(i for i, r in enumerate(rows) if r and r[0] == f"# {TABLE_BUCKETS}")
    widths = set()
    for row in rows[start + 1:]:
        if not row:
            break
        widths.add(len(row))
    assert len(widths) == 1, f"列数不一致: {widths}"


def test_by_type_percentages_and_ordering() -> None:
    """类型分布按条数降序，并给出占比。"""
    text = build_stats_csv(payload())
    rows = section_rows(text, TABLE_BY_TYPE)
    assert rows[0][0] == "danmaku"
    assert rows[0][1] == "2"
    assert rows[0][2] == "66.7%"
    assert rows[1][2] == "33.3%"


def test_default_bucket_is_five_minutes() -> None:
    """需求：没选时段时长时按 5 分钟导出。"""
    assert DEFAULT_BUCKET_S == 300.0
    # payload 不带 bucket_s 时，概要行不应报错
    text = build_stats_csv(payload(bucket_s=None, buckets=[]))
    assert "# " + TABLE_SUMMARY in text


def test_empty_payload_does_not_crash() -> None:
    """空库 / 空范围也要产出**结构完整**的 CSV，而不是抛异常。

    导出失败比"导出一份空的"糟糕得多（用户至少能从空表看出真的没数据）。
    """
    text = build_stats_csv({})
    for title in (TABLE_SUMMARY, TABLE_BY_TYPE, TABLE_BUCKETS, TABLE_SENDERS):
        assert f"# {title}" in text
    assert section_rows(text, TABLE_BUCKETS) == []
    assert section_rows(text, TABLE_SENDERS) == []


def test_total_falls_back_to_by_type_sum() -> None:
    """没给 total 时用 by_type 求和，避免概要行空白。"""
    text = build_stats_csv(payload(total=None, by_type={"danmaku": 5, "super_chat": 2}))
    rows = dict(section_rows(text, TABLE_SUMMARY))
    assert rows["弹幕总数"] == "7"


def test_room_id_none_reads_as_all_rooms() -> None:
    text = build_stats_csv(payload(room_id=None, anchor_name=""))
    rows = dict(section_rows(text, TABLE_SUMMARY))
    assert rows["房间号"] == "（全部房间）"


def test_timestamp_out_of_range_is_blank_not_crash() -> None:
    """越界时间戳不能让整份导出失败。"""
    text = build_stats_csv(payload(since=1e18))
    assert "# " + TABLE_SUMMARY in text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(300.0, "5 分钟"), (60.0, "1 分钟"), (90.0, "90 秒"), (None, "")],
)
def test_bucket_label_is_human_readable(seconds, expected) -> None:
    text = build_stats_csv(payload(bucket_s=seconds, buckets=[]))
    rows = dict(section_rows(text, TABLE_SUMMARY))
    assert rows["时段长度"] == expected
