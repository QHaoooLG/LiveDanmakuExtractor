"""V2-b 单测：Super Chat 汇总（`cli.run_sc` / 终端输出 / 告警行）。

重点钉死三件事：

1. **金额单位**：门槛是「元」，而库里 `price` 的单位可以用 `--price-unit` 声明
   （默认 `yuan`，`milli` 时 ÷1000）—— 因为这一条**还没用真实 SC 样本验证过**（见 HANDOVER），
   必须留好可覆盖的开关；
2. 只统计 `super_chat`（别把礼物/上舰混进 SC 报表）；
3. 汇总数字与明细必须自洽：合计 = 明细之和 = 贡献榜之和。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bili_danmaku.cli import (
    SC_PRICE_UNIT,
    _SC_UNIT_SCALE,
    print_sc_report,
    run_sc,
    sc_alert_line,
)
from bili_danmaku.config import Config
from bili_danmaku.models import Event, SessionMeta
from bili_danmaku.sinks import SqliteSink
from bili_danmaku.store import DanmakuStore

ROOM = 23438368
SESSION = "20260928-120000-23438368"
BASE_TS = 1_790_570_000.0


def sc_event(seq: int, *, uname: str, price: int | None, text: str = "加油", uid: int | None = None) -> Event:
    return Event(
        seq=seq,
        session_id=SESSION,
        room_id=ROOM,
        type="super_chat",
        cmd="SUPER_CHAT_MESSAGE",
        recv_ts=BASE_TS + seq,
        server_ts=int(BASE_TS) + seq,
        msg_id=f"sc-{seq}",
        uid=uid if uid is not None else 1000 + seq,
        uname=uname,
        text=text,
        price=price,
    )


@pytest.fixture
def store(tmp_path: Path):
    config = Config.load(
        path="missing.toml",
        overrides={"sinks": {"out_dir": str(tmp_path), "types": ["sqlite"], "keep_raw": False}},
    )
    sink = SqliteSink(config)
    sink.open(SessionMeta(session_id=SESSION, room_id=ROOM, started_at=1.0))
    sink.write(
        [
            sc_event(1, uname="甲", price=30, text="主播加油", uid=1001),
            sc_event(2, uname="乙", price=50, text="这波漂亮", uid=1002),
            sc_event(3, uname="甲", price=100, text="再充一次", uid=1001),
            # 非 SC 事件：不能出现在 SC 报表里
            Event(
                seq=4, session_id=SESSION, room_id=ROOM, type="danmaku", cmd="DANMU_MSG",
                recv_ts=BASE_TS + 4, msg_id="d-4", uid=1003, uname="丙", text="普通弹幕",
            ),
            Event(
                seq=5, session_id=SESSION, room_id=ROOM, type="gift", cmd="COMBO_SEND",
                recv_ts=BASE_TS + 5, msg_id="g-5", uid=1004, uname="丁", text="辣条",
                price=100_000,  # 金瓜子：不该被当成 SC 的 10 万元
            ),
        ]
    )
    sink.close()

    opened = DanmakuStore(tmp_path / "danmaku.db")
    try:
        yield opened
    finally:
        opened.close()


def report(store: DanmakuStore, **kwargs) -> dict:
    base = {
        "config": None,
        "store": store,
        "room_id": ROOM,
        "session_id": None,
        "since": None,
        "until": None,
        "min_price": None,
        "limit": None,
        "top": 10,
        "as_json": False,
    }
    base.update(kwargs)
    return run_sc(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 基础汇总
# --------------------------------------------------------------------------- #


def test_only_super_chat_is_counted(store: DanmakuStore) -> None:
    data = report(store)
    assert data["count"] == 3, "礼物与弹幕不能混进 SC 报表"
    assert data["total_price"] == 180
    assert {event["text"] for event in data["items"]} == {"主播加油", "这波漂亮", "再充一次"}


def test_summary_is_internally_consistent(store: DanmakuStore) -> None:
    data = report(store)
    items_sum = sum(item["price"] for item in data["items"])
    senders_sum = sum(item["total_price"] for item in data["top_senders"])
    assert data["total_price"] == items_sum == senders_sum == 180
    assert data["max_price"] == 100
    assert data["avg_price"] == 60.0
    assert data["count"] == len(data["items"])
    assert data["first_ts"] < data["last_ts"]


def test_items_are_sorted_by_time(store: DanmakuStore) -> None:
    data = report(store)
    stamps = [item["recv_ts"] for item in data["items"]]
    assert stamps == sorted(stamps)
    assert [item["text"] for item in data["items"]] == ["主播加油", "这波漂亮", "再充一次"]


def test_top_senders_aggregate_by_uid(store: DanmakuStore) -> None:
    data = report(store)
    top = data["top_senders"]
    assert [item["uname"] for item in top] == ["甲", "乙"], "按金额降序"
    assert top[0]["total_price"] == 130 and top[0]["count"] == 2
    assert top[1]["total_price"] == 50 and top[1]["count"] == 1


def test_top_limit_is_respected(store: DanmakuStore) -> None:
    data = report(store, top=1)
    assert len(data["top_senders"]) == 1
    assert data["top_senders"][0]["uname"] == "甲"
    assert data["count"] == 3, "top 只影响贡献榜，不影响总数"


def test_scope_filters(store: DanmakuStore) -> None:
    assert report(store, room_id=ROOM)["count"] == 3
    assert report(store, room_id=999)["count"] == 0
    assert report(store, session_id=SESSION)["count"] == 3
    assert report(store, session_id="不存在")["count"] == 0


def test_time_window_filter(store: DanmakuStore) -> None:
    data = report(store, since=BASE_TS + 1.5, until=BASE_TS + 3.5)
    assert [item["text"] for item in data["items"]] == ["这波漂亮", "再充一次"]


def test_limit_keeps_newest(store: DanmakuStore) -> None:
    data = report(store, limit=2)
    assert [item["text"] for item in data["items"]] == ["这波漂亮", "再充一次"]
    assert data["total_price"] == 150, "合计只算留下的两条"


def test_empty_scope(store: DanmakuStore) -> None:
    data = report(store, room_id=999)
    assert data["count"] == 0
    assert data["total_price"] == 0
    assert data["max_price"] == 0
    assert data["avg_price"] == 0.0
    assert data["first_ts"] is None
    assert data["last_ts"] is None
    assert data["top_senders"] == []
    assert data["items"] == []


# --------------------------------------------------------------------------- #
# 金额单位（关键：尚未用真实样本验证）
# --------------------------------------------------------------------------- #


def test_min_price_filters_in_yuan(store: DanmakuStore) -> None:
    data = report(store, min_price=50)
    assert data["count"] == 2
    assert data["total_price"] == 150
    assert {item["price"] for item in data["items"]} == {50, 100}

    assert report(store, min_price=101)["count"] == 0
    assert report(store, min_price=30)["count"] == 3, "门槛是闭区间"


def test_price_unit_milli_divides_by_1000(store: DanmakuStore) -> None:
    """库里若是毫元（`--price-unit milli`），金额要 ÷1000 才是元。"""
    assert _SC_UNIT_SCALE == {"yuan": 1, "milli": 1000}

    data = report(store, price_unit="milli")
    assert data["price_unit"] == "milli"
    assert data["total_price"] == 0.18
    assert data["max_price"] == 0.1
    assert [item["price"] for item in data["items"]] == [0.03, 0.05, 0.1]
    assert [item["raw_price"] for item in data["items"]] == [30, 50, 100], "原始值要保留可追溯"


def test_price_unit_milli_affects_threshold(store: DanmakuStore) -> None:
    """毫元模式下 50 元门槛 = 原始 50000。"""
    assert report(store, price_unit="milli", min_price=50)["count"] == 0
    assert report(store, price_unit="milli", min_price=1)["count"] == 0
    assert report(store, price_unit="milli", min_price=0)["count"] == 3


def test_unknown_price_unit_is_rejected(store: DanmakuStore) -> None:
    with pytest.raises(ValueError, match="price-unit"):
        report(store, price_unit="分")


def test_price_unit_label(store: DanmakuStore) -> None:
    data = report(store)
    assert data["unit"] == SC_PRICE_UNIT == "元"


# --------------------------------------------------------------------------- #
# 终端输出
# --------------------------------------------------------------------------- #


def test_print_report_covers_summary_and_items(store: DanmakuStore, capsys: pytest.CaptureFixture) -> None:
    from click.testing import CliRunner

    from bili_danmaku.cli import main

    # 直接调 print_sc_report 需要 click 上下文；用 CliRunner 跑命令更贴近真实
    db = store.path
    result = CliRunner().invoke(main, ["sc", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Super Chat 汇总" in result.output
    assert "合计        180 元" in result.output
    assert "贡献 TOP" in result.output
    assert "甲" in result.output and "130 元" in result.output
    assert "主播加油" in result.output


def test_print_report_when_empty(store: DanmakuStore) -> None:
    from click.testing import CliRunner

    from bili_danmaku.cli import main

    result = CliRunner().invoke(main, ["sc", "--db", str(store.path), "--room", "999"])
    assert result.exit_code == 0
    assert "没有 SC" in result.output


def test_sc_alert_line_format() -> None:
    line = sc_alert_line(sc_event(1, uname="甲", price=30, text="主播加油"))
    assert "SC 30 元" in line
    assert "甲" in line
    assert "主播加油" in line
    assert line.count("\n") == 0, "告警必须是单行（终端里一行一条）"


def test_sc_alert_line_truncates_long_text() -> None:
    line = sc_alert_line(sc_event(1, uname="甲", price=30, text="喂" * 200))
    assert len(line) < 120
    assert line.endswith("…")


def test_sc_alert_line_handles_missing_fields() -> None:
    line = sc_alert_line(sc_event(1, uname=None, price=None, text=None))  # type: ignore[arg-type]
    assert "匿名" in line and "SC 0 元" in line


def test_report_is_json_serialisable(store: DanmakuStore) -> None:
    payload = json.dumps(report(store), ensure_ascii=False, default=str)
    assert json.loads(payload)["count"] == 3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (30, "30"),
        (30.0, "30"),
        (0.03, "0.03"),
        (0.1, "0.1"),
        (180.0, "180"),
        (12.345, "12.35"),
        (None, "0"),
    ],
)
def test_format_amount(value: float | int | None, expected: str) -> None:
    """整数不留 `.0`，小数最多两位 —— 毫元模式下全是小数，普通模式全是整数。"""
    from bili_danmaku.cli import format_amount

    assert format_amount(value) == expected
