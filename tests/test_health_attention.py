"""采集健康提示（需求：凭据/风控失效要**看得见**）。

两块被测的东西：

1. `analysis.assess_health()` —— **纯函数**，把房间快照翻成"现在需要我做什么"。
2. `collector._remember_error()` —— 修掉一个真 bug：异常分支原来写成
   `log.warning("连接失败: %s: %s", type(exc).__name__, exc)`，而
   `AuthFailed` / `BiliApiError` **自己是空消息** ⇒ `%s` 打出来是空的，
   日志与面板都看不出原因，用户只看到"重连中"。
"""

from __future__ import annotations

import pytest

from bili_danmaku.analysis import assess_health
from bili_danmaku.client import AuthFailed
from bili_danmaku.collector import RoomCollector
from bili_danmaku.config import Config


# --------------------------------------------------------------------------- #
# assess_health：纯函数
# --------------------------------------------------------------------------- #

def _room(**fields):
    """一个最小的"房间快照"（dict 形态，`assess_health` 两种都支持）。"""
    base = {
        "room_id": 804456,
        "conn_state": "STREAMING",
        "reconnects": 0,
        "last_error": "",
        "last_error_kind": "",
    }
    base.update(fields)
    return base


def test_healthy_rooms_produce_no_alert() -> None:
    assert assess_health([_room()])["level"] == "ok"
    assert assess_health([_room(), _room(room_id=2)])["level"] == "ok"


def test_no_rooms_is_ok() -> None:
    """面板还没加房间时不该报警（那是"没开始"，不是"坏了"）。"""
    assert assess_health([])["level"] == "ok"


def test_auth_failure_is_alert_and_says_relogin() -> None:
    """需求核心：凭证失效要给出**能照做**的话，而不是只报个状态。"""
    result = assess_health(
        [_room(conn_state="RECONNECTING", last_error_kind="auth", last_error="认证失败（AuthFailed）")]
    )
    assert result["level"] == "alert"
    assert "登录" in result["title"] + result["detail"]
    assert "扫码" in result["detail"] or "重新登录" in result["detail"]
    assert result["rooms"] == [804456]


def test_paused_after_repeated_auth_failure_is_warn() -> None:
    """连续认证失败后采集会 PAUSED —— 这也要提示，但不必吓成红色。"""
    result = assess_health([_room(conn_state="PAUSED", last_error="认证失败（AuthFailed）")])
    assert result["level"] == "warn"
    assert "暂停" in result["title"]


def test_single_reconnect_is_not_reported() -> None:
    """**别看到一次重连就报警**：B 站会主动断连，偶发一次很正常。"""
    assert assess_health([_room(reconnects=1)])["level"] == "ok"
    assert assess_health([_room(reconnects=2)])["level"] == "ok"


def test_many_reconnects_is_warn() -> None:
    result = assess_health([_room(reconnects=9)])
    assert result["level"] == "warn"
    assert "重连" in result["title"] + result["detail"]
    assert "9" in result["detail"]


def test_network_error_with_few_reconnects_is_warn() -> None:
    result = assess_health(
        [_room(reconnects=3, last_error_kind="network", last_error="连接失败（OSError）：timed out")]
    )
    assert result["level"] == "warn"
    assert "网络" in result["title"] + result["detail"]


def test_auth_outranks_network() -> None:
    """多个房间各有各的问题时，**最严重的那个**决定提示等级。"""
    result = assess_health(
        [
            _room(room_id=1, reconnects=20, last_error_kind="network"),
            _room(room_id=2, last_error_kind="auth", last_error="认证失败（AuthFailed）"),
        ]
    )
    assert result["level"] == "alert"
    assert result["rooms"] == [2], "只报真正需要用户动手的那些房间"


def test_accepts_objects_not_only_dicts() -> None:
    """`PanelService.rooms()` 返回 dict，但也要能接对象 —— 便于直接传 RoomRuntime。"""

    class _Room:
        room_id = 7
        conn_state = "RECONNECTING"
        reconnects = 0
        last_error = "认证失败（AuthFailed）"
        last_error_kind = "auth"

    result = assess_health([_Room()])
    assert result["level"] == "alert"
    assert result["rooms"] == [7]


# --------------------------------------------------------------------------- #
# _remember_error：修掉的空消息 bug
# --------------------------------------------------------------------------- #

def _remember_error(stats, kind: str, exc: BaseException, *, fallback: str) -> None:
    """直接调用真实实现（`_remember_error` 只用到 `self.stats`）。

    刻意不构造整个 `RoomCollector`（要 api / bus / sink 三件套）：
    这个方法是**记账**，用最小宿主就能验证；端到端的部分见
    `tests/test_collector.py::test_collector_records_error_reason`。
    """
    RoomCollector._remember_error(_Host(stats), kind, exc, fallback=fallback)


class _Host:
    def __init__(self, stats) -> None:
        self.stats = stats


def test_remember_error_falls_back_when_message_is_empty() -> None:
    """`BiliApiError("")` / `TimeoutError()` 的 `str(exc)` 都是空串（实测）。

    只写 `str(exc)` 会让面板与日志都没有线索，所以必须带 fallback + 类型名。
    """
    from bili_danmaku.analysis import analyze  # noqa: F401 - 保持与本文件主题一致的导入
    from bili_danmaku.collector import CollectorStats

    exc = TimeoutError()
    assert str(exc) == "", "前置条件：这个异常自己就是空消息"

    stats = CollectorStats()
    _remember_error(stats, "network", exc, fallback="连接失败")

    assert stats.last_error_kind == "network"
    assert stats.last_error, "错误原因不能是空串"
    assert "连接失败" in stats.last_error
    assert "TimeoutError" in stats.last_error


def test_remember_error_includes_detail_when_present() -> None:
    from bili_danmaku.collector import CollectorStats

    stats = CollectorStats()
    _remember_error(stats, "network", OSError("connection reset"), fallback="连接失败")
    assert stats.last_error_kind == "network"
    assert "connection reset" in stats.last_error
    assert "OSError" in stats.last_error


def test_auth_error_is_translated_to_actionable_text() -> None:
    """**需求核心**：`code=-101` 是"登录失效"，必须翻成人话。

    `AuthFailed` 自己的文案是 `认证失败 code=-101` —— 对用户毫无意义，
    不翻译的话采集中凭据过期时用户只看到一串负数，
    不知道该重新扫码（这正是"凭据失效要看得见、而且要能照做"要解决的）。
    """
    from bili_danmaku.collector import CollectorStats

    stats = CollectorStats()
    RoomCollector._remember_auth_error(_Host(stats), AuthFailed(-101), -101)

    assert stats.last_error_kind == "auth"
    assert "登录已失效" in stats.last_error
    assert "扫码" in stats.last_error
    assert "-101" in stats.last_error, "错误码要保留，便于排查"


def test_unknown_auth_code_still_readable() -> None:
    """没见过的认证码也要给出可读回退，而不是空话或崩溃。"""
    from bili_danmaku.collector import CollectorStats

    stats = CollectorStats()
    RoomCollector._remember_auth_error(_Host(stats), AuthFailed(99999), 99999)
    assert "认证被服务端拒绝" in stats.last_error
    assert "99999" in stats.last_error


def test_error_fields_are_exposed_in_stats_dict() -> None:
    """面板只能拿快照 —— 这两个字段必须进 `as_dict()`，否则前端看不到。"""
    from bili_danmaku.collector import CollectorStats

    stats = CollectorStats()
    RoomCollector._remember_auth_error(_Host(stats), AuthFailed(-101), -101)
    data = stats.as_dict()
    assert data["last_error"] == stats.last_error
    assert data["last_error_kind"] == "auth"
