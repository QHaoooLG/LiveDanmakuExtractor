"""标注存储：**独立库**里的标记 / 笔记（ROADMAP §6.2 R2，V3-a2）。

## 为什么必须是独立库，而不是往 `danmaku.db` 里加表

1. **职责单一**：`danmaku.db` 是"采集产物"，由四个 sink 写入；标注是**人**产生的，
   生命周期完全不同（采集库会被归档/清理，标注不该跟着走）。
2. **采集库的 schema 只由 `SqliteSink` 掌管**。把标注表塞进去意味着
   "一个只读的分析层" 和 "一个用户可写的 UI" 共用同一份迁移历史，
   任何一边改动都可能弄坏另一边。
3. **可删**：用户想清空标注，直接删一个文件就行，不必碰采集数据。

## 与 `store.py` 的分工

`store.py` 是**只读**的（`mode=ro`），这个模块是**可写**的 —— 两者刻意不合并。
采集库那边一张表都不动。

## 表结构

与 ROADMAP §6.2 保持一致。`anchor_id` 允许为空：空 = 纯时间点标注（"此刻"），
非空 = 钉在某条弹幕上。`recv_ts` 始终有值，所以**排序与区间导出只需要它**，
`anchor_id` 只是"钉住哪一条"的溯源信息 —— 被钉的弹幕即使后来被清理，
标注本身依然可用（这是刻意设计）。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("bili_danmaku.annotations")

#: 单条标注的字段长度上限（防止 UI 或脚本塞进超长文本）
MAX_LABEL_LEN = 120
MAX_NOTE_LEN = 2000
#: 一次最多返回多少条标注（本地使用足够，且避免误配超大 limit 拖垮前端）
MAX_LIST_LIMIT = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmark (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id    INTEGER NOT NULL,
    session_id TEXT,
    anchor_id  INTEGER,
    recv_ts    REAL    NOT NULL,
    label      TEXT,
    note       TEXT,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookmark_room_ts ON bookmark(room_id, recv_ts);
"""

#: 允许写入的列（防止把任意键写进 SQL）
WRITABLE_COLUMNS = ("room_id", "session_id", "anchor_id", "recv_ts", "label", "note")


class AnnotationError(Exception):
    """标注库不可用或操作失败。"""


class AnnotationStore:
    """标记 / 笔记的可写存储。

    `readonly=True` 时**不创建**库文件，也不建表：面板只是打开一次页面、
    还没打过任何标记的情况下，不该在工作区里凭空留下一个空库。
    """

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._conn: sqlite3.Connection | None = None
        if readonly and not self.path.is_file():
            return
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connect()

    # ------------------------------------------------------------------ #
    def _connect(self) -> None:
        try:
            if self.readonly:
                # `mode=ro` 与采集库同一套路：只读连接不会意外建表
                self._conn = sqlite3.connect(
                    f"file:{self.path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False
                )
            else:
                self._conn = sqlite3.connect(str(self.path), timeout=10.0, check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.executescript(SCHEMA)
                self._conn.commit()
        except sqlite3.Error as exc:
            raise AnnotationError(f"无法打开标注库 {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise AnnotationError("标注库不可用（只读模式下库文件不存在）")
        return self._conn

    @property
    def available(self) -> bool:
        """只读模式下库不存在时返回 False —— 调用方据此降级为空列表。"""
        return self._conn is not None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
            self._conn = None

    def __enter__(self) -> "AnnotationStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def add(
        self,
        *,
        room_id: int,
        recv_ts: float,
        label: str | None = None,
        note: str | None = None,
        session_id: str | None = None,
        anchor_id: int | None = None,
    ) -> dict[str, Any]:
        """新增一条标记，返回落库后的完整记录。

        `label` 缺省为「标记」；超长会被拒绝而不是静默截断 ——
        静默截断会让用户以为存下来的就是他输入的。
        """
        if self.readonly:
            raise AnnotationError("只读模式下不能新增标注")
        text = (label or "").strip() or "标记"
        if len(text) > MAX_LABEL_LEN:
            raise ValueError(f"label 过长（上限 {MAX_LABEL_LEN} 字）")
        if note is not None and len(note) > MAX_NOTE_LEN:
            raise ValueError(f"note 过长（上限 {MAX_NOTE_LEN} 字）")
        if recv_ts is None or float(recv_ts) <= 0:
            raise ValueError("recv_ts 必须是正数时间戳")

        cursor = self.connection.execute(
            "INSERT INTO bookmark (room_id, session_id, anchor_id, recv_ts, label, note, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                int(room_id),
                session_id,
                int(anchor_id) if anchor_id is not None else None,
                float(recv_ts),
                text,
                note,
                time.time(),
            ),
        )
        self.connection.commit()
        return self.get(int(cursor.lastrowid))  # type: ignore[arg-type]

    def get(self, bookmark_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM bookmark WHERE id = ?", (int(bookmark_id),)
        ).fetchone()
        if row is None:
            raise KeyError(bookmark_id)
        return dict(row)

    def delete(self, bookmark_id: int) -> bool:
        if self.readonly:
            raise AnnotationError("只读模式下不能删除标注")
        cursor = self.connection.execute("DELETE FROM bookmark WHERE id = ?", (int(bookmark_id),))
        self.connection.commit()
        return bool(cursor.rowcount)

    def clear(self, *, room_id: int | None = None) -> int:
        """清空标注（可只清一个房间）。返回删除条数。"""
        if self.readonly:
            raise AnnotationError("只读模式下不能清空标注")
        if room_id is None:
            cursor = self.connection.execute("DELETE FROM bookmark")
        else:
            cursor = self.connection.execute("DELETE FROM bookmark WHERE room_id = ?", (int(room_id),))
        self.connection.commit()
        return int(cursor.rowcount)

    # ------------------------------------------------------------------ #
    def list(
        self,
        *,
        room_id: int | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """按时间升序列出标注（前端的时间轴与下拉都按时间序用）。"""
        if not self.available:
            return []
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        clauses: list[str] = []
        params: list[Any] = []
        if room_id is not None:
            clauses.append("room_id = ?")
            params.append(int(room_id))
        if since is not None:
            clauses.append("recv_ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("recv_ts <= ?")
            params.append(float(until))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM bookmark{where} ORDER BY recv_ts, id LIMIT ?", [*params, limit]
        ).fetchall()
        return [dict(row) for row in rows]

    def nearest_before(self, ts: float, *, room_id: int | None = None) -> dict[str, Any] | None:
        """时间上最近的、不晚于 `ts` 的标注。

        导出区间时用来把"起点/终点"从某个标记推到相邻标记：
        用户只选了一个标记，也能一键导出"从这个标记到下一个标记"。
        """
        if not self.available:
            return None
        clauses = ["recv_ts <= ?"]
        params: list[Any] = [float(ts)]
        if room_id is not None:
            clauses.append("room_id = ?")
            params.append(int(room_id))
        row = self.connection.execute(
            f"SELECT * FROM bookmark WHERE {' AND '.join(clauses)}"
            " ORDER BY recv_ts DESC, id DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None

    def bounds(self, *, room_id: int | None = None) -> tuple[float | None, float | None]:
        """标注的时间范围（给"导出全部标注区间"之类用）。"""
        if not self.available:
            return None, None
        params: list[Any] = []
        where = ""
        if room_id is not None:
            where = " WHERE room_id = ?"
            params.append(int(room_id))
        row = self.connection.execute(
            f"SELECT MIN(recv_ts) AS lo, MAX(recv_ts) AS hi FROM bookmark{where}", params
        ).fetchone()
        if row is None or row["lo"] is None:
            return None, None
        return float(row["lo"]), float(row["hi"])


def resolve_annotations_path(config: Any) -> Path:
    """标注库路径：显式配置优先，否则 `{out_dir}/annotations.db`。"""
    configured = (getattr(config.sinks, "annotations_path", "") or "").strip()
    if configured:
        return Path(configured)
    out_dir = getattr(config.sinks, "out_dir", "data") or "data"
    return Path(out_dir) / "annotations.db"


def summarize(bookmarks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """给前端一小段聚合信息（条数 + 时间范围），省掉前端自己算。"""
    if not bookmarks:
        return {"count": 0, "lo": None, "hi": None}
    stamps = [float(item["recv_ts"]) for item in bookmarks]
    return {"count": len(bookmarks), "lo": min(stamps), "hi": max(stamps)}
