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
        self._columns = {row[1] for row in self._conn.execute("PRAGMA table_info(event)")}

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
        batch_size: int = 1000,
        **filters: Any,
    ) -> Iterator[Event]:
        """流式产出事件，避免一次性载入内存（导出大结果集时用）。"""
        where, params = self._build_where(**filters)
        sql = f"SELECT {self._event_columns_sql} FROM event{where} ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params = [*params, limit]

        cursor = self._conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield self._to_event(row)

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
                key = int(float(row["recv_ts"]) / bucket_s)
                entry = buckets.setdefault(key, {"ts": key * bucket_s, "count": 0, "by_type": {}})
                count = int(row["n"])
                type_name = row["type"]
                entry["count"] += count
                entry["by_type"][type_name] = entry["by_type"].get(type_name, 0) + count
                by_type_totals[type_name] = by_type_totals.get(type_name, 0) + count

        # `range` 必须服从**同一套时间边界**，否则会出现
        # "summary 说跨了 264 小时、但一根柱子都没画"这种自相矛盾的界面
        # （实测：只传 since 时，桶被过滤掉了而 range 还是全库范围）。
        # 所以这里把 since/until 一起交给 `time_range` —— 不能只算一次全量再裁剪，
        # 因为"窗口起点"要的是**该范围内第一条数据的时间**，不是 max(since, lo)。
        lo, hi = self.time_range(
            **{**filters, "since": since, "until": until}
        )
        return {
            "bucket_s": bucket_s,
            "buckets": [buckets[key] for key in sorted(buckets)],
            "total": sum(entry["count"] for entry in buckets.values()),
            "by_type": dict(sorted(by_type_totals.items(), key=lambda kv: -kv[1])),
            "range": {"lo": lo, "hi": hi},
        }

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
    def _to_event(row: sqlite3.Row) -> Event:
        from .models import Medal

        medal = None
        if row["medal_name"] or row["medal_level"]:
            medal = Medal(name=row["medal_name"], level=row["medal_level"])

        raw = None
        if row["raw_json"]:
            try:
                raw = json.loads(row["raw_json"])
            except ValueError:
                raw = None

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
