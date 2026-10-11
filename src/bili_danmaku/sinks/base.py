"""Sink 抽象与基类。

约定：
    * `write()` 接收一批事件，同步写；抛异常由 Bus 捕获并降级；
    * sink 实例不持有事件（写完即丢），因此内存占用与运行时长无关；
    * `open/close` 由 Bus 在会话开始/结束调用。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from ..config import Config
from ..models import Event, SessionMeta, SinkStats

log = logging.getLogger("bili_danmaku.sinks")

#: 单文件体积上限，超过即轮转
MAX_FILE_BYTES = 128 * 1024 * 1024


class Sink(ABC):
    """落盘目标的抽象基类。"""

    #: 唯一名称，同时用作 CLI `--format` 取值与统计 key
    name: str = "sink"

    #: 本 sink 是否**把原始帧持久化下来**（`Event.raw`）。
    #:
    #: 背景：`Event` 对象在多个 sink 队列里是**共享引用**，
    #: 而 `raw` 是它身上最大的一块（JSON 文本，约 0.9~3 KB/条）。既然它已经
    #: 被写进库/文件，那条事件就不必再背着它 —— 及时放掉引用，队列里剩下的
    #: 对象立刻变轻。
    #:
    #: 口径必须**保守**：`Bus` 只有在「本次所有 sink 都声明会持久化 raw」时
    #: 才敢释放（见 `Bus._release_raw`）。只声明一个 sink 是不够的 ——
    #: 多个 sink 并发消费同一批共享对象，谁先跑完就把 raw 清掉，
    #: 后跑的 sink 就再也拿不到原始帧了。只要有一个 sink 不落 raw
    #: （csv / txt），就宁可一直留着。
    persists_raw: bool = False

    def __init__(self, config: Config) -> None:
        self.config = config
        self.stats = SinkStats()
        self._meta: SessionMeta | None = None

    # ------------------------------------------------------------------ #
    @property
    def meta(self) -> SessionMeta:
        if self._meta is None:
            raise RuntimeError(f"sink {self.name} 尚未 open()")
        return self._meta

    @property
    def out_dir(self) -> str:
        return self.config.sinks.out_dir

    def open(self, meta: SessionMeta) -> None:
        self._meta = meta

    @abstractmethod
    def write(self, events: Sequence[Event]) -> None:
        """写入一批事件。实现方不负责批大小/频率控制。"""

    def flush(self) -> None:
        """把缓冲刷到磁盘。默认无操作。"""

    def close(self) -> None:
        """收尾。默认无操作。"""

    # ------------------------------------------------------------------ #
    def record(self, count: int, size_bytes: int = 0) -> None:
        self.stats.written += count
        self.stats.bytes += size_bytes

    def note_dropped(self, count: int = 1) -> None:
        self.stats.dropped += count

    def mark_failed(self, error: str) -> None:
        self.stats.ok = False
        self.stats.failed += 1
        self.stats.error = error

    # ------------------------------------------------------------------ #
    def hour_bucket(self, timestamp: float | None = None) -> str:
        """按 `rotate_hours` 生成时间桶名，如 `20260918-13`。"""
        hours = max(float(self.config.sinks.rotate_hours or 0), 0.0)
        moment = time.time() if timestamp is None else timestamp
        if hours <= 0:
            return time.strftime("%Y%m%d", time.localtime(moment))
        seconds = max(int(hours * 3600), 1)
        return time.strftime("%Y%m%d-%H", time.localtime(int(moment / seconds) * seconds))


class RotatingFileWriter:
    """按「时间桶 + 体积上限」轮转的文本写入器。

    JSONL / CSV / TXT 三个 sink 共用，避免三份重复的轮转与续写逻辑。
    文件名形如 `{room_id}_{时间桶}{suffix}`；已存在的文件**追加**而不是覆盖。
    """

    def __init__(
        self,
        sink: Sink,
        *,
        suffix: str,
        encoding: str = "utf-8",
        header: str | None = None,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self._sink = sink
        self._suffix = suffix
        self._encoding = encoding
        self._header = header
        self._max_bytes = max_bytes
        self._handle: IO[str] | None = None
        self._bucket: str | None = None
        self._path: Path | None = None
        self._size = 0

    # ------------------------------------------------------------------ #
    def ensure_open(self, timestamp: float) -> None:
        bucket = self._sink.hour_bucket(timestamp)
        if self._handle is not None and bucket == self._bucket and self._size < self._max_bytes:
            return

        self.close()
        directory = Path(self._sink.out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._sink.meta.room_id}_{bucket}{self._suffix}"

        is_new = not path.exists()
        self._handle = path.open("a", encoding=self._encoding, newline="\n")
        self._bucket = bucket
        self._path = path
        self._size = path.stat().st_size
        if is_new and self._header:
            self._handle.write(self._header)
            self._size += len(self._header.encode(self._encoding))
        log.info("%s 输出: %s", self._sink.name, path)

    def writeline(self, line: str) -> int:
        if self._handle is None:
            raise RuntimeError("RotatingFileWriter 尚未就绪（请先调用 ensure_open）")
        self._handle.write(line + "\n")
        size = len(line.encode(self._encoding)) + 1
        self._size += size
        return size

    def flush(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                self._handle.close()
            except OSError as exc:  # noqa: BLE001
                log.warning("关闭文件失败: %s", exc)
            self._handle = None

    @property
    def current_path(self) -> str | None:
        return str(self._path) if self._path else None
