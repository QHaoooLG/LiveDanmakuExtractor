"""通用工具：日志、凭据掩码、退避、限流、脱敏。

设计约束（见 docs/PRIVACY.md）：
    任何凭据值都不得进入日志。`setup_logging()` 会安装 RedactingFilter，
    对日志文本做模式替换，作为"忘记手动掩码"的最后一道防线。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# 掩码与脱敏
# --------------------------------------------------------------------------- #

_SENSITIVE_KEYS = (
    "sessdata",
    "bili_jct",
    "dedeuserid",
    "dede_user_id",
    "dedeuserid__ckmd5",
    "buvid3",
    "buvid4",
    "cookie",
    "set-cookie",
    "authorization",
    "refresh_token",
    "access_key",
)

# 形如 SESSDATA=xxx / "SESSDATA": "xxx" / sessdata: xxx
_SECRET_PATTERNS = [
    re.compile(rf"({key}\s*=\s*)[^;\s\"',}}]+", re.IGNORECASE) for key in _SENSITIVE_KEYS
] + [
    re.compile(rf"([\"']?{key}[\"']?\s*:\s*[\"']?)[^\"',;\s}}]+", re.IGNORECASE)
    for key in _SENSITIVE_KEYS
]


def mask(value: object, keep: int = 4) -> str:
    """把凭据变成 前4****后4 的形式；短值整体星号化。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * 8}{text[-keep:]}"


def redact(text: str) -> str:
    """对任意文本做凭据替换。

    两道防线：
      1. 按 `key=value` 模式匹配（覆盖 Cookie 头、JSON 字段等常见形态）；
      2. 替换已登记的密钥**字面量** —— 覆盖"只打印了 token 本身、没带字段名"的情况，
         这类泄露靠模式匹配是抓不到的。
    """
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}<redacted>", out)
    for secret in sorted(_secret_values, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, "<redacted>")
    return out


#: 已登记的凭据字面量。太短的值不登记，否则会误伤正常文本。
_secret_values: set[str] = set()
_MIN_SECRET_LEN = 8


def register_secret(value: object) -> None:
    """登记一个凭据值；此后它出现在任何日志里都会被替换掉。"""
    if value is None:
        return
    text = str(value)
    if len(text) >= _MIN_SECRET_LEN:
        _secret_values.add(text)


def register_secrets(values: object) -> None:
    """批量登记（接受 dict / 可迭代对象）。"""
    items = values.values() if isinstance(values, dict) else values
    try:
        for item in items:  # type: ignore[union-attr]
            register_secret(item)
    except TypeError:
        return


def clear_registered_secrets() -> None:
    """清空登记表（仅供测试使用）。"""
    _secret_values.clear()


class RedactingFilter(logging.Filter):
    """在日志落盘/上屏前统一清洗凭据。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志本身不能抛
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def anonymize_uid(uid: int | None, salt: str) -> str | None:
    """uid 加盐哈希（--anonymize 用）。"""
    if uid in (None, 0):
        return None
    digest = hashlib.sha256(f"{salt}:{uid}".encode()).hexdigest()
    return f"u_{digest[:12]}"


def anonymize_uname(uname: str | None, salt: str) -> str | None:
    """昵称加盐哈希（--anonymize 用）。"""
    if not uname:
        return None
    digest = hashlib.sha256(f"{salt}:{uname}".encode()).hexdigest()
    return f"user_{digest[:6]}"


def is_masked_identity(uid: int | None, uname: str | None) -> bool:
    """判断该条记录的身份是否被平台打码（实测 uid=0 / 昵称含 ***）。"""
    if not uid:
        return True
    return bool(uname) and "***" in uname


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(
    level: str | int = "INFO",
    *,
    log_file: str | Path | None = None,
) -> None:
    """配置根日志器，并安装凭据清洗过滤器。

    `log_file` 非空时**额外**挂一个文件 handler（同时保留 stderr）：

    * 这是 `--windowed` 打包 / 托盘应用**唯一**的日志出口 ——
      GUI 子系统进程没有 stderr，只写 `StreamHandler(sys.stderr)` 等于什么都不写
      （实测：exe 退出后 `.runtime/web.log` 是 0 字节）。
    * 文件以 **UTF-8** 写入，避免中文在非 UTF-8 机器上写成乱码。
    * 父目录不存在会自动创建。
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)
    redactor = RedactingFilter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(redactor)

    handlers: list[logging.Handler] = [stderr_handler]

    if log_file:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            handlers.append(file_handler)
        except OSError:
            # 写不了文件不是致命问题 —— 退回只用 stderr（例如目录只读）
            stderr_handler.addFilter(_LogFileFailureFilter(path))

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    root.addFilter(redactor)

    # httpx 的请求日志会带上完整 URL（含 query 里的敏感参数），压到 WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class _LogFileFailureFilter(logging.Filter):
    """文件 handler 建不起来时，往 stderr 补一条提示（只补一次）。"""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self._warned = False

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._warned:
            self._warned = True
            record.msg = f"[日志文件不可写: {self.path}] {record.msg}"
            record.args = ()
        return True


# --------------------------------------------------------------------------- #
# 退避与限流
# --------------------------------------------------------------------------- #


def backoff_delay(
    attempt: int, base: float = 1.0, cap: float = 60.0, jitter: bool = True
) -> float:
    """指数退避：attempt 从 0 开始 → 1,2,4,...,cap，附带最多 1s 抖动。"""
    delay = min(base * (2**max(attempt, 0)), cap)
    if jitter:
        delay += random.uniform(0.0, 1.0)
    return delay


class RateLimiter:
    """异步最小间隔限流器（诚实串行化，不做令牌桶）。"""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(min_interval, 0.0)
        self._last: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #


def now() -> float:
    """墙钟时间（秒，浮点）。"""
    return time.time()


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def harden_file_permissions(path: str) -> None:
    """尽力把凭据文件收紧到仅当前用户可读。失败不抛异常。"""
    try:
        if os.name == "nt":
            import subprocess

            user = os.environ.get("USERNAME")
            if not user:
                return
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                check=False,
                capture_output=True,
            )
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - 权限收紧失败不应阻断主流程
        pass
