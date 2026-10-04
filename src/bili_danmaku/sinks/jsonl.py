"""JSONL 落盘：每行一个事件的 JSON。

`ensure_ascii=False` 保留中文原样，便于直接用文本工具查看。
轮转与续写逻辑由 `RotatingFileWriter` 承担（CSV/TXT 共用）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence

from ..config import Config
from ..models import Event, SessionMeta
from .base import RotatingFileWriter, Sink


class JsonlSink(Sink):
    name = "jsonl"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._writer = RotatingFileWriter(self, suffix=".jsonl", encoding="utf-8")

    def open(self, meta: SessionMeta) -> None:
        super().open(meta)

    def write(self, events: Sequence[Event]) -> None:
        written_events = 0
        written_bytes = 0
        for event in events:
            self._writer.ensure_open(event.recv_ts or time.time())
            line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            written_bytes += self._writer.writeline(line)
            written_events += 1
        self.record(written_events, written_bytes)

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()

    @property
    def current_path(self) -> str | None:
        return self._writer.current_path
