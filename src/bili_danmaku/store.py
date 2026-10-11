"""SQLite 读取层：`stats` / `export` / `replay` / **面板历史分页** 的查询实现。

以**只读方式**打开数据库（`file:...?mode=ro`），避免分析操作意外写坏采集数据。

关键词检索走 `LIKE` 子串匹配，**刻意不用 FTS5**：

* `unicode61` 分词器把连续中文当成单个 token —— 搜「主播」匹配不到「主播好」；
* `trigram` 分词器要求查询 ≥3 字符，而中文弹幕关键词通常只有 2 个字（「哈哈」「主播」）。

对本项目的量级（万~十万行）`LIKE` 的扫描成本可以接受，且语义正确。

## 历史分页为什么用 `(recv_ts, id)` 复合游标（V3-a 实测结论）

* `recv_ts` **会并列**：实测房间 804456 有 393 组重复时间戳，最多 10 条同刻
  （同一批推送被解析成同一 `recv_ts`）。只用 `recv_ts` 做游标会在页边界**漏条**，
  所以必须带 `id` 作为次级键 —— 复合比较既不漏也不重。
* 但 `ORDER BY` 必须与游标键**一致**（都是 `recv_ts DESC, id DESC`）。若写成
  `ORDER BY id DESC` 而游标用 `recv_ts`，排序键与过滤键不一致，同样会漏条。
* 查询计划实测：
  - 带 `room_id` 时走 `SEARCH event USING COVERING INDEX idx_event_room_ts (room_id=?)`，
    **没有临时排序**（`rowid` 是索引的隐式尾列，`(recv_ts, id)` 顺序天然可用），0.3 ms / 400 行；
  - **不带** `room_id` 时退化为 `SCAN` + `USE TEMP B-TREE FOR ORDER BY`（39.8 ms），
    所以"全部房间"的历史浏览必须用**时间窗**收窄（见 `iter_history(window_s=...)`），
    否则每翻一页都是一次全表扫描 + 排序。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from .models import Event


class StoreError(Exception):
    """数据库不可用或查询失败。"""


def _load_extra(payload: Any) -> dict[str, Any]:
    """`extra_json` → dict（旧库缺列或载荷损坏时返回空字典，不抛异常）。"""
    if not payload:
        return {}
    try:
        loaded = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


#: 历史分页单页上限（防止一个请求把整库拖进内存）
MAX_HISTORY_LIMIT = 1000
#: 历史分页默认页大小
DEFAULT_HISTORY_LIMIT = 400
#: 无房间过滤时默认的时间窗（秒）：把全表扫描收窄成索引区间扫描
DEFAULT_HISTORY_WINDOW_S = 3600.0

#: 面板历史视图真正需要的列 —— `raw_json` 与 `dedupe_key` 刻意不在其中。
#: 实测完整 `Event` 的线上体积约 2.0 KB/条（含 raw），400 条一页接近 800 KB，
#: 足以触发服务端 `SLOW_CONSUMER` 断连；瘦投影后约 0.3 KB/条。
HISTORY_COLUMNS: tuple[str, ...] = (
    "id",
    "room_id",
    "session_id",
    "recv_ts",
    "type",
    "uid",
    "uname",
    "text",
    "price",
    "medal_name",
    "medal_level",
    "extra_json",
)

#: 游标分页允许的过滤参数（`recv_ts` 由游标边界自己管，不能重复传）
CURSOR_FILTERS = ("session_id", "room_id", "types", "keyword", "since", "until")


class DanmakuStore:
    """只读查询接口。"""

    #: 用于把 DB 行还原成 Event 的列
    EVENT_COLUMNS = (
        "id",
        "session_id",
        "room_id",
        "recv_ts",
        "server_ts",
        "latency_ms",
        "type",
        "cmd",
        "source",
        "msg_id",
        "uid",
        "uname",
        "text",
        "price",
        "guard_level",
        "user_level",
        "medal_name",
        "medal_level",
        "field_source",
        "raw_json",
        "extra_json",
    )

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        if not self.path.is_file():
            raise StoreError(f"数据库不存在: {self.path}")
        try:
            # `check_same_thread=False` 是必需的（由面板 `/api/export` 的缺陷暴露）：
            # Starlette 用线程池执行同步生成器，一次流式导出的各次 `next()` 可能落在
            # 不同工作线程上，默认的线程检查会直接抛
            # `sqlite3.ProgrammingError: SQLite objects created in a thread can only be
            # used in that same thread`。本对象是**只读**查询层，且始终串行使用，
            # 关掉该检查不会引入并发写风险。
            self._conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise StoreError(f"无法打开数据库 {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        # **夹住这条连接的页缓存**。
        #
        # 面板是"每请求开一条只读连接"（`_run_with_store`），而
        # `anyio.to_thread` 默认 40 个工作线程 ⇒ 最多 40 条连接同时在。
        # SQLite 默认 `cache_size = -2000`（约 2 MB）⇒ **约 80 MB 重复页缓存**，
        # 而且它们缓存的是同一个只读文件、内容完全一样。
        #
        # 512 KB/连接对我们这种"每次只扫一个时间窗口"的查询足够，
        # 40 条并发也只有约 20 MB。真正的大查询（导出、密度聚合）是流式的，
        # 不吃页缓存。
        self._conn.execute("PRAGMA cache_size = -512")
        self._columns = {row[1] for row in self._conn.execute("PRAGMA table_info(event)")}

    @property
    def connection(self) -> sqlite3.Connection:
        """底层的**只读**连接（供调用方执行自己的 PRAGMA / 轻量查询）。

        暴露它而不是让调用方去摸 `_conn`：这是只读查询层，
        给一个明确的公开入口比"大家都知道那个私有字段"更诚实。
        写入请走 `SqliteSink`。
        """
        return self._conn

    def has_column(self, name: str) -> bool:
        """表里有没有这一列。

        本层是**只读**的，不会像 `SqliteSink` 那样执行 ALTER 迁移，
        所以旧库可能缺列（例如词云要用的 `extra_json`）。
        查询前必须先问一句，否则会抛 `no such column` —— 这不是配置错误，
        而是"老数据 + 新代码"的正常组合，应该降级而不是崩。
        """
        return name in self._columns

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "DanmakuStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def sessions(self, limit: int = 50, *, room_id: int | None = None) -> list[dict[str, Any]]:
        """抓取会话列表（按开始时间倒序）。

        每行的 `events` 是该会话的事件数。注意它来自 `COUNT(*)` 子查询，
        与 `event.id` 的**空洞无关**（`id` 被去重跳过不影响会话内计数）。
        """
        where = " WHERE s.room_id = ?" if room_id is not None else ""
        params: list[Any] = [room_id] if room_id is not None else []
        rows = self._conn.execute(
            f"""
            SELECT s.id, s.room_id, s.started_at, s.ended_at,
                   (SELECT COUNT(*) FROM event e WHERE e.session_id = s.id) AS events
            FROM session s{where}
            ORDER BY s.started_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    def _build_where(
        self,
        *,
        session_id: str | None = None,
        room_id: int | None = None,
        types: Sequence[str] | None = None,
        keyword: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if room_id is not None:
            clauses.append("room_id = ?")
            params.append(room_id)
        if types:
            placeholders = ",".join("?" * len(types))
            clauses.append(f"type IN ({placeholders})")
            params.extend(types)
        if since is not None:
            clauses.append("recv_ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("recv_ts <= ?")
            params.append(until)
        if keyword:
            # LIKE 子串匹配：对中文语义正确（见模块 docstring 中不用 FTS5 的原因）。
            # 转义 LIKE 的通配符，避免用户输入的 % 和 _ 变成通配。
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("text LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    # ------------------------------------------------------------------ #
    def count(self, **filters: Any) -> int:
        where, params = self._build_where(**filters)
        row = self._conn.execute(f"SELECT COUNT(*) AS n FROM event{where}", params).fetchone()
        return int(row["n"]) if row else 0

    def by_type(self, **filters: Any) -> dict[str, int]:
        where, params = self._build_where(**filters)
        rows = self._conn.execute(
            f"SELECT type, COUNT(*) AS n FROM event{where} GROUP BY type ORDER BY n DESC", params
        ).fetchall()
        return {row["type"]: int(row["n"]) for row in rows}

    def by_source(self, **filters: Any) -> dict[str, int]:
        where, params = self._build_where(**filters)
        rows = self._conn.execute(
            f"SELECT source, COUNT(*) AS n FROM event{where} GROUP BY source", params
        ).fetchall()
        return {row["source"]: int(row["n"]) for row in rows}

    def count_by_room(self, **filters: Any) -> dict[int, int]:
        """按房间统计条数（`{room_id: n}`）。

        用于导出时挑"这份报告的代表房间"（文件名里要一个房间号，见
        `server/app.py::_busiest_room`）。按 `room_id` 分组会走
        `idx_event_room_ts_type` 的索引顺序，不触发临时 B 树。
        """
        where, params = self._build_where(**filters)
        rows = self._conn.execute(
            f"SELECT room_id, COUNT(*) AS n FROM event{where} GROUP BY room_id", params
        ).fetchall()
        return {int(row["room_id"]): int(row["n"]) for row in rows}

    def room_anchor_names(
        self, room_ids: Iterable[int], *, limit: int = 500
    ) -> dict[int, str]:
        """按房间取**留言最多的那个昵称**，当"主播昵称"用（`{room_id: uname}`）。

        用途（需求 #8）：房间号下拉列表里，对"曾采集过的房间"顺带显示主播昵称。

        ## 为什么是"留言最多的昵称"而不是真的主播名

        **数据库里没有主播名**。采集库里只有弹幕发送者（`uname`），主播昵称要么
        来自正在监控的房间运行时缓存，要么得去调 B 站接口（`get_anchor_in_room`）。
        为了一个下拉列表去调网络接口是不划算的（还会给风控多送请求）。

        而"留言最多的昵称"在直播间里**几乎总是主播自己** —— 主播发弹幕比谁都勤，
        这是直播间弹幕数据的常识性特征。所以这里当成**启发式**用它，并在界面上
        明确标注"曾采集"（不是"正在监控"），避免让用户以为这是权威信息。

        `limit` 是房间数上限：调用方只传下拉列表里那几个房间号，不会失控。

        ## 为什么用 `fetchmany` 而不是 `fetchall`

        `limit` 只夹住**房间数**，夹不住**返回的行数** —— 分组键是
        `(room_id, uname)`，一个热门房间有 10 万个不同昵称就有 10 万行。
        `fetchall()` 会把它们全部物化成 `sqlite3.Row` 对象，
        而这里其实**每个房间只留第一行**。流式读取时一旦凑齐 `len(wanted)` 个
        房间就可以提前收手，大房间里通常几百行就够。
        """
        wanted = [int(item) for item in room_ids if item is not None][: max(int(limit), 1)]
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        # 每个房间取 `count` 最大的 uname；并列时用 uname 兜底排序，保证结果稳定
        cursor = self._conn.execute(
            "SELECT room_id, uname, COUNT(*) AS n FROM event "
            f"WHERE room_id IN ({placeholders}) AND uname IS NOT NULL AND uname != '' "
            "GROUP BY room_id, uname ORDER BY n DESC, uname ASC",
            wanted,
        )
        names: dict[int, str] = {}
        while len(names) < len(wanted):
            rows = cursor.fetchmany(200)
            if not rows:
                break
            for row in rows:
                room = int(row["room_id"])
                if room in names:
                    continue  # 已取过该房间的第一名（结果按 n 降序）
                names[room] = str(row["uname"])
            if len(names) >= len(wanted):
                break
        return names

    def time_range(self, **filters: Any) -> tuple[float | None, float | None]:
        where, params = self._build_where(**filters)
        row = self._conn.execute(
            f"SELECT MIN(recv_ts) AS lo, MAX(recv_ts) AS hi FROM event{where}", params
        ).fetchone()
        if not row or row["lo"] is None:
            return None, None
        return float(row["lo"]), float(row["hi"])

    def latency_percentiles(self, **filters: Any) -> tuple[int | None, int | None]:
        """P50 / P95 延迟（毫秒）。用 ORDER BY + OFFSET 取值，避免把全部数据读进内存。"""
        where, params = self._build_where(**filters)
        clause = f"{where} AND latency_ms IS NOT NULL" if where else " WHERE latency_ms IS NOT NULL"

        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM event{clause}", params
        ).fetchone()
        total = int(row["n"]) if row else 0
        if total == 0:
            return None, None

        def _at(fraction: float) -> int | None:
            offset = min(int(total * fraction), total - 1)
            result = self._conn.execute(
                f"SELECT latency_ms FROM event{clause} ORDER BY latency_ms LIMIT 1 OFFSET ?",
                [*params, offset],
            ).fetchone()
            return int(result["latency_ms"]) if result else None

        return _at(0.50), _at(0.95)

    def top_senders(self, *, limit: int = 10, **filters: Any) -> list[dict[str, Any]]:
        where, params = self._build_where(**filters)
        clause = (
            f"{where} AND uid IS NOT NULL AND uid != 0"
            if where
            else " WHERE uid IS NOT NULL AND uid != 0"
        )
        rows = self._conn.execute(
            f"""
            SELECT uid, MAX(uname) AS uname, COUNT(*) AS n
            FROM event{clause}
            GROUP BY uid ORDER BY n DESC LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [{"uid": row["uid"], "uname": row["uname"], "count": int(row["n"])} for row in rows]

    def room_ids(self) -> list[int]:
        """库里出现过事件的房间号（升序）。

        用途：`/api/stats` 的"全部房间"发言榜要**按房间分别查再合并**
        （见 `app._window_top_senders`）—— 那条路走 `idx_event_room_ts_type`，
        比"全库 `GROUP BY uid`"快好几个数量级。

        **实测**（`dist/exe/data/danmaku.db`，1 GB / 244,036 行、9 个房间）：
        `SELECT DISTINCT room_id FROM event` = **16 ms**，计划是
        `SCAN event USING COVERING INDEX idx_event_room_ts`（纯索引扫描、不回表）。

        ⚠️ 不要改用 `count_by_room()` 来取房间号：它带 `recv_ts` 过滤时会退化成
        整索引扫描（实测 17~227 ms），而我们只要房间号。
        """
        rows = self._conn.execute(
            "SELECT DISTINCT room_id FROM event ORDER BY room_id"
        ).fetchall()
        return [int(row["room_id"]) for row in rows]

    def top_gifts(self, *, limit: int = 10, **filters: Any) -> list[dict[str, Any]]:
        where, params = self._build_where(**filters)
        clause = (
            f"{where} AND price IS NOT NULL" if where else " WHERE price IS NOT NULL"
        )
        rows = self._conn.execute(
            f"""
            SELECT type, COALESCE(SUM(price), 0) AS total, COUNT(*) AS n
            FROM event{clause} GROUP BY type ORDER BY total DESC LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return [
            {"type": row["type"], "total_price": int(row["total"]), "count": int(row["n"])}
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    @property
    def _event_columns_sql(self) -> str:
        """只选出数据库中**实际存在**的列（旧库缺 `extra_json` 时自动降级）。"""
        columns = [name for name in self.EVENT_COLUMNS if name in self._columns]
        if "extra_json" in self._columns and "extra_json" not in columns:
            columns.append("extra_json")
        return ", ".join(columns)

    def iter_events(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        batch_size: int = 1000,
        slim: bool = False,
        **filters: Any,
    ) -> Iterator[Any]:
        """流式产出事件，避免一次性载入内存（导出大结果集时用）。

        `slim=True` 走**瘦投影**：
        不构造 pydantic `Event`、不带原始帧，直接由行拼 dict。

        为什么这是导出的大头：`_to_event()` 每条都要构造一个 pydantic `Event`
        再 `model_dump()` 序列化，实测 5 万条要 14~16 秒；而"原始帧"在导出里
        只是一列转义 JSON（表格化之后既看不了也搜不动）。默认导出因此走瘦投影，
        需要取证时显式要 `raw=1`。

        ⚠️ 之后 `raw` 走的是 **JSON 文本**、**不再有 `json.loads` 往返**
        （见 `models.Event.raw`），所以这条路径剩下的唯一大头就是 pydantic 的构造与序列化。

        ## `offset`：分页必须**下推到 SQL**（P0-1，2026-10-10）

        本方法按 `id` 升序流式产出，天然支持 `LIMIT ? OFFSET ?`。
        调用方**不要**自己去 `list(...)` 再切片：那会把前 `offset` 行也整行取出、
        逐行 `_to_event()`（构造 pydantic + 携带原始帧），**内存随 offset 线性增长**。
        `/api/events` 之前就是这么写的（`limit=limit + offset` 再切片），
        而 `offset` 没有上限 ⇒ `offset=200000` 时会一次物化 20 万个 Event，
        是实打实的 OOM 口子。下推到 SQL 之后，只有 `limit` 行会被构造。
        """
        where, params = self._build_where(**filters)
        sql = f"SELECT {self._event_columns_sql} FROM event{where} ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params = [*params, max(int(limit), 0)]
            skip = max(int(offset), 0)
            if skip:
                sql += " OFFSET ?"
                params = [*params, skip]
        elif offset:
            # 只给 offset 不给 limit 的语义太容易误用（等于"跳过 N 条然后全取"），
            # 直接拒绝而不是静默忽略。
            raise ValueError("iter_events(offset=...) 必须同时给出 limit")

        cursor = self._conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield self._to_event(row, with_raw=not slim)

    # ------------------------------------------------------------------ #
    # 历史浏览（V3-a）：游标分页 + 瘦投影 + 密度聚合
    #
    # 这一组方法与 `iter_events` 的区别是**有意为之**的：
    #   * `iter_events` 面向导出/重放：要完整 Event、按 id 升序、可流式；
    #   * 这一组面向面板翻页：要瘦投影、按 (recv_ts, id) 降序、必须能用索引。
    # 不要为了"少一个方法"把它们合并 —— 两边的排序语义和载荷宽度是相反的。
    # ------------------------------------------------------------------ #
    @staticmethod
    def _project(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
        """把事件行转成面板历史视图需要的瘦字典。

        刻意**不**构造 `Event`：那条路要解析 `raw_json`（单条 ~2 KB），
        对"翻页显示"是几十倍的浪费。`extra` 只保留 `emots`（表情图片映射），
        其余分析型字段面板渲染用不到。
        """
        items: list[dict[str, Any]] = []
        for row in rows:
            keys = row.keys()
            extra = _load_extra(row["extra_json"] if "extra_json" in keys else None)
            emots = extra.get("emots")
            items.append(
                {
                    "id": int(row["id"]),
                    "room_id": int(row["room_id"]),
                    "session_id": row["session_id"],
                    "recv_ts": float(row["recv_ts"]),
                    "type": row["type"],
                    "uid": row["uid"],
                    "uname": row["uname"],
                    "text": row["text"],
                    "price": row["price"],
                    "medal_name": row["medal_name"],
                    "medal_level": row["medal_level"],
                    "emots": emots if isinstance(emots, dict) else None,
                }
            )
        return items

    def _history_columns_sql(self) -> str:
        """瘦投影需要的、且**库里确实存在**的列（旧库缺 `extra_json` 时降级）。"""
        columns = [name for name in HISTORY_COLUMNS if name in self._columns]
        return ", ".join(columns)

    def _window_clause(
        self,
        filters: dict[str, Any],
        *,
        window_s: float | None,
        anchor_ts: float | None,
    ) -> tuple[str, list[Any]]:
        """无房间过滤时的**兜底时间窗**。

        只有"既没有 `room_id`、也没有显式 `since/until`"时才加窗：
        全表扫描 + 临时排序实测 39.8 ms/页，加窗后退回索引区间扫描。
        显式时间过滤本身就是收窄，再叠一层默认窗反而会把用户要的数据切掉。
        """
        if filters.get("room_id") is not None:
            return "", []
        if filters.get("since") is not None or filters.get("until") is not None:
            return "", []
        span = DEFAULT_HISTORY_WINDOW_S if window_s is None else max(float(window_s), 1.0)
        center = time.time() if anchor_ts is None else float(anchor_ts)
        return "recv_ts >= ? AND recv_ts <= ?", [center - span, center + span]

    def _cursor_clause(
        self,
        *,
        before: tuple[float, int] | None,
        after: tuple[float, int] | None,
    ) -> tuple[str, list[Any]]:
        """`(recv_ts, id)` 复合游标条件。

        必须比两个键：`recv_ts` 会并列（实测同一房间有 393 组重复时间戳），
        只比 `recv_ts` 会在页边界漏条。
        """
        if before is not None:
            return (
                "(recv_ts < ? OR (recv_ts = ? AND id < ?))",
                [before[0], before[0], before[1]],
            )
        if after is not None:
            return (
                "(recv_ts > ? OR (recv_ts = ? AND id > ?))",
                [after[0], after[0], after[1]],
            )
        return "", []

    def _page_where(
        self,
        filters: dict[str, Any],
        *,
        before: tuple[float, int] | None = None,
        after: tuple[float, int] | None = None,
        window_s: float | None = None,
        anchor_ts: float | None = None,
    ) -> tuple[str, list[Any]]:
        """组装一页查询（或存在性探测）的 WHERE 子句与参数。

        返回 `(where_sql, params)` —— 刻意**不用**实例属性传递参数：
        这个对象被面板的多个请求共享（`check_same_thread=False`），
        把中间状态挂在 `self` 上会留下极难查的串参数隐患。
        """
        clauses: list[str] = []
        params: list[Any] = []

        base_where, base_params = self._build_where(**filters)
        if base_where:
            clauses.append(base_where.removeprefix(" WHERE "))
            params.extend(base_params)

        window_sql, window_params = self._window_clause(
            filters, window_s=window_s, anchor_ts=anchor_ts
        )
        if window_sql:
            clauses.append(window_sql)
            params.extend(window_params)

        cursor_sql, cursor_params = self._cursor_clause(before=before, after=after)
        if cursor_sql:
            clauses.append(cursor_sql)
            params.extend(cursor_params)

        return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def _exists(
        self,
        filters: dict[str, Any],
        *,
        before: tuple[float, int] | None = None,
        after: tuple[float, int] | None = None,
        window_s: float | None = None,
        anchor_ts: float | None = None,
    ) -> bool:
        """相对某个游标位置，还有没有更多事件（`LIMIT 1` 存在性探测）。"""
        where, params = self._page_where(
            filters,
            before=before,
            after=after,
            window_s=window_s,
            anchor_ts=anchor_ts,
        )
        row = self._conn.execute(
            f"SELECT 1 FROM event{where} ORDER BY recv_ts DESC, id DESC LIMIT 1", params
        ).fetchone()
        return row is not None

    def iter_history(
        self,
        *,
        limit: int = DEFAULT_HISTORY_LIMIT,
        before: tuple[float, int] | None = None,
        after: tuple[float, int] | None = None,
        window_s: float | None = None,
        anchor_ts: float | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """游标分页取一页历史事件（**升序返回**，便于直接渲染）。

        `before=(recv_ts, id)`：取**更旧**的一页（向上翻）；
        `after=(recv_ts, id)`：取**更新**的一页（向下翻）；两者互斥。

        `window_s` / `anchor_ts`：**没有 `room_id` 过滤**时的兜底时间窗与锚点
        （`anchor_ts` 缺省用墙钟；真实调用应传数据里最新的 `recv_ts`，
        否则"最后一次抓取是三天前"的库会翻不到东西）。

        **`limit` 作用于"命中行"**：带 `keyword`/`types` 过滤时，窗口会自动往更旧的
        时间戳延伸直到凑满 `limit` 条命中（而不是"先截最新 N 条再过滤"）。
        翻页搜索要的正是前者；代价是过滤很严时单页可能跨很长的时间跨度。

        返回 `(items, has_older, has_newer)`，是**相对这一页**的判断。
        """
        if before is not None and after is not None:
            raise ValueError("before 与 after 不能同时使用")
        limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        filters = {key: value for key, value in filters.items() if key in CURSOR_FILTERS}

        where, params = self._page_where(
            filters, before=before, after=after, window_s=window_s, anchor_ts=anchor_ts
        )
        sql = (
            f"SELECT {self._history_columns_sql()} FROM event{where}"
            " ORDER BY recv_ts DESC, id DESC LIMIT ?"
        )
        rows = self._conn.execute(sql, [*params, limit]).fetchall()

        if not rows:
            # 空页：`has_older` 不可能为真（我们问的是"更旧的"）。
            # `has_newer` 只有在"客户端明确要更新的一页"时才能确定为假；
            # 首次打开（无游标）时为空 = 这个范围内什么都没有。
            if after is not None or before is None:
                return [], False, False
            return [], False, True

        items = self._project(rows)
        items.reverse()  # 降序取、升序回

        has_older = self._exists(
            filters,
            before=(items[0]["recv_ts"], items[0]["id"]),
            window_s=window_s,
            anchor_ts=anchor_ts,
        )
        has_newer = self._exists(
            filters,
            after=(items[-1]["recv_ts"], items[-1]["id"]),
            window_s=window_s,
            anchor_ts=anchor_ts,
        )
        return items, has_older, has_newer

    def density(
        self,
        *,
        bucket_s: float = 60.0,
        since: float | None = None,
        until: float | None = None,
        include_types: Iterable[str] | None = None,
        cancel_event: Any = None,
        with_types: bool = False,
        **filters: Any,
    ) -> dict[str, Any]:
        """时间轴密度：把事件按 `bucket_s` 聚合成桶（V3-a 的时间轴数据源）。

        **为什么不直接 `GROUP BY CAST(recv_ts/bucket AS INTEGER)`**：对表达式分组时
        SQLite 用不上索引顺序，退化成 `USE TEMP B-TREE FOR GROUP BY`。实测 20 万行：

        | 写法 | 查询计划 | 耗时 |
        |------|----------|------|
        | `GROUP BY CAST(recv_ts/b AS INT), type` | 索引 + TEMP B-TREE | 118 ms |
        | `GROUP BY recv_ts, type`（本实现） | `SEARCH ... USING COVERING INDEX idx_event_room_ts_type` | 38 ms |

        所以这里按**原始 `recv_ts`** 分组、在 Python 里折算桶（行数 = 不同时间戳数，
        远小于事件数），把"表达式分组"这个唯一会触发临时 B 树的操作挪出 SQL。

        仍然**不**把事件搬到前端算 —— 那正是旧的 `iter_events(limit=20100)` 路径在干的事。

        ## `cancel_event`：让这段折算**可以被叫停**（2026-10-09）

        实测（`tools/probe_density_cost.py`）：单发 87 ms 里有 **54 ms 是这段 Python 折算**，
        而它持 GIL ⇒ 并发请求超线性变慢（1→67ms、4→912ms、16→6.0s）。
        前端的 `AbortController` 只断开 HTTP 连接，**停不掉**工作线程里的这活，
        于是"用户超时后又点一下"会不断往后台堆查询（自增强雪崩）。

        调用方传一个 `threading.Event` 进来（`server/app.py` 会在客户端断开时置位），
        这里每读一批就查一次：置位了就**立刻收手**、返回已经算好的部分
        并带 `cancelled: True`。真正的取消语义只能是"协作式"的 ——
        线程没法被外部强杀，所以每分钟都要有一次"你还要吗"的机会。

        ## `with_types`：**每个桶的类型明细默认不算**（2026-10-09）

        实测（`tools/probe_density_fold.py`，真库 187.7 小时 / 31,190 行）：
        每个桶都维护一个 `{type: count}` 嵌套 dict 要 **多花约 9 ms（−19%）**，
        而**前端一处都没消费它**（`TypeBars` 用的是 `/api/stats.by_type`；
        曲线与柱状都只用 `count`）。所以默认只算**扁平计数 + 顶层 by_type**，
        桶里的 `by_type` 一律给 `null`（**键还在**，形状不变）；
        真需要桶级明细的调用方显式传 `with_types=True`（老行为，供测试与将来用）。

        同时把取数改成**位置取值**（绕开 `sqlite3.Row` 的构造）——
        这两项合起来把折算从 ~48 ms 压到 ~34 ms（−27%），而这段正是并发时
        抢 GIL 的部分（见 `tools/probe_density_cost.py` 的并发扫描）。
        """
        bucket_s = max(float(bucket_s), 0.001)
        clauses: list[str] = []
        params: list[Any] = []

        base_where, base_params = self._build_where(**filters)
        if base_where:
            clauses.append(base_where.removeprefix(" WHERE "))
            params.extend(base_params)
        if since is not None:
            clauses.append("recv_ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("recv_ts <= ?")
            params.append(float(until))
        types = [item for item in (include_types or []) if item]
        if types:
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        buckets: dict[int, dict[str, Any]] = {}
        by_type_totals: dict[str, int] = {}

        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        # 还没开始就被叫停（客户端早走了）：直接回空，别再扫库
        if cancelled():
            return self._density_payload(buckets, by_type_totals, bucket_s, True)

        # 流式处理：`fetchmany` 而不是 `fetchall()` —— 20 万行的聚合结果仍有 2.5 万行，
        # 一次全取出没有意义；游标能配合索引顺序边读边折。
        cursor = self._conn.execute(
            f"SELECT recv_ts, type, COUNT(*) AS n FROM event{where} GROUP BY recv_ts, type",
            params,
        )
        while True:
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            for row in rows:
                # 位置取值（`row[0]`）比 `row["recv_ts"]` 快：`sqlite3.Row` 的按名取值
                # 要走一次 mapping 查找，2~3 万行就是几毫秒（实测见 docstring）
                key = int(float(row[0]) / bucket_s)
                count = row[2]
                type_name = row[1]
                if with_types:
                    entry = buckets.setdefault(key, {"ts": key * bucket_s, "count": 0, "by_type": {}})
                    entry["count"] += count
                    entry["by_type"][type_name] = entry["by_type"].get(type_name, 0) + count
                else:
                    entry = buckets.get(key)
                    if entry is None:
                        buckets[key] = {"ts": key * bucket_s, "count": count, "by_type": None}
                    else:
                        entry["count"] += count
                by_type_totals[type_name] = by_type_totals.get(type_name, 0) + count

        # `range` 必须服从**同一套时间边界**，否则会出现
        # "summary 说跨了 264 小时、但一根柱子都没画"这种自相矛盾的界面
        # （实测：只传 since 时，桶被过滤掉了而 range 还是全库范围）。
        # 所以这里把 since/until 一起交给 `time_range` —— 不能只算一次全量再裁剪，
        # 因为"窗口起点"要的是**该范围内第一条数据的时间**，不是 max(since, lo)。
        lo, hi = self.time_range(
            **{**filters, "since": since, "until": until}
        )
        return self._density_payload(buckets, by_type_totals, bucket_s, cancelled(), lo=lo, hi=hi)

    @staticmethod
    def _density_payload(
        buckets: dict[int, dict[str, Any]],
        by_type_totals: dict[str, int],
        bucket_s: float,
        is_cancelled: bool,
        *,
        lo: float | None = None,
        hi: float | None = None,
    ) -> dict[str, Any]:
        """把折算结果拼成响应（含 `cancelled` 标记）。

        `cancelled: True` 表示"这次没算完就被叫停了"（客户端已经走了）——
        调用方**不该**把它当成一份可信的密度数据用（例如别写进缓存）。
        **只在被叫停时才加这个键**：正常响应的形状与从前**逐字一致**，
        不给前端/契约增加无谓的字段。
        """
        payload: dict[str, Any] = {
            "bucket_s": bucket_s,
            "buckets": [buckets[key] for key in sorted(buckets)],
            "total": sum(entry["count"] for entry in buckets.values()),
            "by_type": dict(sorted(by_type_totals.items(), key=lambda kv: -kv[1])),
            "range": {"lo": lo, "hi": hi},
        }
        if is_cancelled:
            payload["cancelled"] = True
        return payload

    @staticmethod
    def estimate_bucket_s(lo: float | None, hi: float | None, target: int = 240) -> float:
        """按区间长度挑一个"桶数接近 target"的步长（时间轴自适应）。

        步长取自一组"人类可读"的档位，避免出现 37 秒一个桶这种没人看得懂的刻度。
        """
        steps = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400]
        if lo is None or hi is None or hi <= lo:
            return float(steps[2])
        span = hi - lo
        ideal = span / max(target, 1)
        for step in steps:
            if step >= ideal:
                return float(step)
        return float(steps[-1])

    @staticmethod
    def _to_event(row: sqlite3.Row, *, with_raw: bool = True) -> Event:
        from .models import Medal

        medal = None
        if row["medal_name"] or row["medal_level"]:
            medal = Medal(name=row["medal_name"], level=row["medal_level"])

        raw = None
        # `with_raw=False` 时**不带原始帧**（见 `iter_events` 的说明）；
        # 需要取证就 `raw=1`。
        #
        # 这里**不再 `json.loads`**：`Event.raw` 存的就是 JSON 文本，
        # 而库里 `raw_json` 列存的也是同一份文本，原样传下去即可。
        # 于是"读回来"这条路径既省一次解析、也省下那棵 ~3 KB 的对象树
        # （原先解析成 dict 只为了导出时再 `json.dumps` 一次，是纯粹的往返浪费）。
        if with_raw and row["raw_json"]:
            raw = row["raw_json"]

        extra: dict[str, Any] = {}
        # 旧库可能没有 extra_json 列（schema v1），此时 sqlite3.Row 会抛 IndexError
        try:
            payload = row["extra_json"]
        except IndexError:  # pragma: no cover - 仅旧库路径
            payload = None
        if payload:
            try:
                loaded = json.loads(payload)
            except ValueError:
                loaded = None
            if isinstance(loaded, dict):
                extra = loaded

        return Event(
            seq=int(row["id"]),
            session_id=row["session_id"],
            room_id=int(row["room_id"]),
            type=row["type"],
            cmd=row["cmd"],
            source=row["source"],
            recv_ts=float(row["recv_ts"]),
            server_ts=row["server_ts"],
            latency_ms=row["latency_ms"],
            msg_id=row["msg_id"],
            uid=row["uid"],
            uname=row["uname"],
            text=row["text"],
            price=row["price"],
            guard_level=row["guard_level"],
            user_level=row["user_level"],
            medal=medal,
            field_source=row["field_source"],
            extra=extra,
            raw=raw,
        )

    def iter_texts(
        self,
        *,
        limit: int | None = None,
        batch_size: int = 2000,
        **filters: Any,
    ) -> Iterator[tuple[str, str, dict[str, Any]]]:
        """只取 `(type, text, extra)` —— 词云/文本分析用。

        刻意**不**走 `iter_events()`：那条路要构造完整 Event（含 raw JSON 解析），
        对"只想数词"的场景是几十倍的浪费。这里直接用 SQL 抽需要的列。

        旧库（schema v1）没有 `extra_json` 列，此时用 NULL 占位 —— 表情元数据缺失
        不影响分词，只是表情榜里没有图片链接。
        """
        where, params = self._build_where(**filters)
        extra_column = "extra_json" if "extra_json" in self._columns else "NULL AS extra_json"
        sql = f"SELECT type, text, {extra_column} FROM event{where} ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params = [*params, limit]

        cursor = self._conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                extra: dict[str, Any] = {}
                try:
                    payload = row["extra_json"]
                except IndexError:  # pragma: no cover - 仅旧库路径
                    payload = None
                if payload:
                    try:
                        loaded = json.loads(payload)
                    except ValueError:
                        loaded = None
                    if isinstance(loaded, dict):
                        extra = loaded
                yield row["type"], (row["text"] or ""), extra
