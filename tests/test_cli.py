"""M4 覆盖率：CLI 端到端（`cli.py`）。

`cli.py` 是最大的模块（582 条语句）也是覆盖率最低的（26%）——它是用户真正的入口，
不该只靠"手工敲一遍"来验证。这里用 click 的 `CliRunner` 在进程内跑真实命令：

* 离线分析类命令（`stats` / `export` / `replay`）是 M2 的交付物，必须逐条回归；
* 参数解析（`parse_duration` / `resolve_deadline` / `parse_time`）是纯函数，边界要钉死；
* `grab` / `web` 这类要联网的命令只覆盖"参数错误即退出"与打桩后的收尾路径。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from bili_danmaku.cli import (
    _default_db_path,
    _parse_time,
    _resolve_deadline,
    _write_records,
    main,
    parse_duration,
)
from bili_danmaku.config import Config
from bili_danmaku.models import Event
from bili_danmaku.sinks import CSV_COLUMNS

CONFIG_TEMPLATE = """
[general]
log_level = "WARNING"
content_only = true

[api]
cookie_file = "{cookie_file}"
min_request_interval = 0.0

[client]
heartbeat_interval = 0.3
heartbeat_timeout = 1.0
recv_timeout = 0.2
reconnect_max_delay = 0.3

[sinks]
types = ["jsonl", "csv", "sqlite"]
out_dir = "{out_dir}"
keep_raw = false
batch_size = 5
flush_interval = 0.02
queue_size = 100
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def write_config(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG_TEMPLATE.format(
            out_dir=str(overrides.get("out_dir", tmp_path / "data")).replace("\\", "/"),
            cookie_file=str(overrides.get("cookie_file", tmp_path / ".secrets" / "cookies.json")).replace("\\", "/"),
        ),
        encoding="utf-8",
    )
    return path


def make_event(seq: int, *, text: str = "你好", type_: str = "danmaku", uname: str = "甲", **extra) -> Event:
    data = {
        "seq": seq,
        "session_id": "20260918-141801-804456",
        "room_id": 804456,
        "type": type_,
        "cmd": "DANMU_MSG",
        "recv_ts": 1_789_710_200.0 + seq,
        "server_ts": 1_789_710_200,
        "latency_ms": 900 + seq,
        "msg_id": f"m{seq}",
        "uid": 1000 + seq,
        "uname": uname,
        "text": text,
        "source": "live",
    }
    data.update(extra)
    return Event(**data)


def seed_db(tmp_path: Path, events: list[Event], out_dir: Path | None = None) -> Path:
    """把事件写进 CLI 会去读的那个库（默认 out_dir = tmp/data，与配置保持一致）。"""
    from bili_danmaku.models import SessionMeta
    from bili_danmaku.sinks import SqliteSink

    target = out_dir or (tmp_path / "data")
    target.mkdir(parents=True, exist_ok=True)
    config = Config.load(
        path="missing.toml", overrides={"sinks": {"out_dir": str(target), "types": ["sqlite"]}}
    )
    sink = SqliteSink(config)
    sink.open(SessionMeta(session_id="20260918-141801-804456", room_id=804456, started_at=1_789_710_100.0))
    sink.write(events)
    sink.close()
    return target / "danmaku.db"


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30s", 30.0), ("5m", 300.0), ("1h", 3600.0), ("1d", 86400.0), ("90", 90.0), ("2.5m", 150.0), (" 10S ", 10.0)],
)
def test_parse_duration_accepts_units(text: str, expected: float) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "5x", "m5", "-3s"])
def test_parse_duration_rejects_garbage(text: str) -> None:
    import click

    with pytest.raises(click.BadParameter):
        parse_duration(text)


def test_resolve_deadline_from_duration() -> None:
    import time

    deadline = _resolve_deadline("1m", None)
    assert deadline is not None
    assert 50 <= deadline - time.monotonic() <= 61


def test_resolve_deadline_zero_means_unlimited() -> None:
    assert _resolve_deadline("0", None) is None


def test_resolve_deadline_until_today() -> None:
    import time

    # 用"1 小时后的钟点"在当时刻接近午夜时会滚到次日，反而变成"已过去"，
    # 所以这里要求它确实还没到；否则跳过（不是被测代码的问题）。
    target = time.localtime(time.time() + 3600)
    hhmm = f"{target.tm_hour:02d}:{target.tm_min:02d}"
    if not _is_past(hhmm):
        deadline = _resolve_deadline("0", hhmm)
        assert deadline is not None and deadline - time.monotonic() > 0


def test_resolve_deadline_rejects_past_and_invalid() -> None:
    import click
    import time

    with pytest.raises(click.BadParameter, match="无法解析时刻"):
        _resolve_deadline("0", "25-99")
    with pytest.raises(click.BadParameter, match="超出范围"):
        _resolve_deadline("0", "99:99")

    # 1 分钟前的时刻必然是"今天已过去"（跨零点时的极小概率窗口交给上层）
    past = time.localtime(time.time() - 60)
    hhmm = f"{past.tm_hour:02d}:{past.tm_min:02d}"
    if _is_past(hhmm):
        with pytest.raises(click.BadParameter, match="已过去"):
            _resolve_deadline("0", hhmm)


def _is_past(hhmm: str) -> bool:
    import time

    hour, minute = (int(part) for part in hhmm.split(":"))
    now = time.localtime()
    return (hour, minute) <= (now.tm_hour, now.tm_min)


def test_parse_time_accepts_timestamp_and_formats() -> None:
    assert _parse_time(None) is None
    assert _parse_time("1789710200") == 1_789_710_200.0
    assert _parse_time("2026-09-18 14:00:00") is not None
    assert _parse_time("2026-09-18 14:00") is not None
    assert _parse_time("2026-09-18") is not None


def test_parse_time_rejects_garbage() -> None:
    import click

    with pytest.raises(click.BadParameter):
        _parse_time("昨天下午")


def test_default_db_path_follows_config(tmp_path: Path) -> None:
    config = Config.load(path="missing.toml", overrides={"sinks": {"out_dir": str(tmp_path)}})
    assert _default_db_path(config) == tmp_path / "danmaku.db"

    custom = Config.load(
        path="missing.toml",
        overrides={"sinks": {"out_dir": str(tmp_path), "sqlite_path": str(tmp_path / "x.db")}},
    )
    assert _default_db_path(custom) == tmp_path / "x.db"


# --------------------------------------------------------------------------- #
# 通用参数与帮助
# --------------------------------------------------------------------------- #


def test_version_and_help(runner: CliRunner) -> None:
    assert runner.invoke(main, ["--version"]).exit_code == 0
    result = runner.invoke(main, ["-h"])
    assert result.exit_code == 0
    assert "login" in result.output and "grab" in result.output


def test_config_error_exits_with_code_2(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "broken.toml"
    bad.write_text("[client]\nprotover = 9\n", encoding="utf-8")
    result = runner.invoke(main, ["--config", str(bad), "stats"])
    assert result.exit_code == 2
    assert "配置错误" in result.output


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #


def test_stats_prints_report(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(
        tmp_path,
        [
            make_event(1, text="主播好", uname="甲"),
            make_event(2, text="你好", uname="乙"),
            make_event(3, text="第二条", uname="甲", type_="super_chat", price=30000),
        ],
    )
    result = runner.invoke(main, ["--config", str(config), "stats", "--top", "5"])
    assert result.exit_code == 0, result.output
    assert "数据统计" in result.output
    assert "总条数      3" in result.output
    assert "发言 TOP" in result.output
    assert "金额汇总" in result.output
    assert "最近会话" in result.output


def test_stats_json_output(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1)])
    result = runner.invoke(main, ["--config", str(config), "stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["by_type"] == {"danmaku": 1}
    assert payload["sessions"][0]["id"] == "20260918-141801-804456"


def test_stats_on_empty_scope_says_so(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1)])
    result = runner.invoke(main, ["--config", str(config), "stats", "--room", "999"])
    assert result.exit_code == 0
    assert "没有数据" in result.output


def test_stats_without_database_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "stats", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code == 1
    assert "无法读取数据库" in result.output


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def test_export_csv_to_file(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="导出我"), make_event(2, text="还有我", type_="super_chat", price=30)])
    out = tmp_path / "hits.csv"

    result = runner.invoke(
        main, ["--config", str(config), "export", "--format", "csv", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].split(",") == list(CSV_COLUMNS)
    assert len(lines) == 3
    assert any("导出我" in line for line in lines)
    assert out.read_bytes().startswith(b"\xef\xbb\xbf"), "CSV 需要 BOM 以便 Excel 打开"


def test_export_jsonl_to_stdout(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="流式")])
    result = runner.invoke(main, ["--config", str(config), "export", "--format", "jsonl"])
    assert result.exit_code == 0
    records = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert records and records[0]["text"] == "流式"


def test_export_filters_by_type_keyword_and_time(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(
        tmp_path,
        [
            make_event(1, text="主播好"),
            make_event(2, text="无关内容", type_="super_chat", price=100),
        ],
    )
    result = runner.invoke(
        main,
        [
            "--config", str(config), "export", "--format", "jsonl",
            "--types", "danmaku", "--keyword", "主播",
            "--since", "2026-01-01", "--limit", "10",
        ],
    )
    assert result.exit_code == 0
    records = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert [record["text"] for record in records] == ["主播好"]


def test_export_txt_keeps_only_danmaku(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="弹幕"), make_event(2, text="SC", type_="super_chat", price=30)])
    result = runner.invoke(main, ["--config", str(config), "export", "--format", "txt"])
    assert result.exit_code == 0
    assert "弹幕" in result.output and "SC" not in result.output


def test_export_empty_scope_warns(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1)])
    result = runner.invoke(main, ["--config", str(config), "export", "--session", "不存在"])
    assert result.exit_code == 0
    assert "没有数据" in result.output


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


def test_replay_converts_jsonl_to_csv(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    source = tmp_path / "raw.jsonl"
    events = [make_event(1, text="第一条"), make_event(2, text="第二条")]
    source.write_text(
        "\n".join(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "converted.csv"

    result = runner.invoke(main, ["--config", str(config), "replay", str(source), "--to", "csv", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "已转换 2 条" in result.output
    assert out.read_text(encoding="utf-8-sig").count("\n") == 3  # 表头 + 2 行


def test_replay_skips_broken_lines(runner: CliRunner, tmp_path: Path) -> None:
    """坏行只能跳过并告警，不能中断整体转换。"""
    config = write_config(tmp_path)
    source = tmp_path / "raw.jsonl"
    good = json.dumps(make_event(1, text="好的").model_dump(mode="json"), ensure_ascii=False)
    source.write_text(f"{good}\n不是 JSON\n{{\"seq\": \"错的\"}}\n\n", encoding="utf-8")

    result = runner.invoke(main, ["--config", str(config), "replay", str(source), "--to", "jsonl"])
    assert result.exit_code == 0
    assert "不是合法 JSON" in result.output
    assert "跳过第 3 行" in result.output
    assert result.output.count('"好的"') == 1, "只有那一条正常记录被写出"


def test_replay_applies_type_keyword_and_limit(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    source = tmp_path / "raw.jsonl"
    events = [
        make_event(1, text="主播好"),
        make_event(2, text="主播又说", type_="super_chat", price=30),
        make_event(3, text="主播第三次"),
    ]
    source.write_text(
        "\n".join(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        main,
        ["--config", str(config), "replay", str(source), "--to", "txt", "--types", "danmaku",
         "--keyword", "主播", "--limit", "1"],
    )
    assert result.exit_code == 0
    assert "已转换 1 条" in result.output


def test_replay_missing_source_is_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "replay", str(tmp_path / "nope.jsonl")])
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# _write_records 直接测（三种格式的差异都在这一个函数里）
# --------------------------------------------------------------------------- #


def test_write_records_csv_txt_jsonl(tmp_path: Path) -> None:
    events = [
        make_event(1, text="有文字"),
        make_event(2, text=None, type_="danmaku"),
        make_event(3, text="SC 正文", type_="super_chat", price=30),
    ]

    csv_path = tmp_path / "a.csv"
    with csv_path.open("w", encoding="utf-8", newline="\n") as handle:
        assert _write_records(handle, events, "csv") == 3

    txt_path = tmp_path / "a.txt"
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        assert _write_records(handle, events, "txt") == 1, "txt 只保留有文字的弹幕"
    assert "有文字" in txt_path.read_text(encoding="utf-8")

    jsonl_path = tmp_path / "a.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        assert _write_records(handle, events, "jsonl") == 3


# --------------------------------------------------------------------------- #
# 需要联网的命令：只测「参数校验即退出」路径。
#
# 注意：这些用例必须在**发起任何网络请求之前**就失败退出，否则测试会真的去连 B 站。
# 因此它们的断言都是「非 0 退出码 + 有错误提示」，不需要打桩。
# --------------------------------------------------------------------------- #


def test_grab_rejects_bad_keyword_regex(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path, cookie_file=tmp_path / "none.json")
    result = runner.invoke(
        main, ["--config", str(config), "grab", "804456", "--keyword", "re:(["]
    )
    assert result.exit_code == 2, result.output
    assert "正则" in result.output


def test_grab_rejects_bad_keyword_mode(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path, cookie_file=tmp_path / "none.json")
    result = runner.invoke(main, ["--config", str(config), "grab", "804456", "--keyword-mode", "xor"])
    assert result.exit_code == 2, "click Choice 校验失败"


def test_grab_rejects_bad_duration(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path, cookie_file=tmp_path / "none.json")
    result = runner.invoke(main, ["--config", str(config), "grab", "804456", "--duration", "很久"])
    assert result.exit_code == 2
    assert "无法解析时长" in result.output


def test_grab_rejects_bad_until(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path, cookie_file=tmp_path / "none.json")
    result = runner.invoke(main, ["--config", str(config), "grab", "804456", "--until", "25-99"])
    assert result.exit_code == 2


def test_grab_rejects_unknown_format(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path, cookie_file=tmp_path / "none.json")
    result = runner.invoke(main, ["--config", str(config), "grab", "804456", "--format", "xml"])
    assert result.exit_code == 2
    assert "未知 sink 类型" in result.output or "不可用" in result.output


def test_all_commands_are_registered() -> None:
    """命令集就是对外契约，少一个都算破坏。"""
    expected = {
        "login", "check-auth", "purge", "probe", "grab", "web",
        "stats", "export", "replay", "wordcloud", "sc",
    }
    assert expected <= set(main.commands)


# --------------------------------------------------------------------------- #
# sc（V2-b）：Super Chat 汇总 / 告警
# --------------------------------------------------------------------------- #


def sc_config(tmp_path: Path) -> Path:
    """写一份配置，并把 SC 事件种进 CLI 会去读的那个库。"""
    config = write_config(tmp_path)
    seed_db(
        tmp_path,
        [
            make_event(1, text="主播加油", type_="super_chat", price=30, uname="甲"),
            make_event(2, text="这波漂亮", type_="super_chat", price=50, uname="乙"),
            make_event(3, text="再充一次", type_="super_chat", price=100, uname="甲"),
            make_event(4, text="普通弹幕"),
        ],
    )
    return config


def test_sc_prints_summary(runner: CliRunner, tmp_path: Path) -> None:
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc"])
    assert result.exit_code == 0, result.output
    assert "Super Chat 汇总" in result.output
    assert "条数        3" in result.output
    assert "合计        180 元" in result.output
    assert "贡献 TOP" in result.output
    assert "主播加油" in result.output
    assert "普通弹幕" not in result.output, "非 SC 不能出现在 SC 报表里"


def test_sc_json_output(runner: CliRunner, tmp_path: Path) -> None:
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 3
    assert payload["total_price"] == 180
    assert payload["unit"] == "元"
    assert payload["price_unit"] == "yuan"
    assert payload["top_senders"][0]["uname"] == "甲"


def test_sc_min_price_filter(runner: CliRunner, tmp_path: Path) -> None:
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc", "--min-price", "50", "--json"])
    payload = json.loads(result.output)
    assert payload["count"] == 2
    assert payload["total_price"] == 150
    assert payload["min_price"] == 50


def test_sc_price_unit_override(runner: CliRunner, tmp_path: Path) -> None:
    """没验证过单位时的逃生舱：`--price-unit milli` 把金额当毫元。"""
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc", "--price-unit", "milli", "--json"])
    payload = json.loads(result.output)
    assert payload["price_unit"] == "milli"
    assert payload["total_price"] == 0.18

    text_run = runner.invoke(main, ["--config", str(config), "sc", "--price-unit", "milli"])
    assert "金额单位" in text_run.output, "非默认单位要在终端里明示"


def test_sc_rejects_unknown_price_unit(runner: CliRunner, tmp_path: Path) -> None:
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc", "--price-unit", "分"])
    assert result.exit_code == 2
    assert "price-unit" in result.output


def test_sc_empty_scope(runner: CliRunner, tmp_path: Path) -> None:
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc", "--room", "999"])
    assert result.exit_code == 0
    assert "没有 SC" in result.output


def test_sc_follow_requires_room(runner: CliRunner, tmp_path: Path) -> None:
    """`--follow` 必须有房间号：否则应立刻以配置错误退出，不该去打网络。"""
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc", "--follow"])
    assert result.exit_code == 2
    assert "需要指定房间号" in result.output


def test_sc_accepts_positional_room(runner: CliRunner, tmp_path: Path) -> None:
    """位置参数用来限定房间（与 `--room` 等价）。"""
    config = sc_config(tmp_path)
    result = runner.invoke(main, ["--config", str(config), "sc", "804456", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["count"] == 3

    other = runner.invoke(main, ["--config", str(config), "sc", "999999", "--json"])
    assert json.loads(other.output)["count"] == 0


# --------------------------------------------------------------------------- #
# wordcloud（V2）
# --------------------------------------------------------------------------- #


def test_wordcloud_prints_ranking(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(
        tmp_path,
        [
            make_event(1, text="主播好可爱"),
            make_event(2, text="主播加油"),
            make_event(3, text="打卡打卡"),
        ],
    )
    result = runner.invoke(main, ["--config", str(config), "wordcloud", "--top", "10"])
    assert result.exit_code == 0, result.output
    assert "文本条数    3" in result.output
    assert "主播" in result.output
    assert "分词" in result.output


def test_wordcloud_emotes_are_separate_by_default(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(
        tmp_path,
        [make_event(1, text="哈哈[dog]", extra={"emots": {"[dog]": {"url": "https://x/y.png"}}})],
    )

    plain = runner.invoke(main, ["--config", str(config), "wordcloud", "--format", "json"])
    payload = json.loads(plain.output)
    assert payload["emotes"][0]["name"] == "dog"
    assert payload["emotes"][0]["count"] == 1
    assert all(item["word"] != "[dog]" for item in payload["words"])
    assert all("[" not in item["word"] for item in payload["words"])

    with_emotes = runner.invoke(
        main, ["--config", str(config), "wordcloud", "--format", "json", "--include-emotes"]
    )
    assert any(item["word"] == "[dog]" for item in json.loads(with_emotes.output)["words"])


def test_wordcloud_json_to_file(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="主播好")])
    out = tmp_path / "wc.json"
    result = runner.invoke(
        main, ["--config", str(config), "wordcloud", "--format", "json", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["filters"]["types"] == "danmaku,super_chat"
    assert payload["words"][0]["word"] == "主播"


def test_wordcloud_html_output_is_self_contained(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="主播好可爱")])
    out = tmp_path / "wc.html"
    result = runner.invoke(
        main, ["--config", str(config), "wordcloud", "--format", "html", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    content = out.read_text(encoding="utf-8")
    assert "<html" in content and "主播" in content
    assert "http://" not in content, "HTML 不应依赖外部资源"


def test_wordcloud_png_output(runner: CliRunner, tmp_path: Path) -> None:
    pytest.importorskip("wordcloud")
    from bili_danmaku.text import find_cjk_font

    if find_cjk_font() is None:
        pytest.skip("系统没有中文字体")

    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(index, text="主播加油打卡") for index in range(1, 6)])
    out = tmp_path / "wc.png"
    result = runner.invoke(
        main,
        ["--config", str(config), "wordcloud", "--format", "png", "--out", str(out), "--top", "5"],
    )
    assert result.exit_code == 0, result.output
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_wordcloud_png_reports_missing_dependency(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 wordcloud 时应给出可执行的提示并以配置错误码退出，而不是抛栈。"""
    from bili_danmaku import text as text_module

    def boom(*args: object, **kwargs: object):
        raise text_module.MissingDependency(
            "渲染 PNG 需要 wordcloud：pip install 'bili-danmaku[wordcloud]'"
        )

    monkeypatch.setattr(text_module, "render_png", boom)

    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="主播好")])
    result = runner.invoke(
        main,
        ["--config", str(config), "wordcloud", "--format", "png", "--out", str(tmp_path / "x.png")],
    )
    assert result.exit_code == 2
    assert "wordcloud" in result.output


def test_wordcloud_from_jsonl_source(runner: CliRunner, tmp_path: Path) -> None:
    """`--source` 走 JSONL，完全不碰数据库（离线可用）。"""
    config = write_config(tmp_path)
    source = tmp_path / "raw.jsonl"
    lines = [
        json.dumps(
            make_event(1, text="主播好[dog]", extra={"emots": {"[dog]": {"url": "u"}}}).model_dump(mode="json"),
            ensure_ascii=False,
        ),
        json.dumps(make_event(2, text="主播棒").model_dump(mode="json"), ensure_ascii=False),
        "不是 JSON",
    ]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 输出到文件而不是 stdout：CliRunner 会混入 stderr 的"跳过第 3 行"告警，
    # 直接 json.loads(stdout) 会被那行提示污染
    out = tmp_path / "wc.json"
    result = runner.invoke(
        main,
        ["--config", str(config), "wordcloud", "--source", str(source), "--format", "json", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "跳过第 3 行" in result.output, "坏行应告警但不中断"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["total_texts"] == 2
    assert payload["words"][0] == {"word": "主播", "count": 2}
    assert payload["emotes"][0]["name"] == "dog"


def test_wordcloud_empty_scope_says_so(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="主播")])
    result = runner.invoke(main, ["--config", str(config), "wordcloud", "--session", "不存在"])
    assert result.exit_code == 0
    assert "没有数据" in result.output


def test_wordcloud_honours_stopwords_and_length_flags(runner: CliRunner, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    seed_db(tmp_path, [make_event(1, text="主播好可爱")])
    stop = tmp_path / "stop.txt"
    stop.write_text("主播\n", encoding="utf-8")

    result = runner.invoke(
        main,
        [
            "--config", str(config), "wordcloud", "--format", "json",
            "--stopwords-file", str(stop), "--min-length", "1", "--min-count", "1",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    words = {item["word"] for item in payload["words"]}
    assert "主播" not in words, "自定义停用词应覆盖内置表"
    assert "可爱" in words, "其余词照常统计"


def test_wordcloud_works_on_legacy_db(runner: CliRunner, tmp_path: Path) -> None:
    """老库（schema v1，没有 extra_json 列）也要能出词云。"""
    import sqlite3

    config = write_config(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "danmaku.db"
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE session (id TEXT PRIMARY KEY, room_id INTEGER NOT NULL,
                short_id INTEGER, anchor_uid INTEGER, token_source TEXT,
                started_at REAL NOT NULL, ended_at REAL, note TEXT);
            CREATE TABLE event (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                room_id INTEGER NOT NULL, recv_ts REAL NOT NULL, server_ts INTEGER,
                latency_ms INTEGER, type TEXT NOT NULL, cmd TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'live', msg_id TEXT, uid INTEGER, uname TEXT,
                text TEXT, price INTEGER, guard_level INTEGER, user_level INTEGER,
                medal_name TEXT, medal_level TEXT, field_source TEXT,
                dedupe_key TEXT UNIQUE, raw_json TEXT);
            INSERT INTO session (id, room_id, started_at) VALUES ('old', 804456, 1.0);
            INSERT INTO event (session_id, room_id, recv_ts, type, cmd, text, dedupe_key)
                VALUES ('old', 804456, 1.0, 'danmaku', 'DANMU_MSG', '老库弹幕内容', 'k1');
            """
        )
        connection.commit()
    finally:
        connection.close()

    result = runner.invoke(main, ["--config", str(config), "wordcloud", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_texts"] == 1
    assert {item["word"] for item in payload["words"]} == {"老库", "弹幕", "内容"}
