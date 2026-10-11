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
from collections.abc import Callable
from typing import Any

from ..config import Config
from ..models import Event, SessionMeta, SinkStats
from .base import Sink

log = logging.getLogger("bili_danmaku.sinks.bus")

#: 连续失败多少次后停用该 sink（其余 sink 继续工作）
MAX_CONSECUTIVE_FAILURES = 3

#: "队列顶满"告警的最小间隔（秒）。限频而不是只报一次：
#: 偶发顶满无所谓，**反复**顶满才是"queue_size 调小了"的信号。
OVERFLOW_LOG_INTERVAL = 60.0


class SinkWorker:
    """把一个 sink 包装成带队列的独立消费者。"""

    def __init__(
        self,
        sink: Sink,
        *,
        queue_size: int,
        batch_size: int,
        flush_interval: float,
        on_written: Callable[[list[Event]], Any] | None = None,
    ) -> None:
        self.sink = sink
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max(queue_size, 1))
        self.batch_size = max(batch_size, 1)
        self.flush_interval = max(flush_interval, 0.05)
        self.dropped = 0
        self.disabled = False
        #: 写成功后的回调（第三步用来释放 `raw`）。由 `Bus` 注入；
        #: `None` 表示这条 worker 不参与释放（例如还有 sink 没把 raw 落下来）。
        self.on_written = on_written
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._consecutive_failures = 0
        #: 上次"队列顶满"告警的时刻（限频用，见 `submit`）
        self._last_overflow_log = 0.0

    # ------------------------------------------------------------------ #
    async def start(self, meta: SessionMeta) -> None:
        """打开 sink 并启动它的消费协程。

        ⚠️ **`open()` 必须走工作线程**（2026-10-11 实测，与同步 IO 阻塞事件循环是同一条原理）：
        `sink.open()` 是**同步 IO**，而 `Bus.start()` 是在**事件循环**里被 await 的
        （`RoomCollector.run()` → `await self.bus.start(...)`）。写在循环里就是
        "打开 sink 期间整个面板停摆" —— 连 `/api/health` 都不回。

        这条以前看不出来，因为 `open()` 通常只有几毫秒（建目录 + 开连接 + 跑一遍
        `SCHEMA`）。但 `SCHEMA` 里是 `CREATE INDEX IF NOT EXISTS`，**老库**首次
        开库会真的建一次索引：实测 1.6 GB / 383,248 行的库上要
        **5~16 s**，而它正好发生在
        "用户点添加房间"那条交互路径上 —— 表现为**整个面板卡住十几秒**，
        探针实测期间有 health 请求直接 5 s 超时。

        与 D17 的 `_flush_off_loop()` 是同一个修法：写入已经在工作线程里了，
        这里把**打开**也补上（`close()` 同理，见 `stop()`）。
        """
        # `to_thread` 的惰性求值在这里无害：`open()` 本身没有 await，
        # 交给线程池执行即可（`args` 里不能出现 await，那样会在调用前就展开）。
        await asyncio.to_thread(self.sink.open, meta)
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
        # 队列顶满 = sink 生产快过消费，此时**正在丢数据**（丢的是最旧的）。
        # 限频提醒（60 秒一次），既不会刷屏、又能让"反复顶满"看得见 ——
        # 这是 queue_size 调小之后"是不是调得太小"的唯一现场信号，
        # 否则用户只会看到产物条数变少而不知道原因。
        now = time.monotonic()
        if now - self._last_overflow_log >= OVERFLOW_LOG_INTERVAL:
            self._last_overflow_log = now
            log.warning(
                "sink %s 队列已满（queue_size=%d），开始丢弃最旧事件（累计 %d 条）。"
                "若频繁出现，说明该 sink 跟不上生产速率："
                "调大 sinks.queue_size 或 sinks.batch_size",
                self.sink.name,
                self.queue.maxsize,
                self.dropped,
            )
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
            return

        # 只在**写成功之后**释放原始帧：写失败时 raw 还留在对象上，
        # 下一批（或重试）还有机会把它落下来，不会静默丢取证数据。
        if self.on_written is not None:
            try:
                self.on_written(batch)
            except Exception as exc:  # noqa: BLE001 - 释放失败绝不能影响落盘
                log.warning("sink %s 释放 raw 失败: %s", self.sink.name, exc)

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
        #: 是否可以安全地"落盘后释放 raw"。
        #:
        #: 判据**必须保守**：只有当本次**每一个** sink 都声明会把原始帧持久化下来
        #: （`Sink.persists_raw`）时才允许。因为各 sink 的 worker 是并发消费
        #: **同一批共享对象**的 —— 只要有一个 sink 不落 raw（csv / txt），
        #: 先跑完的那个把 `raw` 置空，后跑的就永远拿不到原始帧了。
        #:
        #: 常见配置里 `types` 默认是 jsonl+csv+sqlite，csv 不落 raw ⇒ 默认**不释放**，
        #: 行为与改造前完全一致；只有显式配成 sqlite / jsonl（或两者）时才生效。
        self.release_raw_after_write = bool(sinks) and all(s.persists_raw for s in sinks)
        on_written = self.release_raw if self.release_raw_after_write else None
        self.workers: list[SinkWorker] = [
            SinkWorker(
                sink,
                queue_size=config.sinks.queue_size,
                batch_size=config.sinks.batch_size,
                flush_interval=config.sinks.flush_interval,
                on_written=on_written,
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
    def release_raw(self, batch: list[Event]) -> int:
        """放掉这一批事件上的原始帧引用，返回释放了几个。

        第三步：`raw` 是 `Event` 身上最大的一块（JSON 文本，0.9~3 KB/条）。
        sink 把它持久化下来之后，事件就不必再背着它 —— 把引用置空，
        队列里剩下的对象立刻变轻。

        安全性由两点保证：

        * **只在写成功之后调用**（见 `SinkWorker._flush`）——不存在
          "写失败还把取证数据丢了"；
        * **只在本次所有 sink 都会落 raw 时才启用**（`release_raw_after_write`）
          ——多 sink 并发消费同一批共享对象，不会出现"先跑完的把 raw 清掉、
          后跑的拿不到"。

        `Event` 的字段仍然存在、只是变 `None`，因此不破坏任何读取方：
        取证读的是库/文件里的 `raw_json`，它在释放之前就已经写完了。
        """
        released = 0
        for event in batch:
            if event.raw is not None:
                event.raw = None
                released += 1
        return released

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
