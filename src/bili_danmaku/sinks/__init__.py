"""落盘层：Sink 抽象、具体实现与扇出总线。"""

from __future__ import annotations

from .base import RotatingFileWriter, Sink
from .bus import Bus, SinkWorker
from .csv_txt import CSV_COLUMNS, CsvSink, TxtSink, sanitize_cell
from .jsonl import JsonlSink
from .sqlite import SqliteSink


class SinkUnavailable(Exception):
    """请求了尚未实现或不存在的 sink。"""


#: 已实现的 sink（对应 CLI `--format`）
SINK_REGISTRY: dict[str, type[Sink]] = {
    "jsonl": JsonlSink,
    "csv": CsvSink,
    "txt": TxtSink,
    "sqlite": SqliteSink,
}

#: 已规划但尚未实现的 sink
PLANNED_SINKS: dict[str, str] = {}


def build_sinks(names: list[str], config) -> list[Sink]:
    """按名称构造 sink 实例。重复的名称只构造一次。"""
    sinks: list[Sink] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        sink_cls = SINK_REGISTRY.get(name)
        if sink_cls is None:
            if name in PLANNED_SINKS:
                raise SinkUnavailable(
                    f"sink '{name}' 计划在 {PLANNED_SINKS[name]} 实现，"
                    f"当前可用: {', '.join(sorted(SINK_REGISTRY))}"
                )
            raise SinkUnavailable(
                f"未知 sink 类型 '{name}'，可用: {', '.join(sorted(SINK_REGISTRY))}"
            )
        sinks.append(sink_cls(config))
    return sinks


__all__ = [
    "Bus",
    "CSV_COLUMNS",
    "CsvSink",
    "JsonlSink",
    "PLANNED_SINKS",
    "RotatingFileWriter",
    "SINK_REGISTRY",
    "Sink",
    "SinkUnavailable",
    "SinkWorker",
    "SqliteSink",
    "TxtSink",
    "build_sinks",
    "sanitize_cell",
]
