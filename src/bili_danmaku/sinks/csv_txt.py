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
#:
#: ⚠️ **加列只能往后加**：这两列在历史 CSV 与用户的脚本里都按**位置**被引用过
#: （"第 1 列是时间戳"）。插到中间会让所有既有脚本静默错位。
CSV_COLUMNS = (
    "ts",
    "time",
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


#: `format_ts` 的**分钟级前缀缓存**。
#:
#: 同一条弹幕流里绝大多数行的 `YYYY-MM-DD HH:MM` 是重复的，而
#: `time.localtime()` + `strftime` 正好是最贵的一步。实测 10 万行导出：
#: 每行直算要多 **175%** 的时间，缓存分钟前缀后只多 **41.6%**
#: （`tools/bench_csv_timestamp.py` 可复跑）。
#:
#: 上限 4 天：够覆盖"连续两场直播 + 跨夜"，又不会无界增长（常驻进程里）。
_TS_PREFIX_CACHE: dict[int, str] = {}
_TS_PREFIX_CACHE_LIMIT = 4 * 24 * 60


def format_ts(recv_ts: float | None) -> str:
    """`recv_ts` → `YYYY-MM-DD HH:MM:SS`（**本地时间**，秒级）。

    需求："统计的弹幕 CSV 数据中添加时间戳数据，为每一条弹幕都匹配一个时间戳，
    时间戳单位精确到秒即可"。原来只有 `ts`（unix 秒带 3 位小数，如
    `1791368492.149`）—— 在 Excel 里既不是日期也没法直接读。

    为什么保留 `ts` 再加一列而不是替换它：
      * `ts` 是**时区无关**的绝对值，脚本做区间计算不会因为换台机器而错；
      * `time` 是给人看的本地时间，**会随时区变**。
    两者都在才既好读又算得准。

    为什么用秒级、且**不做时区归一化**：采集发生在本机，`recv_ts` 就是本机时钟；
    转成 UTC 反而让"这条几点收到的"要心算。这就是用 `localtime` 的原因。
    需求给的退路是"性能影响大就放宽到分钟"，而实测**分钟级只比秒级快 6%**
    （详见上面的缓存说明），所以没必要牺牲可读性。
    """
    if not recv_ts:
        return ""
    try:
        value = float(recv_ts)
        minute = int(value // 60)
        prefix = _TS_PREFIX_CACHE.get(minute)
        if prefix is None:
            prefix = time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
            if len(_TS_PREFIX_CACHE) >= _TS_PREFIX_CACHE_LIMIT:
                _TS_PREFIX_CACHE.clear()
            _TS_PREFIX_CACHE[minute] = prefix
        return f"{prefix}:{int(value % 60):02d}"
    except (OSError, OverflowError, ValueError):
        # 非法时间戳（负数/超出平台范围）不该让整次导出失败
        return ""


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
            # 人类可读的本地时间（秒级）；见 `format_ts` 的说明
            sanitize_cell(format_ts(event.recv_ts)),
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
