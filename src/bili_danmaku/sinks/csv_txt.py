"""CSV / TXT 落盘。

* CSV：`utf-8-sig`（Excel 直接打开不乱码），列固定，带表头；
* TXT：`[HH:MM:SS] 昵称: 内容`，仅弹幕，便于词云与直接阅读；
* 两者都做 **CSV 公式注入防护**：以 `= + - @` 开头的字段前置 `'`。

轮转与续写由 `RotatingFileWriter` 承担。
"""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Sequence

from ..config import Config
from ..models import Event, EventType, SessionMeta
from .base import RotatingFileWriter, Sink

#: CSV 列顺序（改这里要同步改读取方）
CSV_COLUMNS = (
    "ts",
    "type",
    "uid",
    "uname",
    "text",
    "price",
    "guard_level",
    "medal_level",
    "msg_id",
)

#: 会被 Excel/Sheets 当公式解析的前缀
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def sanitize_cell(value: object) -> str:
    """CSV 公式注入防护。非字符串原样转字符串。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


class CsvSink(Sink):
    name = "csv"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._writer = RotatingFileWriter(
            self,
            suffix=".csv",
            encoding="utf-8-sig",
            header=",".join(CSV_COLUMNS) + "\n",
        )

    def open(self, meta: SessionMeta) -> None:
        super().open(meta)

    @staticmethod
    def _row(event: Event) -> list[str]:
        return [
            sanitize_cell(f"{event.recv_ts:.3f}" if event.recv_ts else ""),
            sanitize_cell(event.type),
            sanitize_cell(event.uid),
            sanitize_cell(event.uname),
            sanitize_cell(event.text),
            sanitize_cell(event.price),
            sanitize_cell(event.guard_level),
            sanitize_cell(event.medal.level if event.medal else None),
            sanitize_cell(event.msg_id),
        ]

    def write(self, events: Sequence[Event]) -> None:
        written = 0
        total_bytes = 0
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        for event in events:
            self._writer.ensure_open(event.recv_ts or time.time())
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(self._row(event))
            total_bytes += self._writer.writeline(buffer.getvalue().rstrip("\n"))
            written += 1
        self.record(written, total_bytes)

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()

    @property
    def current_path(self) -> str | None:
        return self._writer.current_path


class TxtSink(Sink):
    """纯文本弹幕，`[HH:MM:SS] 昵称: 内容`。非弹幕类型直接跳过。"""

    name = "txt"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._writer = RotatingFileWriter(self, suffix=".txt", encoding="utf-8")
        self.skipped = 0

    def open(self, meta: SessionMeta) -> None:
        super().open(meta)

    @staticmethod
    def _line(event: Event) -> str | None:
        if event.type != EventType.DANMAKU.value:
            return None
        text = (event.text or "").replace("\r", " ").replace("\n", " ")
        if not text:
            return None
        stamp = time.strftime("%H:%M:%S", time.localtime(event.recv_ts or time.time()))
        name = event.uname or "匿名"
        return f"[{stamp}] {name}: {text}"

    def write(self, events: Sequence[Event]) -> None:
        written = 0
        total_bytes = 0
        for event in events:
            line = self._line(event)
            if line is None:
                self.skipped += 1
                continue
            self._writer.ensure_open(event.recv_ts or time.time())
            total_bytes += self._writer.writeline(line)
            written += 1
        self.record(written, total_bytes)

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()

    @property
    def current_path(self) -> str | None:
        return self._writer.current_path
