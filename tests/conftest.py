"""pytest 全局配置。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:  # 未安装为包时也能直接跑测试
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def danmu_samples() -> list[dict]:
    """真实抓取的 DANMU_MSG 样本（房间 804456）。"""
    path = FIXTURES / "danmu_msg_samples.json"
    if not path.is_file():
        pytest.skip(f"缺少夹具 {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def interact_samples() -> list[dict]:
    """真实抓取的 INTERACT_WORD_V2 / LOG_IN_NOTICE 样本（房间 804456）。

    INTERACT_WORD_V2 的数据是 base64 编码的 protobuf，这批夹具是字段映射的依据。
    """
    path = FIXTURES / "interact_v2_samples.json"
    if not path.is_file():
        pytest.skip(f"缺少夹具 {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def emote_samples() -> list[dict]:
    """真实抓取的**含表情**弹幕样本（房间 804456）。

    从 `data/804456_20260921-13.jsonl` 的 `raw` 里筛出来的：
    正文内嵌占位符（`[dog]` / `[捂脸]` / `[花]`），
    `info[0][15].extra.emots` 给出「占位符 → 图片元数据」的映射。
    V2 词云把表情单独成榜，靠的就是这个字段。
    """
    path = FIXTURES / "emote_samples.json"
    if not path.is_file():
        pytest.skip(f"缺少夹具 {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def priced_samples() -> list[dict]:
    """真实抓取的付费事件样本（COMBO_SEND 等）。

    实测该族命令的字段是 **snake_case**（`gift_name` / `combo_total_coin`），
    且用户信息在嵌套的 `sender_uinfo` 里 —— 与早期文档假设的 camelCase 不同。
    """
    path = FIXTURES / "priced_event_samples.json"
    if not path.is_file():
        pytest.skip(f"缺少夹具 {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def make_config(tmp_path: Path):
    """构造一个输出到 tmp_path 的测试配置。"""
    from bili_danmaku.config import Config

    def _make(**overrides: dict) -> Config:
        base: dict = {
            "sinks": {
                "out_dir": str(tmp_path),
                "types": ["jsonl"],
                "keep_raw": True,
                "batch_size": 10,
                "flush_interval": 0.05,
                "queue_size": 50,
            },
        }
        for section, values in overrides.items():
            base.setdefault(section, {}).update(values)
        return Config.load(path="definitely-not-a-real-config.toml", overrides=base)

    return _make


@pytest.fixture
def make_event():
    """构造测试用 Event。"""
    from bili_danmaku.models import Event

    def _make(**overrides) -> Event:
        data = {
            "seq": 1,
            "session_id": "test-session",
            "room_id": 804456,
            "type": "danmaku",
            "cmd": "DANMU_MSG",
            "recv_ts": 1_789_710_200.5,
            "server_ts": 1_789_710_200,
            "msg_id": "m-1",
            "uid": 1001,
            "uname": "测试用户",
            "text": "测试弹幕",
        }
        data.update(overrides)
        return Event(**data)

    return _make
