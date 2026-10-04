"""命令行入口。"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import click

from . import __version__
from .api import BiliApi, BiliApiError
from .client import AuthFailed, probe
from .collector import RoomCollector
from .config import Config, ConfigError
from .credentials import (
    Credentials,
    LoginError,
    load_credentials,
    purge_credentials,
    qr_login,
    save_credentials,
)
from .filters import FilterError, FilterSpec, build_filter
from .models import Event
from .parser import parse_message
from .rooms import parse_room_input
from .sinks import CSV_COLUMNS, Bus, CsvSink, SinkUnavailable, build_sinks
from .store import DanmakuStore, StoreError
from .utils import setup_logging

log = logging.getLogger("bili_danmaku.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_ROOM = 3
EXIT_AUTH = 4
EXIT_NETWORK = 5


# --------------------------------------------------------------------------- #
# 公共辅助
# --------------------------------------------------------------------------- #


def safe_echo(message: str, **kwargs: Any) -> None:
    """打印一行文本，**容忍控制台编码不是 UTF-8**。

    为什么需要它：Windows 的默认控制台代码页是 GBK，`click.echo("✅ …")` 会抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'`。
    打包成 **`--windowed`** exe 后这个异常更致命 —— 没有控制台可看错误，
    进程直接带异常退出。

    降级策略：**只把个别无法编码的字符换成 `?`**，其余（中文、路径、数字）原样保留。
    （曾用 `encode("ascii", "replace")` 整行降级 —— 那会把中文也一起吃掉，
    所以改成逐字符处理。`PYTHONIOENCODING=utf-8` 时行为完全不变。）
    """
    try:
        click.echo(message, **kwargs)
        return
    except UnicodeEncodeError:
        pass

    encoding = getattr(getattr(sys, "stdout", None), "encoding", None) or "utf-8"
    cleaned_parts: list[str] = []
    for char in message:
        try:
            char.encode(encoding)
            cleaned_parts.append(char)
        except UnicodeEncodeError:
            cleaned_parts.append("?")
    click.echo("".join(cleaned_parts), **kwargs)


def _build_config(ctx: click.Context, overrides: dict[str, Any] | None = None) -> Config:
    overrides = dict(overrides or {})
    level = (ctx.obj or {}).get("level")
    if level:
        overrides.setdefault("general", {})["log_level"] = level
    try:
        config = Config.load((ctx.obj or {}).get("config_path"), overrides=overrides or None)
    except ConfigError as exc:
        click.secho(f"配置错误: {exc}", fg="red", err=True)
        raise SystemExit(EXIT_CONFIG) from exc
    setup_logging(config.general.log_level)
    return config


def _load_creds(config: Config, cookie_file: str | None) -> Credentials:
    return load_credentials(cookie_file or config.api.cookie_file, config.api.cookie)


async def _check_auth(config: Config, creds: Credentials) -> tuple[bool, dict[str, Any]]:
    async with BiliApi(config, creds) as api:
        result = await api.nav()
    if result.code == 0 and (result.data or {}).get("isLogin"):
        return True, result.data
    return False, {"code": result.code, "message": result.message}


async def _ensure_buvid(config: Config, creds: Credentials) -> Credentials:
    """缺少 buvid3 时自动获取并附加（提升连接稳定性）。"""
    if creds.buvid3:
        return creds
    async with BiliApi(config, creds) as api:
        buvid3, buvid4 = await api.fingerprint()
    if buvid3:
        log.debug("已获取设备指纹 buvid3")
    return creds.with_buvid(buvid3, buvid4)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@click.command("tray")
@click.option("--rooms", default=None, help="预置房间号（逗号分隔；留空则启动空面板）")
@click.option("--port", type=int, default=None, help="监听端口（默认取配置）")
@click.option("--out", "out_dir", default=None, help="落盘目录")
@click.option("--no-open", is_flag=True, help="不自动打开浏览器")
@click.option(
    "--no-start", is_flag=True,
    help="只起托盘不启动采集（双击 exe 的默认行为；见 DEC-6/D4）",
)
@click.pass_context
def tray_command(
    ctx: click.Context,
    rooms: str | None,
    port: int | None,
    out_dir: str | None,
    no_open: bool,
    no_start: bool,
) -> None:
    """启动托盘应用（面板跑在本进程内）。"""
    from .gui.tray import run_tray

    # `auto_start` 与 `open_browser` 是**两个独立开关**（曾经合并过，是 bug：
    # `--no-open` 会连面板都不启动）。
    raise SystemExit(
        run_tray(
            rooms=tuple(item.strip() for item in (rooms or "").split(",") if item.strip()),
            port=port,
            out_dir=out_dir,
            open_browser=not no_open,
            auto_start=not no_start,
        )
    )


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(__version__, "-V", "--version", prog_name="bili-danmaku")
@click.option(
    "--config", "config_path", type=click.Path(dir_okay=False), default=None,
    help="TOML 配置文件路径（默认 ./config.toml）",
)
@click.option("-v", "--verbose", count=True, help="增加日志详细度（-v = DEBUG）")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: int) -> None:
    """B 站直播弹幕提取器。

    凭据只保存在本机 .secrets/ 且不进日志

    **不带子命令时**：打包成 exe（`sys.frozen`）就直接启动**托盘应用**
    （用户双击 exe 的预期行为）；从源码/命令行跑则打印帮助，避免误操作。
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["level"] = "DEBUG" if verbose else None

    if ctx.invoked_subcommand is None:
        if getattr(sys, "frozen", False):
            # 双击 exe：只起托盘 + 显示启动面板，**不自动开始采集**
            # （房间号未定时不该猜）—— 见 DEC-6 / D4。
            ctx.invoke(
                tray_command, rooms=None, port=None, out_dir=None,
                no_open=True, no_start=True,
            )
            ctx.exit(0)
        # 命令行下不带子命令：显示帮助（而不是静默什么都不做）
        click.echo(ctx.get_help())


# --------------------------------------------------------------------------- #


@main.command()
@click.option("--manual", is_flag=True, help="改为手工输入 SESSDATA（不回显，不推荐）")
@click.option("--cookie-file", type=click.Path(dir_okay=False), default=None, help="凭据保存路径")
@click.option("--no-save", is_flag=True, help="仅校验，不写入磁盘")
@click.pass_context
def login(ctx: click.Context, manual: bool, cookie_file: str | None, no_save: bool) -> None:
    """扫码登录并保存凭据。"""
    config = _build_config(ctx)
    target = Path(cookie_file or config.api.cookie_file)

    if manual:
        import getpass

        sessdata = getpass.getpass("请输入 SESSDATA（输入不回显）: ").strip()
        if not sessdata:
            click.secho("未输入内容，已取消。", fg="red", err=True)
            raise SystemExit(EXIT_AUTH)
        creds = Credentials.from_cookie_string(f"SESSDATA={sessdata}", source="inline")
    else:
        try:
            creds = asyncio.run(qr_login())
        except LoginError as exc:
            click.secho(f"登录失败: {exc}", fg="red", err=True)
            raise SystemExit(EXIT_AUTH) from exc

    ok, info = asyncio.run(_check_auth(config, creds))
    if not ok:
        click.secho(
            f"凭据校验失败: code={info.get('code')} {info.get('message')}", fg="red", err=True
        )
        raise SystemExit(EXIT_AUTH)

    # 仅用于确认登录的是哪个账号；不会被写入磁盘
    click.secho(
        f"登录有效: {info.get('uname')} (mid={info.get('mid')})",
        fg="green",
    )

    if no_save:
        click.echo("--no-save 已指定，凭据未写入磁盘。")
        return

    path = save_credentials(creds, target)
    click.echo(f"凭据已保存到 {path}（仅含连接所需 cookie，已收紧文件权限）")
    click.secho("如需删除：bili-danmaku purge", fg="bright_black")


# --------------------------------------------------------------------------- #


@main.command("check-auth")
@click.option("--cookie-file", type=click.Path(dir_okay=False), default=None, help="凭据文件路径")
@click.pass_context
def check_auth_cmd(ctx: click.Context, cookie_file: str | None) -> None:
    """校验当前凭据是否有效。"""
    config = _build_config(ctx)
    creds = _load_creds(config, cookie_file)
    click.echo(f"凭据来源: {creds.describe()}")

    if not creds.logged_in:
        click.secho("未找到可用凭据。请执行 bili-danmaku login", fg="yellow", err=True)
        raise SystemExit(EXIT_AUTH)

    ok, info = asyncio.run(_check_auth(config, creds))
    if ok:
        click.secho(f"凭据有效: {info.get('uname')} (mid={info.get('mid')})", fg="green")
        raise SystemExit(EXIT_OK)

    code = info.get("code")
    if code == -101:
        click.secho("凭据已失效（账号未登录），请重新执行 bili-danmaku login", fg="red", err=True)
    elif code == -352:
        click.secho("触发风控（-352），请稍后重试并降低请求频率", fg="red", err=True)
    else:
        click.secho(f"校验失败: code={code} {info.get('message')}", fg="red", err=True)
    raise SystemExit(EXIT_AUTH)


# --------------------------------------------------------------------------- #


@main.command()
@click.option("--cookie-file", type=click.Path(dir_okay=False), default=None, help="凭据文件路径")
@click.option("--yes", is_flag=True, help="跳过确认")
@click.pass_context
def purge(ctx: click.Context, cookie_file: str | None, yes: bool) -> None:
    """删除本机保存的凭据。"""
    config = _build_config(ctx)
    target = cookie_file or config.api.cookie_file
    if not yes:
        click.confirm(f"确认删除凭据文件 {target} ？", abort=True)
    removed = purge_credentials(target)
    if removed:
        for path in removed:
            click.echo(f"已删除 {path}")
    else:
        click.echo("未找到凭据文件，无需删除。")


# --------------------------------------------------------------------------- #


@main.command()
@click.argument("room")
@click.option("--seconds", type=float, default=30.0, show_default=True, help="采样时长（秒）")
@click.option("--messages", "max_messages", type=int, default=10, show_default=True, help="最多打印多少条")
@click.option("--dump-raw", type=click.Path(file_okay=False), default=None, help="把原始帧与消息落盘到该目录")
@click.option("--cookie-file", type=click.Path(dir_okay=False), default=None, help="凭据文件路径")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出结果")
@click.pass_context
def probe(
    ctx: click.Context,
    room: str,
    seconds: float,
    max_messages: int,
    dump_raw: str | None,
    cookie_file: str | None,
    as_json: bool,
) -> None:
    """连通性与协议自检：房间解析 → token → 握手 → 心跳 → 采样。"""
    config = _build_config(ctx)
    try:
        report = asyncio.run(
            _run_probe(config, room, seconds, max_messages, dump_raw, cookie_file)
        )
    except BiliApiError as exc:
        click.secho(f"接口失败: {exc}", fg="red", err=True)
        raise SystemExit(EXIT_ROOM) from exc
    except AuthFailed as exc:
        click.secho(f"认证失败: {exc}", fg="red", err=True)
        raise SystemExit(EXIT_AUTH) from exc
    except OSError as exc:
        click.secho(f"网络错误，所有节点均不可用: {exc}", fg="red", err=True)
        raise SystemExit(EXIT_NETWORK) from exc

    if as_json:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return

    _print_probe_report(report, max_messages)
    raise SystemExit(EXIT_OK)


async def _run_probe(
    config: Config,
    room: str,
    seconds: float,
    max_messages: int,
    dump_raw: str | None,
    cookie_file: str | None,
) -> dict[str, Any]:
    creds = _load_creds(config, cookie_file)
    print(f"[1/6] 凭据: {creds.describe()}")

    creds = await _ensure_buvid(config, creds)

    async with BiliApi(config, creds) as api:
        print(f"[2/6] 解析房间 {room} …")
        room_info = await api.room_init(parse_room_input(room))
        print(
            f"      真实房间号={room_info.room_id} 主播uid={room_info.anchor_uid} "
            f"live_status={room_info.live_status}"
            f" ({'直播中' if room_info.is_living else '未开播/轮播'})"
        )

        print("[3/6] 获取 WS 节点与 token …")
        bundle = await api.acquire_token(room_info.room_id)
        print(f"      token 来源={bundle.source} 节点数={len(bundle.hosts)}")
        for host in bundle.hosts[:3]:
            print(f"      · {host.url()}")

        print("[4/6] 建立 WebSocket 连接、认证并采样 …")
        from .client import probe as probe_session

        connection, collected = await probe_session(
            room=room_info,
            bundle=bundle,
            credentials=creds,
            config=config,
            seconds=seconds,
            max_messages=max_messages,
            capture_raw=dump_raw is not None,
        )
        try:
            auth_reply = connection.auth_reply
            print(
                f"      认证回应={auth_reply}  协议版本 protover={connection.protover}"
            )

            stats = connection.stats
            print(f"[5/6] 心跳与采样完成：消息 {len(collected)} 条，收包 {stats.packets} 个")
            if stats.last_popularity is not None:
                print(f"      人气值={stats.last_popularity}")

            report: dict[str, Any] = {
                "credentials": creds.describe(),
                "room": room_info.model_dump(),
                "token_source": bundle.source,
                "hosts": [h.url() for h in bundle.hosts],
                "protover": connection.protover,
                "auth_reply": auth_reply,
                "popularity": stats.last_popularity,
                "counters": {
                    "messages": len(collected),
                    "packets": stats.packets,
                    "unknown_protover": stats.unknown_protover,
                    "bad_json": stats.bad_json,
                    "protocol_errors": stats.protocol_errors,
                },
                "cmd_histogram": dict(
                    Counter(str(m.get("cmd", "")).split(":")[0] for m in collected)
                ),
                "parsed_events": [
                    event.model_dump(exclude={"raw"})
                    for event in (
                        parse_message(m, room_info, 0.0, keep_raw=False) for m in collected
                    )
                    if event is not None
                ],
                "messages": collected if dump_raw else collected[:max_messages],
            }

            if dump_raw:
                frames = _dump_fixtures(Path(dump_raw), connection.raw_frames, collected)
                report["dumped"] = frames
                print(f"      夹具已写入 {dump_raw}（{len(frames)} 个文件）")

            return report
        finally:
            await connection.close()


def _dump_fixtures(directory: Path, frames: list[bytes], messages: list[dict[str, Any]]) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    packets_dir = directory / "packets"
    packets_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for index, frame in enumerate(frames):
        path = packets_dir / f"frame_{index:03d}.bin"
        path.write_bytes(frame)
        written.append(str(path))

    (directory / "messages.json").write_text(
        json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    written.append(str(directory / "messages.json"))

    danmu = [m for m in messages if str(m.get("cmd", "")).startswith("DANMU_MSG")]
    if danmu:
        (directory / "danmu_msg_samples.json").write_text(
            json.dumps(danmu, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(str(directory / "danmu_msg_samples.json"))

    return written


def _print_probe_report(report: dict[str, Any], max_messages: int) -> None:
    click.echo("")
    click.secho("=== 自检报告 ===", bold=True)
    room = report.get("room") or {}
    click.echo(
        f"房间      {room.get('room_id')} (主播 uid={room.get('anchor_uid')}, "
        f"live_status={room.get('live_status')})"
    )
    click.echo(f"凭据      {report.get('credentials')}")
    click.echo(f"token来源 {report.get('token_source')}")
    click.echo(f"protover  {report.get('protover')}")
    click.echo(f"认证回应  {report.get('auth_reply')}")
    click.echo(f"人气值    {report.get('popularity')}")

    counters = report.get("counters") or {}
    click.echo(
        f"计数器    消息={counters.get('messages')} 收包={counters.get('packets')} "
        f"未知protover={counters.get('unknown_protover')} "
        f"坏JSON={counters.get('bad_json')} 协议错误={counters.get('protocol_errors')}"
    )

    histogram = report.get("cmd_histogram") or {}
    if histogram:
        click.secho("\n-- cmd 分布 --", bold=True)
        for cmd, count in sorted(histogram.items(), key=lambda kv: -kv[1]):
            click.echo(f"  {cmd:36s} {count}")

    events = report.get("parsed_events") or []
    if events:
        click.secho(f"\n-- 归一化样例（最多 {max_messages} 条，已隐去 raw） --", bold=True)
        for event in events[:max_messages]:
            click.echo(
                f"  [{event.get('type'):14s}] uid={event.get('uid')} "
                f"name={event.get('uname')!r} text={event.get('text')!r} "
                f"src={event.get('field_source')}"
            )


# --------------------------------------------------------------------------- #
# grab
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_DURATION_UNITS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str) -> float:
    """解析 '30s' / '5m' / '1h' / '90'（纯数字视为秒）。"""
    match = _DURATION_RE.match(text or "")
    if not match:
        raise click.BadParameter(f"无法解析时长 '{text}'，示例: 30s / 5m / 1h")
    return float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]


def _resolve_deadline(duration: str, until: str | None) -> float | None:
    """返回 monotonic 截止时刻；None 表示不限期。"""
    if until:
        match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", until)
        if not match:
            raise click.BadParameter(f"无法解析时刻 '{until}'，示例: 23:00")
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise click.BadParameter(f"时刻超出范围: {until}")

        local = time.localtime()
        target = time.mktime(
            (local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1)
        )
        seconds = target - time.time()
        if seconds <= 0:
            raise click.BadParameter(f"时刻 {until} 已过去，请指定今天之后的时刻")
        return time.monotonic() + seconds

    seconds = parse_duration(duration)
    return None if seconds <= 0 else time.monotonic() + seconds


@main.command()
@click.argument("room")
@click.option("--duration", default="5m", show_default=True, help="采集时长：30s / 5m / 1h，0 表示不限期")
@click.option("--until", default=None, help="采集到当天指定时刻，如 23:00（与 --duration 二选一）")
@click.option("--format", "formats", default=None, help="落盘格式，逗号分隔：jsonl,csv,txt,sqlite（默认取配置）")
@click.option("--out", "out_dir", default=None, help="输出目录（覆盖配置）")
@click.option("--db", "sqlite_path", default=None, help="SQLite 文件路径（默认 {out}/danmaku.db）")
@click.option("--raw/--no-raw", "keep_raw", default=None, help="是否保留原始消息（默认保留）")
@click.option("--anonymize", is_flag=True, help="脱敏输出：uid 加盐哈希、昵称哈希化")
@click.option("--history/--no-history", "seed_history", default=False, help="启动时补种最近历史弹幕")
@click.option("--types", "types", default=None, help="只要这些类型，逗号分隔（如 danmaku,gift,super_chat）")
@click.option("--keyword", "keywords", multiple=True, help="关键词，可重复；'re:' 前缀走正则")
@click.option(
    "--keyword-mode", type=click.Choice(["or", "and"]), default="or", show_default=True,
    help="多关键词的组合方式",
)
@click.option("--uid", "include_uids", multiple=True, type=int, help="只看这些 uid，可重复")
@click.option("--exclude-uid", "exclude_uids", multiple=True, type=int, help="排除这些 uid，可重复")
@click.option("--min-price", type=int, default=None, help="最低金额（只对礼物/SC/上舰生效）")
@click.option("--min-medal-level", type=int, default=None, help="最低粉丝牌等级")
@click.option("--exclude-masked", is_flag=True, help="丢弃被平台打码的身份（uid=0 或昵称含 ***）")
@click.option(
    "--content-only/--all-events",
    "content_only",
    default=None,
    help="只采集有文字的弹幕与 SC（默认）；--all-events 会连同进房/上舰/点赞等一并落盘",
)
@click.option("--cookie-file", type=click.Path(dir_okay=False), default=None, help="凭据文件路径")
@click.pass_context
def grab(
    ctx: click.Context,
    room: str,
    duration: str,
    until: str | None,
    formats: str | None,
    out_dir: str | None,
    sqlite_path: str | None,
    keep_raw: bool | None,
    anonymize: bool,
    seed_history: bool,
    types: str | None,
    keywords: tuple[str, ...],
    keyword_mode: str,
    include_uids: tuple[int, ...],
    exclude_uids: tuple[int, ...],
    min_price: int | None,
    min_medal_level: int | None,
    exclude_masked: bool,
    content_only: bool | None,
    cookie_file: str | None,
) -> None:
    """抓取直播间并落盘。Ctrl+C 可随时停止并正常收尾。

    默认只采集**有用户文字内容**的弹幕与 SC；进房 / 上舰 / 点赞 / 礼物 /
    房间数据等不产生文字的事件会被过滤（统计中的「过滤」计数即是它们）。
    """
    overrides: dict[str, Any] = {}
    if out_dir:
        overrides.setdefault("sinks", {})["out_dir"] = out_dir
    if sqlite_path:
        overrides.setdefault("sinks", {})["sqlite_path"] = sqlite_path
    if keep_raw is not None:
        overrides.setdefault("sinks", {})["keep_raw"] = keep_raw
    if anonymize:
        overrides.setdefault("general", {})["anonymize"] = True
    if content_only is not None:
        overrides.setdefault("general", {})["content_only"] = content_only

    config = _build_config(ctx, overrides or None)
    deadline = _resolve_deadline(duration, until)
    format_list = (
        [item.strip() for item in formats.split(",") if item.strip()]
        if formats
        else list(config.sinks.types)
    )
    type_list = [item.strip() for item in types.split(",") if item.strip()] if types else None
    filters = _make_filter(
        types=type_list,
        keywords=keywords,
        keyword_mode=keyword_mode,
        include_uids=include_uids,
        exclude_uids=exclude_uids,
        min_price=min_price,
        min_medal_level=min_medal_level,
        exclude_masked=exclude_masked,
        content_only=config.general.content_only,
    )

    holder: list[RoomCollector] = []
    interrupted = False
    try:
        stats = asyncio.run(
            _run_grab(
                config, room, deadline, format_list, seed_history, cookie_file, holder, filters
            )
        )
    except SinkUnavailable as exc:
        click.secho(f"落盘格式不可用: {exc}", fg="red", err=True)
        raise SystemExit(EXIT_CONFIG) from exc
    except BiliApiError as exc:
        click.secho(f"接口失败: {exc}", fg="red", err=True)
        raise SystemExit(EXIT_ROOM) from exc
    except KeyboardInterrupt:
        interrupted = True
        click.echo("")
        click.secho("收到中断信号，已停止采集并完成收尾。", fg="yellow")
        stats = holder[0].stats if holder else None

    if stats is None:
        raise SystemExit(EXIT_ERROR)

    _print_grab_report(stats, config, room, interrupted=interrupted, filters=filters)
    raise SystemExit(EXIT_OK)


def _make_filter(**kwargs: Any) -> FilterSpec:
    """构造过滤规则，把条件错误转成 CLI 参数错误。"""
    try:
        return build_filter(**kwargs)
    except FilterError as exc:
        raise click.BadParameter(str(exc)) from exc


async def _run_grab(
    config: Config,
    room: str,
    deadline: float | None,
    formats: list[str],
    seed_history: bool,
    cookie_file: str | None,
    holder: list[RoomCollector],
    filters: FilterSpec | None = None,
):
    creds = _load_creds(config, cookie_file)
    creds = await _ensure_buvid(config, creds)

    sinks = build_sinks(formats, config)
    bus = Bus(sinks, config)

    async with BiliApi(config, creds) as api:
        collector = RoomCollector(
            room,
            config,
            creds,
            api,
            bus,
            filters=filters,
            seed_history=seed_history,
            anonymize=config.general.anonymize,
        )
        holder.append(collector)
        return await collector.run(deadline_monotonic=deadline)

def _print_grab_report(
    stats: Any,
    config: Config,
    room: str,
    *,
    interrupted: bool = False,
    filters: FilterSpec | None = None,
) -> None:
    click.echo("")
    click.secho("=== 采集报告 ===", bold=True)
    click.echo(f"房间        {room}")
    click.echo(f"结果        {'被中断（数据已完整刷盘）' if interrupted else '正常结束'}")
    click.echo(f"过滤        {filters.describe() if filters else '无过滤'}")
    click.echo(
        f"时长        {stats.as_dict().get('duration_s', '?')} 秒"
        f"　收包 {stats.received}　入库 {stats.parsed}"
    )
    click.echo(
        f"丢弃        重复 {stats.duplicates}　过滤 {stats.filtered}　"
        f"坏消息 {stats.bad_messages}　背压 {stats.dropped}"
    )
    click.echo(
        f"连接        重连 {stats.reconnects} 次　认证失败 {stats.auth_failures} 次　"
        f"人气值 {stats.last_popularity}"
    )
    if stats.seeded:
        click.echo(f"历史补种    {stats.seeded} 条")
    if stats.by_type:
        click.secho("\n类型分布", bold=True)
        for name, count in sorted(stats.by_type.items(), key=lambda kv: -kv[1]):
            click.echo(f"  {name:16s} {count}")
    click.secho("\n落盘", bold=True)
    click.echo(f"  输出目录  {config.sinks.out_dir}")
    click.echo(f"  保留 raw  {'是' if config.sinks.keep_raw else '否'}")


# --------------------------------------------------------------------------- #
# stats / export / replay
# --------------------------------------------------------------------------- #


def _default_db_path(config: Config) -> Path:
    configured = (config.sinks.sqlite_path or "").strip()
    if configured:
        return Path(configured)
    return Path(config.sinks.out_dir) / "danmaku.db"


def _open_store(config: Config, db: str | None) -> DanmakuStore:
    try:
        return DanmakuStore(db or _default_db_path(config))
    except StoreError as exc:
        click.secho(f"无法读取数据库: {exc}", fg="red", err=True)
        raise SystemExit(EXIT_ERROR) from exc


def _parse_time(value: str | None) -> float | None:
    """接受 unix 时间戳或 `YYYY-MM-DD[ HH:MM[:SS]]`。"""
    if not value:
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    raise click.BadParameter(f"无法解析时间 {value!r}，示例: 1789710200 或 '2026-09-18 14:00:00'")


def _scope_options(func):
    """给命令加上 --db / --session / --room 三个通用选项。"""
    func = click.option("--db", type=click.Path(dir_okay=False), default=None, help="SQLite 文件路径")(func)
    func = click.option("--session", "session_id", default=None, help="限定会话 id")(func)
    func = click.option("--room", "room_id", type=int, default=None, help="限定房间号")(func)
    return func


@main.command()
@_scope_options
@click.option("--top", type=int, default=10, show_default=True, help="榜单条数")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出")
@click.pass_context
def stats(
    ctx: click.Context,
    db: str | None,
    session_id: str | None,
    room_id: int | None,
    top: int,
    as_json: bool,
) -> None:
    """统计已采集的数据（从 SQLite 读取）。"""
    config = _build_config(ctx)
    scope: dict[str, Any] = {"session_id": session_id, "room_id": room_id}

    store = _open_store(config, db)
    try:
        total = store.count(**scope)
        if total == 0:
            click.secho("该范围内没有数据。请先执行 bili-danmaku grab 并用 sqlite 落盘。", fg="yellow")
            raise SystemExit(EXIT_OK)
        low, high = store.time_range(**scope)
        p50, p95 = store.latency_percentiles(**scope)
        report: dict[str, Any] = {
            "db": str(store.path),
            "total": total,
            "first_ts": low,
            "last_ts": high,
            "span_seconds": round(high - low, 1) if low and high else None,
            "by_type": store.by_type(**scope),
            "by_source": store.by_source(**scope),
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "top_senders": store.top_senders(limit=top, **scope),
            "priced": store.top_gifts(limit=top, **scope),
            "sessions": store.sessions(limit=20),
        }
    finally:
        store.close()

    if as_json:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(EXIT_OK)

    _print_stats(report)
    raise SystemExit(EXIT_OK)


def _print_stats(report: dict[str, Any]) -> None:
    click.echo("")
    click.secho("=== 数据统计 ===", bold=True)
    click.echo(f"数据库      {report['db']}")
    click.echo(f"总条数      {report['total']}")
    if report.get("span_seconds") is not None:
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report["first_ts"]))
        end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report["last_ts"]))
        click.echo(f"时间范围    {start} → {end}（{report['span_seconds']} 秒）")
    click.echo(
        f"链路延迟    P50 {report['latency_p50_ms']} ms　P95 {report['latency_p95_ms']} ms"
    )

    click.secho("\n类型分布", bold=True)
    for name, count in (report.get("by_type") or {}).items():
        click.echo(f"  {name:16s} {count}")
    if report.get("by_source"):
        click.secho("\n数据来源", bold=True)
        for name, count in report["by_source"].items():
            click.echo(f"  {name:16s} {count}")

    if report.get("top_senders"):
        click.secho("\n发言 TOP", bold=True)
        for index, item in enumerate(report["top_senders"], 1):
            click.echo(f"  {index:2d}. {item['uname'] or '匿名':24s} {item['count']:6d} 条")

    if report.get("priced"):
        click.secho("\n金额汇总（按类型，单位见采集时的 price_unit）", bold=True)
        for item in report["priced"]:
            click.echo(f"  {item['type']:16s} 合计 {item['total_price']:>10}　{item['count']} 笔")

    if report.get("sessions"):
        click.secho("\n最近会话", bold=True)
        for item in report["sessions"][:10]:
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["started_at"]))
            click.echo(f"  {item['id']:34s} 房间 {item['room_id']}　{item['events']} 条　{started}")


# --------------------------------------------------------------------------- #


def _write_records(handle: Any, records: Any, fmt: str) -> int:
    """把事件序列写进文件句柄，返回写入条数。"""
    written = 0
    if fmt == "csv":
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CSV_COLUMNS)
        for event in records:
            writer.writerow(CsvSink._row(event))
            written += 1
    elif fmt == "txt":
        for event in records:
            if event.type != "danmaku" or not event.text:
                continue
            stamp = time.strftime("%H:%M:%S", time.localtime(event.recv_ts))
            handle.write(f"[{stamp}] {event.uname or '匿名'}: {event.text}\n")
            written += 1
    else:  # jsonl
        for event in records:
            handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
            written += 1
    return written


def _open_output(out_path: str | None):
    """输出到文件（CSV 用 utf-8-sig 方便 Excel）或 stdout。

    注意：`--out` 省略时必须**直接返回 `sys.stdout`**。
    这里曾经用 `contextlib.nullcontext(sys.stdout)` 包了一层，结果不写文件时
    拿到的是那个 nullcontext 而不是 stdout，`handle.write` 直接 AttributeError
    —— 也就是"导出到标准输出"这条最有用的路径整个坏掉（由 CLI 端到端测试发现）。
    """
    if out_path is None:
        return sys.stdout, False
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if path.suffix.lower() == ".csv" else "utf-8"
    return path.open("w", encoding=encoding, newline="\n"), True


@main.command()
@_scope_options
@click.option(
    "--format", "fmt", type=click.Choice(["csv", "jsonl", "txt"]), default="csv", show_default=True
)
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None, help="输出文件（默认打到 stdout）")
@click.option("--types", default=None, help="只要这些类型，逗号分隔")
@click.option("--keyword", default=None, help="关键词过滤（优先走 FTS5）")
@click.option("--since", default=None, help="起始时间：时间戳或 'YYYY-MM-DD HH:MM:SS'")
@click.option("--until", "until", default=None, help="结束时间")
@click.option("--limit", type=int, default=None, help="最多导出多少条")
@click.pass_context
def export(
    ctx: click.Context,
    db: str | None,
    session_id: str | None,
    room_id: int | None,
    fmt: str,
    out_path: str | None,
    types: str | None,
    keyword: str | None,
    since: str | None,
    until: str | None,
    limit: int | None,
) -> None:
    """从 SQLite 导出为 csv / jsonl / txt（流式，不占内存）。

    CSV 带 UTF-8 BOM（`utf-8-sig`），中文版 Excel 双击打开不会乱码；
    Web 面板的 `/api/export` 也做同样处理（见缺陷 D19）。
    """
    config = _build_config(ctx)
    type_list = [item.strip() for item in types.split(",") if item.strip()] if types else None

    store = _open_store(config, db)
    try:
        if store.count(session_id=session_id, room_id=room_id) == 0:
            click.secho("该范围内没有数据。", fg="yellow", err=True)
            raise SystemExit(EXIT_OK)

        handle, is_file = _open_output(out_path)
        try:
            written = _write_records(
                handle,
                store.iter_events(
                    session_id=session_id,
                    room_id=room_id,
                    types=type_list,
                    keyword=keyword,
                    since=_parse_time(since),
                    until=_parse_time(until),
                    limit=limit,
                ),
                fmt,
            )
        finally:
            if is_file:
                handle.close()
    finally:
        store.close()

    if out_path:
        click.secho(f"已导出 {written} 条到 {out_path}（{fmt}）", fg="green", err=True)
    else:
        click.echo(f"# 共 {written} 条", err=True)


# --------------------------------------------------------------------------- #


@main.command()
@click.argument("source", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--to", "fmt", type=click.Choice(["csv", "jsonl", "txt"]), default="csv", show_default=True
)
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None, help="输出文件")
@click.option("--types", default=None, help="只要这些类型，逗号分隔")
@click.option("--keyword", default=None, help="关键词过滤")
@click.option("--limit", type=int, default=None, help="最多转换多少条")
@click.pass_context
def replay(
    ctx: click.Context,
    source: str,
    fmt: str,
    out_path: str | None,
    types: str | None,
    keyword: str | None,
    limit: int | None,
) -> None:
    """把 JSONL 离线转换成 csv / jsonl / txt（**不联网**）。"""
    # 只要日志级别，不读数据库配置；配置非法不该阻断离线转换
    _build_config(ctx)

    type_list = [item.strip() for item in types.split(",") if item.strip()] if types else None
    filters = _make_filter(types=type_list, keywords=(keyword,) if keyword else ())

    def _records() -> Any:
        produced = 0
        with open(source, "r", encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    click.secho(f"  跳过第 {line_no} 行：不是合法 JSON", fg="yellow", err=True)
                    continue
                try:
                    event = Event.model_validate(payload)
                except Exception as exc:  # noqa: BLE001 - 单行坏了不该中断整体转换
                    click.secho(f"  跳过第 {line_no} 行：{exc}", fg="yellow", err=True)
                    continue
                if filters is not None and not filters.match(event):
                    continue
                yield event
                produced += 1
                if limit is not None and produced >= limit:
                    return

    handle, is_file = _open_output(out_path)
    try:
        written = _write_records(handle, _records(), fmt)
    finally:
        if is_file:
            handle.close()

    click.secho(f"已转换 {written} 条（{source} → {fmt}）", fg="green", err=True)


# --------------------------------------------------------------------------- #
# wordcloud（V2）
# --------------------------------------------------------------------------- #


@main.command()
@_scope_options
@click.option("--source", "source_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="改从 JSONL 读（不读数据库；离线可用）")
@click.option("--format", "fmt", type=click.Choice(["text", "json", "png", "html"]), default="text",
              show_default=True, help="输出格式；png 需要 wordcloud 依赖")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), default=None,
              help="输出文件（text 默认打印到终端）")
@click.option("--top", type=int, default=30, show_default=True, help="榜单/词云最多多少个词")
@click.option("--min-count", type=int, default=1, show_default=True, help="低于该次数不统计")
@click.option("--min-length", type=int, default=None, help="最短词长（默认 2；1 表示保留单字）")
@click.option("--max-length", type=int, default=None, help="最长词长（默认 12）")
@click.option("--stopwords-file", type=click.Path(dir_okay=False), default=None,
              help="自定义停用词文件（一行一个，覆盖内置表）")
@click.option("--dict", "user_dict", type=click.Path(dir_okay=False), default=None,
              help="jieba 自定义词典（补齐房间黑话/主播名）")
@click.option("--tokenizer", type=click.Choice(["auto", "jieba", "fallback"]), default="auto",
              show_default=True, help="分词器；auto = 有 jieba 就用")
@click.option("--types", default=None, help="只要这些类型，逗号分隔（默认 danmaku,super_chat）")
@click.option("--since", default=None, help="起始时间：时间戳或 'YYYY-MM-DD HH:MM:SS'")
@click.option("--until", "until", default=None, help="结束时间")
@click.option("--limit", type=int, default=None, help="最多分析多少条")
@click.option("--include-emotes", is_flag=True,
              help="把表情占位符（[dog]）也计入词语榜；默认单独列出")
@click.option("--font", "font_path", type=click.Path(dir_okay=False), default=None,
              help="PNG 用的中文字体（默认自动探测系统字体）")
@click.option("--width", type=int, default=1280, show_default=True, help="PNG 宽")
@click.option("--height", type=int, default=720, show_default=True, help="PNG 高")
@click.pass_context
def wordcloud(
    ctx: click.Context,
    db: str | None,
    session_id: str | None,
    room_id: int | None,
    source_path: str | None,
    fmt: str,
    out_path: str | None,
    top: int,
    min_count: int,
    min_length: int | None,
    max_length: int | None,
    stopwords_file: str | None,
    user_dict: str | None,
    tokenizer: str,
    types: str | None,
    since: str | None,
    until: str | None,
    limit: int | None,
    include_emotes: bool,
    font_path: str | None,
    width: int,
    height: int,
) -> None:
    """弹幕词云：统计词频并输出榜单 / PNG / HTML。

    表情占位符（`哈哈[dog]`）**默认不计入词语统计**，而是单独成榜 ——
    否则词频榜会被表情刷屏（见 `text.py` 的模块说明）。
    想一起统计就加 `--include-emotes`。

    示例：
      bili-danmaku wordcloud --session 20260927-232630-23438368 --top 30
      bili-danmaku wordcloud --format png --out cloud.png --top 120
      bili-danmaku wordcloud --source data/23438368_20260927-23.jsonl --format html --out cloud.html
    """
    from .text import (
        DEFAULT_MAX_LENGTH,
        DEFAULT_MIN_LENGTH,
        MissingDependency,
        count_words,
        load_stopwords,
        load_user_dict,
        render_html,
        render_png,
        render_text,
    )

    config = _build_config(ctx)
    window = {"since": _parse_time(since), "until": _parse_time(until)}
    types_list = [item.strip() for item in types.split(",") if item.strip()] if types else [
        "danmaku",
        "super_chat",
    ]
    stopwords = load_stopwords(stopwords_file) if stopwords_file else None
    if user_dict:
        added = load_user_dict(user_dict)
        click.secho(f"已加载自定义词典 {user_dict}（{added} 词）", fg="cyan", err=True)

    filters: dict[str, Any] = {
        "session": session_id,
        "room": room_id,
        "since": since,
        "until": until,
        "types": ",".join(types_list),
        "limit": limit,
        "source": source_path or str(db or ""),
    }

    if source_path:
        records = _iter_jsonl_texts(source_path, limit=limit)
    else:
        store = _open_store(config, db)
        total = store.count(
            session_id=session_id, room_id=room_id, types=types_list,
            since=window["since"], until=window["until"],
        )
        if total == 0:
            store.close()
            click.secho("该范围内没有数据。先执行 bili-danmaku grab（需 sqlite 落盘）。", fg="yellow")
            raise SystemExit(EXIT_OK)
        records = store.iter_texts(
            session_id=session_id, room_id=room_id, types=types_list,
            since=window["since"], until=window["until"], limit=limit,
        )

    try:
        report = count_words(
            records,
            top=top,
            min_count=min_count,
            min_length=min_length if min_length is not None else DEFAULT_MIN_LENGTH,
            max_length=max_length if max_length is not None else DEFAULT_MAX_LENGTH,
            stopwords=stopwords,
            tokenizer=tokenizer,
            include_emotes=include_emotes,
            filters=filters,
        )
    finally:
        if not source_path:
            store.close()

    if fmt == "text":
        click.echo(render_text(report, top=top))
        return

    if fmt == "json":
        import json as _json

        payload = _json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
        if out_path:
            Path(out_path).write_text(payload, encoding="utf-8")
            click.secho(f"已写入 {out_path}", fg="green", err=True)
        else:
            click.echo(payload)
        return

    if fmt == "html":
        target = Path(out_path or "wordcloud.html")
        target.write_text(render_html(report, top=max(top, 80)), encoding="utf-8")
        click.secho(f"已写入 {target}（用浏览器打开）", fg="green", err=True)
        return

    # png
    target = Path(out_path or "wordcloud.png")
    try:
        render_png(
            report, target, width=width, height=height, font_path=font_path, top=max(top, 200)
        )
    except MissingDependency as exc:
        click.secho(str(exc), fg="red", err=True)
        raise SystemExit(EXIT_CONFIG) from exc
    except ValueError as exc:
        click.secho(f"{exc}", fg="red", err=True)
        raise SystemExit(EXIT_ERROR) from exc
    click.secho(f"已写入 {target}（{report.distinct} 个词，前 {top} 个上色）", fg="green", err=True)


def _iter_jsonl_texts(path: str | Path, *, limit: int | None = None):
    """从 JSONL 流式读出 `(type, text, extra)`，供词云离线分析。"""
    produced = 0
    with open(path, "r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                click.secho(f"  跳过第 {line_no} 行：不是合法 JSON", fg="yellow", err=True)
                continue
            if not isinstance(payload, dict):
                click.secho(f"  跳过第 {line_no} 行：不是对象", fg="yellow", err=True)
                continue
            yield (
                str(payload.get("type") or "other"),
                str(payload.get("text") or ""),
                payload.get("extra") if isinstance(payload.get("extra"), dict) else {},
            )
            produced += 1
            if limit is not None and produced >= limit:
                return


# --------------------------------------------------------------------------- #
# sc（V2-b）：Super Chat 汇总 / 告警
# --------------------------------------------------------------------------- #

#: SC 金额单位换算：B 站 SC 的 `price` 字段单位是**元**。
#: 依据：协议里 SC 价格是 30/50/100/500/1000/2000 元这类整数档，
#: 与 `--min-price 30` 的语义一致；礼物/上舰才是金瓜子（1000 = 1 元）。
#:
#: ⚠️ **这一条尚未用真实 SC 样本验证**（实测蹲了 3 个高热度房间 4 分钟，一条 SC 都没出现）。
#: 如果拿到真实样本发现差了 1000 倍，用 `--price-unit milli` 覆盖即可 ——
#: 不必改代码（见 `_SC_UNIT_SCALE`）。
SC_PRICE_UNIT = "元"

#: `--price-unit` 的取值 → 存储值换算到「元」的除数。
#: 语义是"**存进库里的 price 是什么单位**"：
#: `yuan` 表示存的就是元（默认假设），`milli` 表示存的是毫元（需 ÷1000）。
_SC_UNIT_SCALE = {"yuan": 1, "milli": 1000}


def run_sc(
    *,
    config: Config,
    store: DanmakuStore,
    room_id: int | None,
    session_id: str | None,
    since: float | None,
    until: float | None,
    min_price: int | None,
    limit: int | None,
    top: int,
    as_json: bool,
    price_unit: str = "yuan",
) -> dict[str, Any]:
    """从 SQLite 汇总 SC，返回报告字典（便于单测直接调用）。

    `min_price` 的门槛单位是**元**；`price_unit` 说明库里 `price` 的单位
    （`yuan` 默认 / `milli` 毫元），换算后再比较。
    金额比较用 Python 做而不是下推到 SQL：SC 条数很少，
    而且 `DanmakuStore._build_where` 只支持等值与 LIKE。
    """
    scale = _SC_UNIT_SCALE.get(price_unit)
    if scale is None:
        raise ValueError(f"未知 --price-unit: {price_unit}（支持 yuan / milli）")

    scope: dict[str, Any] = {
        "session_id": session_id,
        "room_id": room_id,
        "types": ["super_chat"],
        "since": since,
        "until": until,
    }
    events = list(store.iter_events(**scope))

    priced: list[tuple[Event, float]] = [
        (event, (event.price or 0) / scale) for event in events
    ]
    if min_price is not None:
        priced = [(event, yuan) for event, yuan in priced if yuan >= min_price]

    priced.sort(key=lambda item: item[0].recv_ts)
    if limit is not None and limit > 0:
        priced = priced[-limit:]  # 保留**最新**的 N 条

    amounts = [yuan for _event, yuan in priced]
    total = round(sum(amounts), 2)
    by_sender: dict[str, dict[str, Any]] = {}
    for event, yuan in priced:
        key = str(event.uid or 0)
        bucket = by_sender.setdefault(
            key, {"uid": event.uid, "uname": event.uname, "total_price": 0.0, "count": 0}
        )
        bucket["total_price"] = round(bucket["total_price"] + yuan, 2)
        bucket["count"] += 1
        if event.uname:
            bucket["uname"] = event.uname

    senders = sorted(by_sender.values(), key=lambda item: (-item["total_price"], item["uid"] or 0))
    if top > 0:
        senders = senders[:top]

    return {
        "db": str(store.path),
        "unit": SC_PRICE_UNIT,
        "price_unit": price_unit,
        "count": len(priced),
        "total_price": total,
        "max_price": max(amounts) if amounts else 0,
        "avg_price": round(total / len(amounts), 1) if amounts else 0.0,
        "first_ts": priced[0][0].recv_ts if priced else None,
        "last_ts": priced[-1][0].recv_ts if priced else None,
        "min_price": min_price,
        "filters": {
            "session": session_id,
            "room": room_id,
            "since": since,
            "until": until,
        },
        "top_senders": senders,
        "items": [
            {
                "recv_ts": event.recv_ts,
                "room_id": event.room_id,
                "uid": event.uid,
                "uname": event.uname,
                "price": yuan,
                "raw_price": event.price,
                "text": event.text,
                "msg_id": event.msg_id,
            }
            for event, yuan in priced
        ],
    }


def format_amount(value: float | int | None) -> str:
    """把金额格式化成人类可读：整数不留 `.0`，小数最多两位。

    SC 的 price 常见是整数元（30/50/100），毫元模式下会变成 0.03 这类小数，
    两种都要好看。
    """
    amount = float(value or 0)
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def print_sc_report(report: dict[str, Any], *, top: int = 10, show_items: int = 20) -> None:
    """终端输出：先汇总，再明细（明细最多 `show_items` 条，避免刷屏）。"""
    unit = report["unit"]
    click.echo("")
    click.secho("=== Super Chat 汇总 ===", bold=True)
    click.echo(f"数据库      {report['db']}")
    click.echo(f"条数        {report['count']}")
    click.echo(f"合计        {format_amount(report['total_price'])} {unit}")
    click.echo(
        f"单条最高    {format_amount(report['max_price'])} {unit}　"
        f"平均 {format_amount(report['avg_price'])} {unit}"
    )
    if report.get("price_unit") and report["price_unit"] != "yuan":
        click.echo(f"金额单位    price 按 {report['price_unit']} 解读（已换算成{unit}）")
    if report.get("min_price") is not None:
        click.echo(f"金额门槛    >= {format_amount(report['min_price'])} {unit}")
    if report["count"]:
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report["first_ts"]))
        end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report["last_ts"]))
        click.echo(f"时间范围    {start} → {end}")

    senders = report.get("top_senders") or []
    if senders:
        click.secho("\n贡献 TOP", bold=True)
        for index, item in enumerate(senders[:top], 1):
            click.echo(
                f"  {index:2d}. {(item['uname'] or '匿名'):<20} "
                f"{format_amount(item['total_price']):>8} {unit}　{item['count']} 笔"
            )

    items = report.get("items") or []
    if items:
        click.secho(f"\n最近 {min(show_items, len(items))} 条", bold=True)
        for event in items[-show_items:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(event["recv_ts"]))
            text = (event["text"] or "").replace("\n", " ")
            if len(text) > 40:
                text = text[:40] + "…"
            click.echo(
                f"  [{stamp}] {(event['uname'] or '匿名'):<16} "
                f"{format_amount(event['price']):>6} {unit}  {text}"
            )
    else:
        click.secho("\n该范围内没有 SC。", fg="yellow")


def sc_alert_line(event: Event) -> str:
    """把一条 SC 变成告警行（终端 + 面板都复用同一份措辞）。"""
    stamp = time.strftime("%H:%M:%S", time.localtime(event.recv_ts))
    text = (event.text or "").replace("\n", " ")
    if len(text) > 60:
        text = text[:60] + "…"
    return (
        f"{stamp}  SC {format_amount(event.price)} {SC_PRICE_UNIT}  "
        f"{event.uname or '匿名'}: {text}"
    )


async def _run_sc_follow(
    config: Config,
    room: str,
    *,
    min_price: int,
    duration: float | None,
    until: str | None,
    bell: bool,
    cookie_file: str | None,
) -> int:
    """实时跟随一个房间，把 >= min_price 的 SC 打成告警行。"""
    from .collector import RoomCollector
    from .filters import build_filter
    from .sinks import Bus, Sink

    deadline = _resolve_deadline(f"{duration}s" if duration is not None else "0", until)
    creds = _load_creds(config, cookie_file)
    creds = await _ensure_buvid(config, creds)

    class AlertSink(Sink):
        """只接收并打印告警；**不落盘** —— 用户要的是告警，不是再存一份。

        类型白名单已经在 collector 的过滤层做了（只放 `super_chat` 进来），
        这里再按金额门槛筛一遍：金额比较不是过滤层的能力（见 `filters.py`）。
        """

        name = "sc-alert"

        def __init__(self, cfg: Config, threshold: int, ring: bool) -> None:
            super().__init__(cfg)
            self.threshold = threshold
            self.ring = ring
            self.hits = 0

        def write(self, events: list[Event]) -> None:
            for event in events:
                if event.type != "super_chat":
                    continue
                if (event.price or 0) < self.threshold:
                    continue
                self.hits += 1
                click.secho(sc_alert_line(event), fg="magenta", bold=True)
                if self.ring:
                    click.echo("\a", nl=False)
            self.record(len(events))

    sink = AlertSink(config, min_price, bell)
    bus = Bus([sink], config)

    async with BiliApi(config, creds) as api:
        resolved = await api.room_init(parse_room_input(room))
        collector = RoomCollector(
            room,
            config,
            creds,
            api,
            bus,
            # 只要 SC：类型白名单（content_only 保留：SC 正文就是内容）
            filters=build_filter(types=["super_chat"], content_only=True),
            seed_history=False,  # 跟踪只看新消息，不复述历史
        )
        click.secho(
            f"开始跟踪房间 {resolved.room_id} 的 SC："
            f"阈值 >= {min_price} {SC_PRICE_UNIT}，Ctrl+C 停止",
            fg="cyan",
        )
        try:
            await collector.run(deadline_monotonic=deadline)
        except KeyboardInterrupt:
            click.echo("")
    click.secho(f"共命中 {sink.hits} 条 SC（收包 {sink.stats.written} 条）。", fg="green")
    return EXIT_OK


@main.command("sc")
@_scope_options
@click.argument("room", required=False)
@click.option("--follow", is_flag=True, help="实时跟踪（配合 ROOM 参数）：只报新的 SC")
@click.option("--duration", type=float, default=None, help="跟踪时长（秒）；不填表示不限期")
@click.option("--until", default=None, help="跟踪到今天指定时刻，如 23:00")
@click.option("--since", default=None, help="汇总起始时间：时间戳或 'YYYY-MM-DD HH:MM:SS'")
@click.option("--until-time", "until_time", default=None, help="汇总结束时间（避免与跟踪的 --until 混淆）")
@click.option(
    "--min-price",
    type=int,
    default=None,
    help=f"金额门槛（单位：{SC_PRICE_UNIT}；汇总时过滤，跟踪时只报不低于它的）",
)
@click.option("--top", type=int, default=10, show_default=True, help="贡献榜条数")
@click.option("--limit", type=int, default=None, help="只看最近 N 条")
@click.option(
    "--price-unit",
    type=click.Choice(["yuan", "milli"]),
    default="yuan",
    show_default=True,
    help="库里 price 字段的单位（yuan=元；milli=毫元，遇到 1000 倍偏差时用）",
)
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出")
@click.option("--bell", is_flag=True, help="告警时响铃（仅 --follow）")
@click.option("--cookie-file", type=click.Path(dir_okay=False), default=None, help="凭据文件路径")
@click.pass_context
def sc_cmd(
    ctx: click.Context,
    db: str | None,
    session_id: str | None,
    room_id: int | None,
    room: str | None,
    follow: bool,
    duration: float | None,
    until: str | None,
    since: str | None,
    until_time: str | None,
    min_price: int | None,
    top: int,
    limit: int | None,
    price_unit: str,
    as_json: bool,
    bell: bool,
    cookie_file: str | None,
) -> None:
    """Super Chat 汇总与告警。

    汇总（默认）：从 SQLite 统计 `super_chat` 的条数、金额合计、贡献榜与最近明细。

        bili-danmaku sc --room 23438368 --min-price 30
        bili-danmaku sc --session 20260928-120000-23438368 --json

    实时告警：跟一个房间，把达到金额门槛的 SC 打成醒目告警行（不落盘）。

        bili-danmaku sc 23438368 --follow --min-price 30 --duration 3600 --bell

    **金额单位是元**（SC 的 `price` 本身就是元；礼物/上舰才是金瓜子，1000 = 1 元）。
    这一条还没用真实 SC 样本验证过；若实测发现差 1000 倍，加 `--price-unit milli`。
    """
    config = _build_config(ctx)

    if follow:
        target = room or (str(room_id) if room_id is not None else None)
        if not target:
            click.secho("实时跟踪需要指定房间号：bili-danmaku sc <房间号> --follow", fg="red", err=True)
            raise SystemExit(EXIT_CONFIG)
        exit_code = asyncio.run(
            _run_sc_follow(
                config,
                target,
                min_price=min_price or 0,
                duration=duration,
                until=until,
                bell=bell,
                cookie_file=cookie_file,
            )
        )
        raise SystemExit(exit_code)

    store = _open_store(config, db)
    try:
        report = run_sc(
            config=config,
            store=store,
            room_id=room_id or (int(room) if room and str(room).isdigit() else None),
            session_id=session_id,
            since=_parse_time(since),
            until=_parse_time(until_time),
            min_price=min_price,
            limit=limit,
            top=top,
            as_json=as_json,
            price_unit=price_unit,
        )
    finally:
        store.close()

    if as_json:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(EXIT_OK)

    print_sc_report(report, top=top)
    raise SystemExit(EXIT_OK)


# --------------------------------------------------------------------------- #
# web：面板（前台运行 / 托管后台运行）
# --------------------------------------------------------------------------- #


def _resolve_web_port(port: int | str | None, config_path: str | None) -> int:
    """从"父组 `web` 的 `--port`"与"配置文件"里解析出端口；解析不出来返回 0。

    拆成纯函数是为了可测：`_effective_web_port()` 只负责从 click 上下文取值。
    返回 0 表示"不确定" —— 调用方据此**不做提示**，
    绝不因为一个提示逻辑把启动流程搞崩。
    """
    from .config import ConfigError

    if port not in (None, ""):
        try:
            value = int(port)
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0
    try:
        config = Config.load(config_path)
    except ConfigError:
        return 0
    return int(config.web.port or 0)


def _effective_web_port(ctx: click.Context) -> int:
    """解析"这次 `web start` 会用哪个端口"（不启动任何东西）。

    只看**父组 `web` 的选项**与配置文件；`web start` 自己给的 `--port` 由调用方优先处理。
    """
    parent = dict((ctx.obj or {}).get("web_parent") or {})
    return _resolve_web_port(parent.get("port"), (ctx.obj or {}).get("config_path"))


def _port_is_free(port: int) -> bool:
    """端口当前是否空闲（不设 SO_REUSEADDR，理由同 `server.bind_port`）。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _web_config(ctx: click.Context, *, host: str | None, port: int | None,
                out_dir: str | None, formats: str | None, keep_raw: bool | None,
                content_only: bool | None) -> Config:
    overrides: dict[str, Any] = {}
    if out_dir:
        overrides.setdefault("sinks", {})["out_dir"] = out_dir
    if formats:
        overrides.setdefault("sinks", {})["types"] = [
            item.strip() for item in formats.split(",") if item.strip()
        ]
    if keep_raw is not None:
        overrides.setdefault("sinks", {})["keep_raw"] = keep_raw
    if content_only is not None:
        overrides.setdefault("general", {})["content_only"] = content_only
    if host:
        overrides.setdefault("web", {})["host"] = host
    if port is not None:
        overrides.setdefault("web", {})["port"] = port
    return _build_config(ctx, overrides or None)


def web_options(func):
    """给 `web` 组与 `web start` 共用的一套选项。

    之所以两处都要挂：click 里父组选项必须写在子命令**之前**
    （`web --port 9000 start`），而用户的直觉是 `web start --port 9000`。
    两处都声明后两种写法都能用，不需要额外解释。
    """
    func = click.option("--rooms", default=None, help="启动时预置监控的房间，逗号分隔")(func)
    func = click.option("--host", default=None, help="监听地址（默认取配置，通常 127.0.0.1）")(func)
    func = click.option("--port", type=int, default=None, help="监听端口（默认 8787；被占用会自动向后尝试）")(func)
    func = click.option("--open", "open_browser", is_flag=True, help="启动后自动打开浏览器")(func)
    func = click.option("--out", "out_dir", default=None, help="输出目录（覆盖配置）")(func)
    func = click.option("--format", "formats", default=None, help="落盘格式，逗号分隔（默认取配置）")(func)
    func = click.option("--raw/--no-raw", "keep_raw", default=None, help="是否保留原始消息")(func)
    func = click.option(
        "--content-only/--all-events",
        "content_only",
        default=None,
        help="只采集有文字的弹幕与 SC（默认）；--all-events 会连同进房/上舰/点赞等一并落盘",
    )(func)
    func = click.option("--cookie-file", type=click.Path(dir_okay=False), default=None, help="凭据文件路径")(func)
    return func


@main.group(invoke_without_command=True)
@web_options
@click.pass_context
def web(
    ctx: click.Context,
    rooms: str | None,
    host: str | None,
    port: int | None,
    open_browser: bool,
    out_dir: str | None,
    formats: str | None,
    keep_raw: bool | None,
    content_only: bool | None,
    cookie_file: str | None,
) -> None:
    """Web 面板：前台运行，或用 start/stop/status/logs 托管后台常驻。

    不带子命令时**前台运行**（Ctrl+C 停止，适合调试）：

        bili-danmaku web --rooms 7878 --open

    常驻（关掉终端也继续跑，用 web stop 优雅停止）：

        bili-danmaku web start --rooms 7878 --open
        bili-danmaku web status
        bili-danmaku web logs --tail 30
        bili-danmaku web stop
    """
    if ctx.invoked_subcommand is not None:
        # 交给子命令；把父级参数挂到 ctx.obj 供子命令兜底复用
        # （注意是**合并**而不是替换：ctx.obj 里还有 --config / -v 留下的小字典）
        ctx.ensure_object(dict)
        ctx.obj["web_parent"] = {
            "rooms": rooms,
            "host": host,
            "port": port,
            "open_browser": open_browser,
            "out_dir": out_dir,
            "formats": formats,
            "keep_raw": keep_raw,
            "content_only": content_only,
            "cookie_file": cookie_file,
            "config_path": (ctx.obj or {}).get("config_path"),
            "level": (ctx.obj or {}).get("level"),
        }
        return

    _run_web_foreground(
        ctx,
        rooms=rooms,
        host=host,
        port=port,
        open_browser=open_browser,
        out_dir=out_dir,
        formats=formats,
        keep_raw=keep_raw,
        content_only=content_only,
        cookie_file=cookie_file,
    )


class _StopHandle:
    """把「停止面板」的回调与 uvicorn 的 `Server` 绑定起来。

    控制端点只依赖这个可调用对象，不依赖 uvicorn 的内部结构；
    `server` 在 `uvicorn.Server(...)` 构造完成后回填。
    """

    def __init__(self) -> None:
        self.server: Any = None

    def request(self) -> None:
        if self.server is None:  # pragma: no cover - 理论上不会发生
            log.warning("停止请求已收到，但 uvicorn Server 还没就绪")
            return
        self.server.should_exit = True


def _run_web_foreground(
    ctx: click.Context,
    *,
    rooms: str | None,
    host: str | None,
    port: int | None,
    open_browser: bool,
    out_dir: str | None,
    formats: str | None,
    keep_raw: bool | None,
    content_only: bool | None,
    cookie_file: str | None,
    managed: bool = False,
) -> None:
    """真正把面板跑起来（前台）或被 `start` 用作子进程入口。

    `managed=True` 时会把运行信息写进状态文件，让 `web status/stop` 能找到它。
    """
    config = _web_config(
        ctx, host=host, port=port, out_dir=out_dir, formats=formats,
        keep_raw=keep_raw, content_only=content_only,
    )

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        click.secho("缺少 uvicorn，无法启动面板：pip install 'uvicorn>=0.29'", fg="red", err=True)
        raise SystemExit(EXIT_CONFIG) from exc

    from .runtime import LOG_FILE, WebRuntime, clear_state, save_state
    from .server import PanelService, WebHub, bind_port, create_app, find_dist

    requested = config.web.port
    actual = bind_port(config.web.host, requested)
    if actual != requested:
        click.secho(f"端口 {requested} 被占用，改用 {actual}", fg="yellow")

    url = f"http://{config.web.host}:{actual}/"
    initial_rooms = tuple(item.strip() for item in (rooms or "").split(",") if item.strip())
    dist = find_dist()

    click.secho("")
    click.secho("=== bili-danmaku 面板 ===", bold=True)
    click.echo(f"地址        {url}")
    click.echo(f"监听        {config.web.host}:{actual}")
    click.echo(f"落盘格式    {', '.join(config.sinks.types)}  →  {config.sinks.out_dir}")
    click.echo(
        "采集范围    "
        + ("仅文字弹幕 / SC（含表情占位符）" if config.general.content_only
           else "全部事件（--all-events）")
    )
    click.echo(f"预置房间    {', '.join(initial_rooms) if initial_rooms else '（无，可在页面上添加）'}")
    if dist is None:
        click.secho(
            "前端未构建：页面会显示提示。执行 cd web && npm install && npm run build 后刷新。",
            fg="yellow",
        )
    else:
        click.echo(f"前端产物    {dist}")
    if config.web.host not in ("127.0.0.1", "localhost", "::1") and not config.web.token:
        click.secho("警告：监听非回环地址但未设置 web.token", fg="red")
    click.echo("停止方式    面板右上角「停止面板」按钮，或 bili-danmaku web stop")
    if not managed:
        click.echo("            前台模式也可以直接 Ctrl+C")
    click.echo("")

    credentials = _load_creds(config, cookie_file)
    hub = WebHub()
    panel = PanelService(config, credentials, hub)

    # 优雅停止：控制端点 → StopHandle 置位 uvicorn 的 should_exit →
    # lifespan 的 finally 调 panel.stop() → 每个房间 bus.stop() flush 落盘。
    #
    # 用「回调容器」而不是把 app 塞进已构建的 Config：`uvicorn.Config` 在构造时
    # 就会处理 app（包中间件），构造后再赋值不可靠。这里让 app 先拿到一个
    # 稳定的可调用对象，稍后把 server 绑进去。
    handle = _StopHandle()
    app = create_app(
        config, credentials, hub, panel,
        initial_rooms=initial_rooms, on_shutdown=handle.request,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.web.host,
            port=actual,
            log_level=config.general.log_level.lower(),
            access_log=False,
        )
    )
    handle.server = server

    runtime_record = WebRuntime(
        pid=os.getpid(),
        host=config.web.host,
        port=actual,
        url=url,
        rooms=list(initial_rooms),
        out_dir=str(config.sinks.out_dir),
        formats=list(config.sinks.types),
        content_only=bool(config.general.content_only),
        started_at=time.time(),
        log_path=str(LOG_FILE if managed else ""),
        frontend_built=dist is not None,
        version=__version__,
    )
    if managed:
        save_state(runtime_record)
        click.secho(f"已后台启动（pid {os.getpid()}），状态文件 .runtime/web.json", fg="green")

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass
    finally:
        if managed:
            clear_state()
    click.echo("")
    click.secho("面板已停止（数据已 flush）。", fg="yellow")


# --------------------------------------------------------------------------- #
# web start / stop / status / logs / restart
# --------------------------------------------------------------------------- #


@web.command("start")
@web_options
@click.option("--force", is_flag=True, help="已有实例在跑时先停掉再启动")
@click.pass_context
def web_start(
    ctx: click.Context,
    rooms: str | None,
    host: str | None,
    port: int | None,
    open_browser: bool,
    out_dir: str | None,
    formats: str | None,
    keep_raw: bool | None,
    content_only: bool | None,
    cookie_file: str | None,
    force: bool,
) -> None:
    """后台启动面板（关掉终端也继续跑）。"""
    from .runtime import LOG_FILE, live_runtime, server_argv, spawn_detached, stop_runtime

    parent = dict((ctx.obj or {}).get("web_parent") or {})
    # 子命令自己给的参数优先，其次才是写在 `web` 前面的那套
    own = {
        "rooms": rooms,
        "host": host,
        "port": port,
        "open_browser": open_browser,
        "out_dir": out_dir,
        "formats": formats,
        "keep_raw": keep_raw,
        "content_only": content_only,
        "cookie_file": cookie_file,
    }
    for key, value in own.items():
        if value not in (None, False):
            parent[key] = value

    existing = live_runtime()
    if existing is not None:
        if not force:
            click.secho(
                f"已有面板在运行（pid {existing.pid}，{existing.url}）。"
                "用 `web stop` 停止，或加 --force 重起。",
                fg="red",
                err=True,
            )
            raise SystemExit(EXIT_CONFIG)
        click.secho(f"先停止已有实例 pid {existing.pid}…", fg="yellow")
        stopped, detail = stop_runtime(existing)
        click.secho(f"  {detail}", fg="yellow" if stopped else "red")
        if not stopped:
            raise SystemExit(EXIT_ERROR)

    argv = server_argv(
        *_web_child_args(
            parent, rooms=parent.get("rooms"), open_browser=bool(parent.get("open_browser"))
        )
    )
    # 解释器与环境：绕开 venv 的转发器 stub（否则会多一个 console 窗口，
    # 关掉它面板就停 —— 见 runtime.child_python 的说明）。
    from .runtime import child_python

    _executable, child_env = child_python()

    # 并存语义：请求的端口被**别人**占着时，先告诉用户，再顺延。
    #
    # 最常见的场景是**托盘应用已经在跑**（它用默认端口 8787 起的进程内面板）。
    # 托盘刻意**不写** `.runtime/web.json`（那张状态文件归 CLI 的
    # `web start/stop/status` 管），所以 `live_runtime()` 看不到它 ——
    # 不提示的话就会出现"两个面板同时开着、而用户以为只有一个"。
    # 这里只**提示 + 顺延**，绝不自动去停止别人的进程（那可能是托盘）。
    requested = int((parent or {}).get("port") or _effective_web_port(ctx))
    if requested and not _port_is_free(requested):
        click.secho(
            f"端口 {requested} 已被占用（可能是托盘应用或其他面板），将顺延到下一个可用端口。",
            fg="yellow",
        )
        click.echo(
            "  提示：托盘应用自己带一个面板，可用托盘菜单打开；"
            "要替代它请先从托盘退出。"
        )

    process = spawn_detached(argv, log_path=LOG_FILE, env_overrides=child_env)

    # 等状态文件出现（子进程写）或进程退出（启动失败）
    deadline = time.monotonic() + 30.0
    runtime = None
    while time.monotonic() < deadline:
        runtime = live_runtime()
        if runtime is not None:
            break
        if process.poll() is not None:
            break
        time.sleep(0.2)

    if runtime is None:
        click.secho("启动失败，最近日志：", fg="red", err=True)
        click.echo(_tail(LOG_FILE, 20), err=True)
        raise SystemExit(EXIT_ERROR)

    click.secho(f"面板已在后台运行：{runtime.url}（pid {runtime.pid}）", fg="green", bold=True)
    click.echo(f"停止：bili-danmaku web stop　日志：bili-danmaku web logs")


def _web_child_args(parent: dict[str, Any], *, rooms: str | None, open_browser: bool) -> list[str]:
    """把父级 `web` 的选项翻译成子进程命令行参数（子进程入口是隐藏命令 `_serve`）。

    只输出**有值**的参数：`None` 会被 `subprocess.Popen` 转成字面量 "None"
    （Windows 上 `list2cmdline` 会把非字符串参数 `str()` 掉），
    子进程就会收到 `--config None` 这种非法配置并启动失败。
    """
    args = ["web", "_serve", "--managed"]
    if rooms:
        args += ["--rooms", str(rooms)]

    config_path = parent.get("config_path")
    if config_path:
        args = ["--config", str(config_path), *args]

    for key, flag in (
        ("host", "--host"),
        ("port", "--port"),
        ("out_dir", "--out"),
        ("formats", "--format"),
        ("cookie_file", "--cookie-file"),
    ):
        value = parent.get(key)
        if value not in (None, ""):
            args += [flag, str(value)]

    if parent.get("keep_raw") is not None:
        args.append("--raw" if parent["keep_raw"] else "--no-raw")
    if parent.get("content_only") is not None:
        args.append("--content-only" if parent["content_only"] else "--all-events")
    if open_browser:
        args.append("--open")
    return args


@web.command("stop")
@click.option("--force", is_flag=True, help="跳过优雅停止，直接强杀")
@click.option("--timeout", type=float, default=None, help="等待优雅退出的秒数（默认 30）")
def web_stop(force: bool, timeout: float | None) -> None:
    """优雅停止后台面板：先 flush 落盘再退出。"""
    from .runtime import STOP_WAIT_SECONDS, live_runtime, stop_runtime

    runtime = live_runtime()
    if runtime is None:
        click.secho("没有正在运行的面板实例（无需停止）。", fg="yellow")
        raise SystemExit(EXIT_OK)

    token = ""
    try:  # 配置里有 token 时要带上，否则控制端点会 403
        token = Config.load(DEFAULT_CONFIG_PATH).web.token
    except Exception:  # noqa: BLE001 - 配置坏了不该妨碍停止
        token = ""

    if force:
        from .runtime import clear_state, force_kill, wait_pid_exit

        killed, detail = force_kill(runtime.pid)
        clear_state()
        ok = killed and wait_pid_exit(runtime.pid, 5.0)
        click.secho(
            f"{'已强杀' if ok else '强杀失败'}（可能丢失少量未落盘数据）：{detail}",
            fg="yellow" if ok else "red",
        )
        raise SystemExit(EXIT_OK if ok else EXIT_ERROR)

    stopped, detail = stop_runtime(runtime, token=token, wait=timeout or STOP_WAIT_SECONDS)
    click.secho(detail, fg="green" if stopped else "red")
    raise SystemExit(EXIT_OK if stopped else EXIT_ERROR)


@web.command("status")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出")
def web_status(as_json: bool) -> None:
    """查看后台面板状态（含地址与已运行时间）。"""
    from .runtime import live_runtime

    runtime = live_runtime()
    if runtime is None:
        if as_json:
            click.echo(json.dumps({"running": False}, ensure_ascii=False))
        else:
            click.secho("面板未在运行。", fg="yellow")
            click.echo("启动：bili-danmaku web start --rooms <房间号>")
        raise SystemExit(EXIT_OK)

    if as_json:
        click.echo(json.dumps({"running": True, **runtime.as_dict()}, ensure_ascii=False, indent=2))
    else:
        click.secho("=== 面板状态 ===", bold=True)
        click.echo(runtime.describe())
    raise SystemExit(EXIT_OK)


@web.command("logs")
@click.option("--tail", type=int, default=40, show_default=True, help="显示最后 N 行")
@click.option("-f", "--follow", "follow", is_flag=True, help="持续输出（Ctrl+C 退出）")
def web_logs(tail: int, follow: bool) -> None:
    """查看后台面板日志（stdout/stderr 都写在这里）。"""
    from .runtime import LOG_FILE

    if not LOG_FILE.is_file():
        click.secho(f"还没有日志文件（{LOG_FILE}）。面板从未后台启动过？", fg="yellow")
        raise SystemExit(EXIT_OK)

    click.echo(_tail(LOG_FILE, tail))
    if not follow:
        raise SystemExit(EXIT_OK)

    click.secho(f"--- 持续输出 {LOG_FILE}（Ctrl+C 退出）---", fg="cyan", err=True)
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, 2)
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                click.echo(line.rstrip("\n"))
    except KeyboardInterrupt:
        pass
    raise SystemExit(EXIT_OK)


@web.command("restart")
@click.pass_context
def web_restart(ctx: click.Context) -> None:
    """重启后台面板（等价于 stop + start）。"""
    from .runtime import live_runtime, stop_runtime

    runtime = live_runtime()
    if runtime is not None:
        stopped, detail = stop_runtime(runtime)
        click.secho(detail, fg="green" if stopped else "red")
        if not stopped:
            raise SystemExit(EXIT_ERROR)
    ctx.invoke(web_start, force=True)


def _tail(path: Path, lines: int) -> str:
    """读文件最后 N 行（文件可能很大，不要整个读进来）。"""
    if not path.is_file():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        from collections import deque

        return "".join(deque(handle, maxlen=max(lines, 1)))


# `web` 组内部用的隐藏入口：子进程实际执行的就是它（等价于前台运行 + 写状态文件）
@web.command("_serve", hidden=True)
@click.option("--rooms", default=None)
@click.option("--managed", is_flag=True)
@click.option("--open", "open_browser", is_flag=True)
@click.option("--host", default=None)
@click.option("--port", type=int, default=None)
@click.option("--out", "out_dir", default=None)
@click.option("--format", "formats", default=None)
@click.option("--raw/--no-raw", "keep_raw", default=None)
@click.option("--content-only/--all-events", "content_only", default=None)
@click.option("--cookie-file", default=None)
@click.pass_context
def web_serve(
    ctx: click.Context,
    rooms: str | None,
    managed: bool,
    open_browser: bool,
    host: str | None,
    port: int | None,
    out_dir: str | None,
    formats: str | None,
    keep_raw: bool | None,
    content_only: bool | None,
    cookie_file: str | None,
) -> None:
    """内部命令：真正运行面板（`web start` 会以子进程方式调用它）。"""
    _run_web_foreground(
        ctx,
        rooms=rooms,
        host=host,
        port=port,
        open_browser=open_browser,
        out_dir=out_dir,
        formats=formats,
        keep_raw=keep_raw,
        content_only=content_only,
        cookie_file=cookie_file,
        managed=managed,
    )


# --------------------------------------------------------------------------- #
# 冻结/打包自检（`bili-danmaku verify`）
#
# 为什么需要它：打包成 exe 后**没有工作区兜底**，一旦前端没被打进去、
# 或 `find_dist()` 的候选路径算错，面板会静默退化成降级页 —— 那是"看起来能跑、
# 其实不能用"。这个命令把"前端能不能定位 + 面板能不能在本进程起来 +
# 能不能真的提供 index.html"这三件事端到端验一遍，供打包脚本与人工排查使用。
# --------------------------------------------------------------------------- #


@main.command("verify")
@click.option("--port", type=int, default=0, help="自检用端口（0 = 让系统分配）")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出（供脚本消费）")
@click.option(
    "--json-out",
    "json_out",
    default=None,
    help="把结果写入文件（`--windowed` 打包后 stdout 不可见，靠它取结果）",
)
@click.pass_context
def verify(ctx: click.Context, port: int, as_json: bool, json_out: str | None) -> None:
    """自检：前端定位 + 面板本进程启动 + 首页可访问（打包后务必跑）。"""
    import json as json_module

    from .launcher import LauncherOptions, PanelController
    from .server import find_dist

    dist = find_dist()
    result: dict[str, Any] = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": getattr(sys, "_MEIPASS", None),
        "find_dist": str(dist) if dist else None,
        "frontend_built": dist is not None,
        "index_exists": bool(dist and (dist / "index.html").is_file()),
        "started": False,
        "http_ok": False,
        "url": "",
        "port": None,
        "detail": "",
    }

    exit_code = EXIT_ERROR
    if dist is None:
        result["detail"] = (
            "找不到前端产物（find_dist() 返回 None）—— 页面会是降级页。"
            "打包时请确认带了 --add-data \"<web/dist>;web_dist\"。"
        )
    else:
        # `--port 0`（默认）交给启动器自己挑空闲端口；不要用 `bind_port` 预先挑，
        # 那个函数对 0 会返回错误的端口号（实测返回 1）。
        controller = PanelController(
            LauncherOptions(host="127.0.0.1", port=port, rooms=(), open_browser=False)
        )
        try:
            status = controller.start(wait=True, timeout=20.0)
            result["started"] = status.running
            result["url"] = status.url
            result["port"] = status.port
            result["detail"] = status.error
            if status.running:
                import urllib.error
                import urllib.request

                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{status.port}/", timeout=5
                    ) as response:
                        body = response.read().decode("utf-8", "replace")
                        result["http_ok"] = response.status == 200
                        # 降级页不含 assets 引用；真前端会引用构建产物
                        result["served_real_frontend"] = "assets/index-" in body
                    exit_code = EXIT_OK
                except (urllib.error.URLError, OSError) as exc:
                    result["detail"] = f"首页请求失败：{type(exc).__name__}: {exc}"
        finally:
            controller.stop()

    if json_out:
        # `--windowed` 打包后没有控制台，stdout 写不出去；先落文件再输出。
        from pathlib import Path as _Path

        target = _Path(json_out)
        if target.parent and str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json_module.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if as_json:
        safe_echo(json_module.dumps(result, ensure_ascii=False, indent=2))
    else:
        mark = "✅" if exit_code == EXIT_OK else "❌"
        safe_echo(f"{mark} 自检结果")
        safe_echo(f"  冻结运行      {'是' if result['frozen'] else '否'}")
        safe_echo(f"  _MEIPASS      {result['meipass']}")
        safe_echo(f"  前端目录      {result['find_dist']}")
        safe_echo(f"  index.html    {'存在' if result['index_exists'] else '缺失'}")
        safe_echo(f"  面板已启动    {'是' if result['started'] else '否'}")
        safe_echo(f"  首页可访问    {'是' if result['http_ok'] else '否'}")
        if result.get("served_real_frontend") is not None:
            safe_echo(
                f"  真前端(非降级页) {'是' if result['served_real_frontend'] else '否'}"
            )
        if result["detail"]:
            safe_echo(f"  说明          {result['detail']}")
        if result["url"]:
            safe_echo(f"  地址          {result['url']}")
        if json_out:
            safe_echo(f"  已写入        {json_out}")

    raise SystemExit(exit_code)


# `tray` 命令定义在 `main` 之前（因为 `main` 的"无子命令即启动托盘"分支要引用它），
# 所以在这里注册到组上。
main.add_command(tray_command)


if __name__ == "__main__":
    main()
