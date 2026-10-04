"""多 sink 扇出总线。

每个 sink 拥有**独立的消费任务与有界队列**：
    * 某个 sink 慢或卡住不会拖累其他 sink，更不会阻塞采集读循环
      （阻塞读循环会导致心跳发不出去而被服务端断开）；
    * 队列满时丢最旧并计数，绝不 OOM。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import Config
from ..models import Event, SessionMeta, SinkStats
from .base import Sink

log = logging.getLogger("bili_danmaku.sinks.bus")

#: 连续失败多少次后停用该 sink（其余 sink 继续工作）
MAX_CONSECUTIVE_FAILURES = 3


class SinkWorker:
    """把一个 sink 包装成带队列的独立消费者。"""

    def __init__(
        self,
        sink: Sink,
        *,
        queue_size: int,
        batch_size: int,
        flush_interval: float,
    ) -> None:
        self.sink = sink
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max(queue_size, 1))
        self.batch_size = max(batch_size, 1)
        self.flush_interval = max(flush_interval, 0.05)
        self.dropped = 0
        self.disabled = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ #
    async def start(self, meta: SessionMeta) -> None:
        self.sink.open(meta)
        self._task = asyncio.create_task(self._run(), name=f"sink-{self.sink.name}")

    def submit(self, event: Event) -> bool:
        """非阻塞投递。返回 False 表示被丢弃。"""
        if self.disabled:
            return False
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            pass

        # 丢最旧，为最新事件腾位置
        try:
            self.queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - 并发兜底
            pass
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - 并发兜底
            pass
        self.dropped += 1
        return False

    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        batch: list[Event] = []
        last_flush = time.monotonic()

        while True:
            timeout = max(self.flush_interval - (time.monotonic() - last_flush), 0.01)
            # 上限 1 秒：否则 flush_interval 很大时会长时间阻塞在 get() 上，
            # 导致 stop() 无法被及时感知（实测 stop 要等满 flush_interval 才返回）。
            # 空闲时多醒几次的代价可以忽略。
            timeout = min(timeout, 1.0)
            try:
                batch.append(await asyncio.wait_for(self.queue.get(), timeout=timeout))
            except asyncio.TimeoutError:
                pass

            due = bool(batch) and (
                len(batch) >= self.batch_size
                or (time.monotonic() - last_flush) >= self.flush_interval
            )
            if due:
                await self._flush_off_loop(batch)
                batch = []
                last_flush = time.monotonic()

            if self._stop.is_set() and self.queue.empty():
                break

        if batch:
            await self._flush_off_loop(batch)
        self._close()

    def _flush(self, batch: list[Event]) -> None:
        try:
            self.sink.write(batch)
            self.sink.flush()
            self._consecutive_failures = 0
        except Exception as exc:  # noqa: BLE001 - sink 故障不得影响采集
            self._consecutive_failures += 1
            self.sink.mark_failed(f"{type(exc).__name__}: {exc}")
            log.error(
                "sink %s 写入失败(%d/%d): %s",
                self.sink.name,
                self._consecutive_failures,
                MAX_CONSECUTIVE_FAILURES,
                exc,
            )
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self.disabled = True
                self._stop.set()
                log.error("sink %s 连续失败，已停用（其余 sink 继续）", self.sink.name)

    async def _flush_off_loop(self, batch: list[Event]) -> None:
        """把 sink 的同步写入挪到工作线程。

        **为什么必须这样做**：`write()` 是同步 IO（SQLite 提交、文件 fsync）。
        直接在事件循环里调用它，只要磁盘一慢，整个循环就被卡住 ——
        心跳任务发不出去，采集器会判定「连接假死」并重连。
        实测用一个故意 sleep 3 秒的 sink 就能复现这条链路：
        收 2 条 → 假死 → 重连，而队列根本没机会顶满。

        刻意**不加超时**：卡死的 sink 只该拖住它自己，不该拖累心跳或读循环。
        给它设超时反而危险 —— 超时后线程仍会跑完，下一批再进来就可能
        并发操作同一个 SQLite 连接。宁可让它一直卡着（有界：一个线程），
        由 `stop()`（15 秒上限后取消）负责收尾。
        """
        await asyncio.to_thread(self._flush, batch)

    def _close(self) -> None:
        try:
            self.sink.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("sink %s 关闭失败: %s", self.sink.name, exc)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=15.0)
        except asyncio.TimeoutError:
            log.warning("sink %s 未在超时内结束，强制取消", self.sink.name)
            self._task.cancel()
        except Exception as exc:  # noqa: BLE001
            log.warning("sink %s 任务异常: %s", self.sink.name, exc)
        finally:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class Bus:
    """把事件扇出到所有 sink。"""

    def __init__(self, sinks: list[Sink], config: Config) -> None:
        self.config = config
        self.workers: list[SinkWorker] = [
            SinkWorker(
                sink,
                queue_size=config.sinks.queue_size,
                batch_size=config.sinks.batch_size,
                flush_interval=config.sinks.flush_interval,
            )
            for sink in sinks
        ]

    async def start(self, meta: SessionMeta) -> None:
        for worker in self.workers:
            await worker.start(meta)

    def publish(self, event: Event) -> bool:
        """同步投递，绝不阻塞调用方。返回是否有 sink 接收。"""
        accepted = False
        for worker in self.workers:
            if worker.submit(event):
                accepted = True
        return accepted

    async def stop(self) -> None:
        await asyncio.gather(*(worker.stop() for worker in self.workers), return_exceptions=True)

    # ------------------------------------------------------------------ #
    @property
    def dropped(self) -> int:
        return sum(worker.dropped for worker in self.workers)

    @property
    def stats(self) -> dict[str, SinkStats]:
        out: dict[str, SinkStats] = {}
        for worker in self.workers:
            stats = worker.sink.stats.model_copy()
            stats.dropped += worker.dropped
            out[worker.sink.name] = stats
        return out

    @property
    def healthy(self) -> bool:
        return all(not worker.disabled for worker in self.workers)
