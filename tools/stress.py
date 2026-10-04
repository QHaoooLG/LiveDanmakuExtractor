"""压力测试：在高热度直播间上跑完整采集链路，量吞吐、背压与数据完整性。

需求方原话是"压测放到初版完全开发之后" —— 现在初版功能齐了（M0–M4），
而且拿到了一个高热度房间，所以补上这一环。

它复用 `grab` 的同一套组件（`RoomCollector` + `Bus` + 全部 sink），
但额外做三件 `grab` 不做的事：

1. **周期性采样**：收包速率、各 sink 队列深度/丢弃、内存占用；
2. **背压观察**：`queue_size` 调小后，sink 队列是否会顶满、是否真的丢最旧而不是阻塞读循环；
3. **数据完整性核对**：JSONL / CSV / TXT / SQLite 四种产物条数必须一致 ——
   压测最容易暴露的就是"落盘悄悄丢数据"。

用法（房间 7878 是实测高热度房间，真实房间号 23438368）:

    python tools/stress.py --room 7878 --duration 120
    python tools/stress.py --room 7878 --duration 60 --queue-size 200 --interval 5
    python tools/stress.py --room 7878 --duration 60 --out data_stress --keep-raw

退出码：0 正常 · 3 房间/接口问题 · 4 认证失败 · 5 网络不可用 · 1 其他错误
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bili_danmaku.api import BiliApi, BiliApiError  # noqa: E402
from bili_danmaku.client import AuthFailed  # noqa: E402
from bili_danmaku.collector import RoomCollector  # noqa: E402
from bili_danmaku.config import Config, ConfigError  # noqa: E402
from bili_danmaku.credentials import load_credentials  # noqa: E402
from bili_danmaku.sinks import Bus, build_sinks  # noqa: E402
from bili_danmaku.utils import setup_logging  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_ROOM, EXIT_AUTH, EXIT_NETWORK = 0, 1, 2, 3, 4, 5
FORMATS = ["jsonl", "csv", "txt", "sqlite"]


def rss_mb() -> float | None:
    """当前进程常驻内存（MB）。

    优先用 psutil；本机 pip 不可用且 PyPI 没有匹配的 psutil wheel（实测
    `wheel_fetch.py --packages psutil` 解析不到兼容产物），所以没有 psutil 时
    **返回 None 而不是编一个数**：内存这一行就显示"不可用"，
    排障时再 `pip install psutil` 即可。
    """
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:  # noqa: BLE001
        return None


class Sampler:
    """按固定间隔采样，并保存时序用于计算峰值/分位。"""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.samples: list[dict[str, Any]] = []

    async def run(self, collector: RoomCollector, bus: Bus, duration: float) -> None:
        # 速率必须用**同一个单调时钟**做基线：stats.started_at 是墙钟时间，
        # 拿 time.monotonic() 去减会得到几千万条/分钟这种荒唐数字（实测踩过）。
        baseline = time.monotonic()
        while time.monotonic() - baseline < duration:
            await asyncio.sleep(
                min(self.interval, max(duration - (time.monotonic() - baseline), 0.05))
            )
            stats = collector.stats
            elapsed = max(time.monotonic() - baseline, 0.001)
            queues = {
                worker.sink.name: {
                    "qsize": worker.queue.qsize(),
                    "dropped": worker.dropped,
                    "disabled": worker.disabled,
                    "written": worker.sink.stats.written,
                }
                for worker in bus.workers
            }
            sample = {
                "t": round(time.monotonic() - baseline, 1),
                "received": stats.received,
                "stored": stats.parsed,
                "filtered": stats.filtered,
                "duplicates": stats.duplicates,
                "rate_per_min": round(stats.received * 60.0 / elapsed, 1),
                "stored_rate_per_min": round(stats.parsed * 60.0 / elapsed, 1),
                "max_qsize": max((item["qsize"] for item in queues.values()), default=0),
                "dropped": sum(item["dropped"] for item in queues.values()),
                "written": sum(item["written"] for item in queues.values()),
                "rss_mb": rss_mb(),
                "queues": queues,
            }
            self.samples.append(sample)
            rss = f" RSS={sample['rss_mb']:.0f}MB" if sample["rss_mb"] else ""
            print(
                f"  t={sample['t']:>6.1f}s 收={sample['received']:>7} 入库={sample['stored']:>7} "
                f"过滤={sample['filtered']:>6} 收速率={sample['rate_per_min']:>8.1f}/min "
                f"队列峰值={sample['max_qsize']:>5} 丢弃={sample['dropped']:>4}{rss}",
                flush=True,
            )


# --------------------------------------------------------------------------- #
# 数据完整性核对
# --------------------------------------------------------------------------- #


def count_jsonl(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def count_csv(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8-sig") as handle:
        return max(sum(1 for line in handle if line.strip()) - 1, 0)  # 去表头


def count_txt(path: Path) -> int:
    """TXT 只写弹幕（且必须有文字），与 jsonl 条数天然不同，单独核对。"""
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def count_sqlite(path: Path) -> dict[str, int]:
    import sqlite3

    if not path.is_file():
        return {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        total = connection.execute("SELECT COUNT(*) FROM event").fetchone()[0]
        danmaku_with_text = connection.execute(
            "SELECT COUNT(*) FROM event WHERE type='danmaku' AND text IS NOT NULL AND TRIM(text) <> ''"
        ).fetchone()[0]
        by_type = dict(connection.execute("SELECT type, COUNT(*) FROM event GROUP BY type").fetchall())
        unique_keys = connection.execute(
            "SELECT COUNT(DISTINCT dedupe_key) FROM event"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "total": total,
        "danmaku_with_text": danmaku_with_text,
        "by_type": by_type,
        "unique_keys": unique_keys,
    }


def verify_outputs(out_dir: Path, room_id: int) -> dict[str, Any]:
    jsonl_files = sorted(out_dir.glob(f"{room_id}_*.jsonl"))
    csv_files = sorted(out_dir.glob(f"{room_id}_*.csv"))
    txt_files = sorted(out_dir.glob(f"{room_id}_*.txt"))

    jsonl = sum(count_jsonl(path) for path in jsonl_files)
    csv_rows = sum(count_csv(path) for path in csv_files)
    txt_lines = sum(count_txt(path) for path in txt_files)
    sqlite = count_sqlite(out_dir / "danmaku.db")

    return {
        "files": {
            "jsonl": [path.name for path in jsonl_files],
            "csv": [path.name for path in csv_files],
            "txt": [path.name for path in txt_files],
        },
        "jsonl": jsonl,
        "csv": csv_rows,
        "txt": txt_lines,
        "sqlite": sqlite,
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


async def run(args: argparse.Namespace) -> int:
    credentials = load_credentials(args.cookie_file)
    if not credentials.logged_in:
        print("未登录：先执行 bili-danmaku login（匿名下更易触发风控）", file=sys.stderr)
        return EXIT_AUTH

    # 只覆盖用户显式给出的项：把 None 传进 overrides 会让 pydantic 校验失败
    #
    # 输出目录默认按"房间+时间戳"分目录：JSONL/CSV/TXT 是**追加**写、
    # SQLite 是共用库，复用同一个目录会把上一轮的产物算进本轮核对里
    # （实测：单轮写入 155 条，却因为目录里已有 242 条而报出 397 条）。
    out_dir = args.out or str(
        Path("data_stress") / f"{args.room}_{time.strftime('%Y%m%d-%H%M%S')}"
    )
    sink_overrides: dict[str, Any] = {
        "types": FORMATS,
        "out_dir": out_dir,
        "keep_raw": args.keep_raw,
    }
    for key, value in (
        ("queue_size", args.queue_size),
        ("batch_size", args.batch_size),
        ("flush_interval", args.flush_interval),
    ):
        if value is not None:
            sink_overrides[key] = value

    overrides: dict[str, Any] = {
        "general": {"content_only": not args.all_events},
        "sinks": sink_overrides,
    }
    config = Config.load(args.config, overrides=overrides)
    setup_logging("WARNING")

    out_dir = Path(config.sinks.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=== 压测 ===")
    print(f"房间            {args.room}")
    print(f"时长            {args.duration:.0f}s（采样间隔 {args.interval:.0f}s）")
    print(f"输出目录        {out_dir}")
    print(f"落盘格式        {', '.join(config.sinks.types)}")
    print(f"队列容量/批大小 {config.sinks.queue_size} / {config.sinks.batch_size}")
    print(f"采集范围        {'全部事件' if args.all_events else '仅文字弹幕 / SC'}")
    print(f"保留 raw        {config.sinks.keep_raw}")
    print("")

    sinks = build_sinks(list(config.sinks.types), config)
    bus = Bus(sinks, config)
    sampler = Sampler(args.interval)
    started_wall = time.time()

    async with BiliApi(config, credentials) as api:
        room = await api.room_init(args.room)
        collector = RoomCollector(
            args.room,
            config,
            credentials,
            api,
            bus,
            seed_history=not args.no_history,
            anonymize=False,
        )
        deadline = time.monotonic() + args.duration
        collector_task = asyncio.create_task(
            collector.run(deadline_monotonic=deadline), name="stress-collector"
        )
        sampler_task = asyncio.create_task(
            sampler.run(collector, bus, args.duration), name="stress-sampler"
        )
        try:
            stats = await collector_task
        except asyncio.CancelledError:
            raise
        finally:
            sampler_task.cancel()
            await asyncio.gather(sampler_task, return_exceptions=True)

    elapsed = max(time.time() - started_wall, 0.001)
    print("\n=== 采集统计 ===")
    for key, value in stats.as_dict().items():
        print(f"  {key:<18} {value}")

    print("\n=== Sink 统计 ===")
    for name, item in bus.stats.items():
        print(f"  {name:<8} 写入={item.written:<7} 丢弃={item.dropped:<5} 失败={item.failed} ok={item.ok}")

    print("\n=== 队列/内存峰值 ===")
    if sampler.samples:
        peak_q = max(sample["max_qsize"] for sample in sampler.samples)
        peak_written = max(sample["written"] for sample in sampler.samples)
        rates = [sample["rate_per_min"] for sample in sampler.samples]
        print(f"  队列峰值        {peak_q} / {config.sinks.queue_size}")
        print(f"  已写入（峰值）  {peak_written}")
        print(f"  总丢弃          {max(sample['dropped'] for sample in sampler.samples)}")
        print(f"  速率中位数      {statistics.median(rates):.1f} 条/分钟")
        print(f"  速率峰值        {max(rates):.1f} 条/分钟")
        peak_rss = max((sample["rss_mb"] or 0) for sample in sampler.samples)
        if peak_rss:
            print(f"  RSS 峰值        {peak_rss:.0f} MB")
        else:
            print("  RSS 峰值        （不可用：未安装 psutil）")

    print("\n=== 产物核对 ===")
    verified = verify_outputs(out_dir, room.room_id)
    print(f"  目录            {out_dir}")
    print(f"  文件            {verified['files']}")
    print(f"  JSONL           {verified['jsonl']}")
    print(f"  CSV（去表头）   {verified['csv']}")
    print(f"  TXT（仅弹幕）   {verified['txt']}")
    print(f"  SQLite          {verified['sqlite'].get('total', 0)}")
    print(f"  SQLite 类型分布 {verified['sqlite'].get('by_type', {})}")

    problems: list[str] = []
    if verified["jsonl"] != verified["csv"]:
        problems.append(f"JSONL({verified['jsonl']}) 与 CSV({verified['csv']}) 条数不一致")
    if verified["jsonl"] != verified["sqlite"].get("total", 0):
        problems.append(
            f"JSONL({verified['jsonl']}) 与 SQLite({verified['sqlite'].get('total', 0)}) 条数不一致"
        )
    if verified["sqlite"].get("total") != verified["sqlite"].get("unique_keys"):
        problems.append("SQLite 里有重复 dedupe_key")
    if verified["txt"] != verified["sqlite"].get("danmaku_with_text", 0):
        problems.append(
            f"TXT({verified['txt']}) 与 SQLite 有文字的弹幕数"
            f"({verified['sqlite'].get('danmaku_with_text', 0)}) 不一致"
        )
    if stats.dropped:
        # 注意措辞：读循环并没有被拖慢（心跳/重连都正常），丢是因为
        # sink 队列容量小于"生产速率 − 消费速率"。把 queue_size 调大即可缓解。
        problems.append(
            f"因队列容量不足丢弃 {stats.dropped} 条（sink 跟不上生产速率；"
            f"queue_size={config.sinks.queue_size}，读循环本身未受影响）"
        )
    for name, item in bus.stats.items():
        if item.dropped:
            problems.append(f"sink {name} 丢弃 {item.dropped} 条（队列顶满，丢最旧）")
        if not item.ok:
            problems.append(f"sink {name} 写入失败: {item.error}")
    if stats.reconnects:
        print(f"\n注意：期间重连 {stats.reconnects} 次（P50/P95 与速率会受影响）")

    print("\n=== 结论 ===")
    if problems:
        for item in problems:
            print(f"  [!] {item}")
        return EXIT_ERROR
    per_run = verified["jsonl"]
    print(f"  四种产物条数一致（{per_run} 条），无丢弃、无写入失败")
    # 速率要用**采集器自己的窗口**算：elapsed 含建连/鉴权/补种前的准备时间，
    # 而且 stats.received 里已经有历史补种那几条（seeded），不能算进实时吞吐。
    window = stats.ended_at - stats.started_at if stats.ended_at else 0.0
    live_received = max(stats.received - stats.seeded, 0)
    if window > 0:
        print(f"  链路吞吐      {live_received / window * 60:.0f} 条/分钟（实时收包，窗口 {window:.0f}s）")
        print(f"  入库吞吐      {per_run / window * 60:.0f} 条/分钟（过滤后落盘）")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="高热度直播间压力测试")
    parser.add_argument("--room", required=True, help="房间号（支持短号）")
    parser.add_argument("--duration", type=float, default=120.0, help="采集秒数（默认 120）")
    parser.add_argument("--interval", type=float, default=10.0, help="采样间隔秒数（默认 10）")
    parser.add_argument("--out", default=None, help="输出目录（默认 data_stress/<房间>_<时间戳>）")
    parser.add_argument("--queue-size", type=int, default=None, help="sink 队列容量（调小可施加背压）")
    parser.add_argument("--batch-size", type=int, default=None, help="sink 批大小")
    parser.add_argument("--flush-interval", type=float, default=None, help="sink 刷盘间隔（秒）")
    parser.add_argument("--config", default=None, help="TOML 配置文件路径")
    parser.add_argument("--cookie-file", default=None, help="凭据文件路径")
    parser.add_argument("--keep-raw", action="store_true", help="保留原始消息（体积约 2-3 倍）")
    parser.add_argument("--all-events", action="store_true", help="连进房/礼物/点赞一起落盘")
    parser.add_argument("--no-history", action="store_true", help="不补种历史弹幕")
    args = parser.parse_args()

    if args.queue_size is not None and args.queue_size < 1:
        print("--queue-size 必须 >= 1", file=sys.stderr)
        return EXIT_CONFIG

    try:
        return asyncio.run(run(args))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AuthFailed as exc:
        print(f"认证失败: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except BiliApiError as exc:
        print(f"接口失败: {exc}", file=sys.stderr)
        return EXIT_ROOM
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_OK
    except OSError as exc:
        print(f"网络不可用: {exc}", file=sys.stderr)
        return EXIT_NETWORK


if __name__ == "__main__":
    raise SystemExit(main())
