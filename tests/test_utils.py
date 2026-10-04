"""`setup_logging` 的用例。

重点守一个**实测踩到的坑**：`setup_logging()` 默认只写 stderr，
而 `--windowed` 打包的 exe（以及托盘进程）**没有 stderr** ——
于是"退出后 `.runtime/web.log` 是 0 字节"，用户拿不到任何排查信息。
"""

from __future__ import annotations

import logging
from pathlib import Path

from bili_danmaku.utils import setup_logging


def _reset_root() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
    root.handlers.clear()


def test_setup_logging_without_file_only_uses_stderr() -> None:
    """不传 log_file：行为与以前一致（只有 stderr handler），不产生文件。"""
    setup_logging("INFO")
    try:
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert not isinstance(root.handlers[0], logging.FileHandler)
    finally:
        _reset_root()


def test_setup_logging_writes_file_when_requested(tmp_path: Path) -> None:
    """传 log_file：**真的落盘**，并且保留 stderr（两者都要）。"""
    log_file = tmp_path / "sub" / "web.log"  # 父目录不存在，应自动创建
    setup_logging("INFO", log_file=log_file)
    try:
        root = logging.getLogger()
        assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root.handlers
        )

        logging.getLogger("bili_danmaku.test").info("落盘测试 ✅")
        for handler in root.handlers:
            handler.flush()

        assert log_file.is_file(), "日志文件没有被创建"
        content = log_file.read_text(encoding="utf-8")
        assert "落盘测试" in content, "日志文件里没有写入的内容"
        assert "✅" in content, "文件必须按 UTF-8 写（否则中文/emoji 会乱码）"
    finally:
        _reset_root()


def test_setup_logging_appends_instead_of_truncating(tmp_path: Path) -> None:
    """重复调用应**追加**，而不是把上一轮日志清掉（托盘重启会调多次）。"""
    log_file = tmp_path / "web.log"
    setup_logging("INFO", log_file=log_file)
    try:
        logging.getLogger("bili_danmaku.test").info("第一次")
        for handler in logging.getLogger().handlers:
            handler.flush()
    finally:
        _reset_root()

    setup_logging("INFO", log_file=log_file)
    try:
        logging.getLogger("bili_danmaku.test").info("第二次")
        for handler in logging.getLogger().handlers:
            handler.flush()
    finally:
        _reset_root()

    content = log_file.read_text(encoding="utf-8")
    assert "第一次" in content
    assert "第二次" in content


def test_setup_logging_survives_unwritable_log_path(tmp_path: Path) -> None:
    """日志文件写不了也不能崩 —— 退回只用 stderr（并提示一次）。"""
    # 用一个"目录"当文件名，制造必然的写入失败
    bad = tmp_path / "not_a_file"
    bad.mkdir()

    setup_logging("INFO", log_file=bad)
    try:
        root = logging.getLogger()
        # 仍在工作：至少有 stderr handler，且没有 FileHandler
        assert any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root.handlers
        )
        logging.getLogger("bili_danmaku.test").warning("这条应该还能打出来")
    finally:
        _reset_root()


def test_redaction_filter_still_attached_to_both_handlers(tmp_path: Path) -> None:
    """凭据清洗过滤器必须挂在**每个** handler 上（含新增的文件 handler）。"""
    log_file = tmp_path / "web.log"
    setup_logging("INFO", log_file=log_file)
    try:
        for handler in logging.getLogger().handlers:
            assert handler.filters, f"{handler} 上没有 RedactingFilter"
    finally:
        _reset_root()
