"""配置加载与校验。

优先级：CLI 参数 > 环境变量(BILI_*) > TOML 文件 > 内置默认值。
配置非法一律「启动即失败」，不留到运行期。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 回退
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_CONFIG_PATH = "config.toml"
#: 只有真正回环的地址才允许无 token 访问；0.0.0.0 会监听所有网卡，必须带 token
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ConfigError(Exception):
    """配置非法。"""


class GeneralConfig(BaseModel):
    log_level: str = "INFO"
    max_rooms: int = 3
    anonymize: bool = False
    #: 只采集「有用户文字内容」的弹幕与 SC。
    #: 置 False 会连同进房 / 上舰 / 点赞 / 礼物 / 房间数据等一并落盘，
    #: 仅建议在协议调试（probe --dump-raw 之类）时临时打开。
    content_only: bool = True


class ApiConfig(BaseModel):
    cookie: str = ""
    cookie_file: str = ".secrets/cookies.json"
    request_timeout: float = 10.0
    #: 「房间号 → 房间信息（含主播昵称）」这条链路的**整体兜底超时**（秒）。
    #:
    #: 为什么不能只靠 `request_timeout`：解析一个房间要走 `room_init` +
    #: `anchor_info`（后者 `retry=1`），实际可能叠加成 3 次请求 × 10 s。
    #: 系统代理不通时（实测黑洞代理）单次 `/api/room-info` 达到 **30.1 s**，
    #: 前端 `RoomPanel` 的输入框/按钮会被 `busy` 一直禁着 —— 用户视角就是"面板卡死"。
    #: 这里给整条链路一个截止时间，超时直接报错让用户重试（见 `PanelService._resolve_room`）。
    resolve_timeout: float = 12.0
    min_request_interval: float = 1.0
    prefer_getdanmuinfo: bool = True
    retry_max: int = 3


class ClientConfig(BaseModel):
    heartbeat_interval: float = 30.0
    heartbeat_timeout: float = 90.0
    recv_timeout: float = 5.0
    reconnect_max_delay: float = 60.0
    protover: int = 3


class SinksConfig(BaseModel):
    types: list[str] = Field(default_factory=lambda: ["jsonl", "csv", "sqlite"])
    out_dir: str = "data"
    keep_raw: bool = True
    rotate_hours: float = 1.0
    batch_size: int = 200
    flush_interval: float = 1.0
    #: 每个 sink 队列的**槽位数**（内存上限）。
    #:
    #: 它**不是**吞吐参数 —— 读循环绝不阻塞，队列只在"生产快过消费"时起作用：
    #: 满载时丢最旧并计数，所以它决定的是
    #: **能容忍 sink 卡顿多久 = queue_size / 生产速率**。
    #: （⚠️ 不要用 queue_size / 消费速率 去算：实测各 sink 消费 2.5~8.6 万条/秒，
    #: 那个口径会得出"只有 0.1 秒"的错误结论。真正的风险是 sink **卡住**时
    #: 消费速率为 0。）
    #:
    #: 取值依据（2026-10-10 实测，含 raw 时单条约 4.0 KB）：
    #:
    #: | queue_size | 3 房间内存 | 容忍卡顿 @200/s | @1000/s |
    #: |---:|---:|---:|---:|
    #: | 2,000 | 23 MB | 10 s | 2 s |
    #: | **5,000** | **57 MB** | **25 s** | **5 s** |
    #: | 20,000（旧默认） | 229 MB | 100 s | 20 s |
    #:
    #: 实测（真 Bus + jsonl/csv/sqlite，灌到 1000 条/秒）
    #: 队列峰值只有 **21 条** —— 队列是纯余量，不是瓶颈。旧默认 20000 带来 229 MB
    #: 常驻内存，换来的只是"磁盘卡满 100 秒也不丢"这种几乎用不到的能力。
    #: 5000 把内存压到 57 MB（−75%），同时仍留 25 秒余量（约等于一次典型磁盘抖动的
    #: 几十倍）。要更保守就直接调大这个值，代价是线性增长的内存。
    queue_size: int = 5000
    #: SQLite 文件路径；留空则使用 `{out_dir}/danmaku.db`
    sqlite_path: str = ""
    #: 标注库（标记/笔记）路径；留空则使用 `{out_dir}/annotations.db`。
    #: **刻意与 `danmaku.db` 分开**：采集库由 sink 掌管、会被归档清理，
    #: 而标注是人产生的，不该跟着走（见 `annotations.py` docstring）。
    annotations_path: str = ""


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    token: str = ""


class AnalysisConfig(BaseModel):
    """实时感知分析（词频）的调参项。

    需求方反馈"统计的词有一部分不符合日常表达"，这两项就是给他自己调的地方
    （比引入语言模型便宜得多，而且回退得回来）：

    * `stopwords_file`：停用词文件（一行一个，`#` 注释）。给了就**整份替换**
      内置的 `text.DEFAULT_STOPWORDS`（与 `wordcloud --stopwords` 同一语义）；
    * `custom_words`：**用户词典**，提交给 jieba，用来防止主播名/房间专名被切碎
      （`军哥` 被切成 `军`+`哥` 这类）。除了这里写死的，运行时会**自动**把
      当前监控房间的主播昵称加进去。
    """

    stopwords_file: str = ""
    custom_words: list[str] = Field(default_factory=list)
    #: 词频榜排序口径：`users` = 按"多少人说过"（默认），`count` = 按出现次数。
    #: 按人数排能自动把"一个人刷 20 遍"压下去，这正是需求方要的效果。
    rank_by: str = "users"


class Config(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    client: ClientConfig = Field(default_factory=ClientConfig)
    sinks: SinksConfig = Field(default_factory=SinksConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> "Config":
        raw: dict[str, Any] = {}

        candidates = [Path(path)] if path else [Path(DEFAULT_CONFIG_PATH)]
        for candidate in candidates:
            if candidate.is_file():
                try:
                    with candidate.open("rb") as fh:
                        raw = tomllib.load(fh)
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    raise ConfigError(f"配置文件解析失败 {candidate}: {exc}") from exc
                break

        raw = _apply_env(raw)
        if overrides:
            raw = _deep_merge(raw, overrides)

        try:
            config = cls.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"配置解析失败: {exc}") from exc

        config.validate_semantics()
        return config

    # ------------------------------------------------------------------ #
    # 语义校验：启动即失败，不留到运行期
    # ------------------------------------------------------------------ #

    def validate_semantics(self) -> None:
        c = self.client
        if c.heartbeat_interval > 30:
            raise ConfigError(
                f"client.heartbeat_interval={c.heartbeat_interval} 超过 30 秒，"
                "服务端 60 秒未收到心跳会强制断开连接"
            )
        if c.heartbeat_interval <= 0:
            raise ConfigError("client.heartbeat_interval 必须为正数")
        if c.heartbeat_timeout <= c.heartbeat_interval:
            raise ConfigError("client.heartbeat_timeout 必须大于 heartbeat_interval")
        if c.protover not in (2, 3):
            raise ConfigError(f"client.protover 只支持 2(zlib) 或 3(brotli)，得到 {c.protover}")

        if self.web.host not in LOOPBACK_HOSTS and not self.web.token:
            raise ConfigError(
                f"web.host={self.web.host} 非回环地址，必须设置 web.token，否则拒绝启动"
            )
        if not (0 <= self.web.port <= 65535):
            raise ConfigError(f"web.port={self.web.port} 非法")

        if not self.sinks.types:
            raise ConfigError("sinks.types 不能为空")
        unknown = set(self.sinks.types) - {"jsonl", "csv", "txt", "sqlite"}
        if unknown:
            raise ConfigError(f"sinks.types 含未知类型: {sorted(unknown)}")

        if self.general.max_rooms < 1:
            raise ConfigError("general.max_rooms 必须 >= 1")


# --------------------------------------------------------------------------- #
# 合并辅助
# --------------------------------------------------------------------------- #


def _apply_env(raw: dict[str, Any]) -> dict[str, Any]:
    """把 BILI_* 环境变量叠加到配置上。"""
    env = os.environ
    mapping: list[tuple[str, tuple[str, ...], type]] = [
        ("BILI_LOG_LEVEL", ("general", "log_level"), str),
        ("BILI_OUT_DIR", ("sinks", "out_dir"), str),
        ("BILI_COOKIE_FILE", ("api", "cookie_file"), str),
    ]
    for var, (section, key), caster in mapping:
        value = env.get(var)
        if value in (None, ""):
            continue
        raw.setdefault(section, {})
        if isinstance(raw[section], dict):
            try:
                raw[section][key] = caster(value)
            except ValueError:
                pass
    return raw


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out
