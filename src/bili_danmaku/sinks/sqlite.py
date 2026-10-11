"""SQLite 落盘。

设计要点：

* WAL 模式 + `synchronous=NORMAL`，兼顾写入性能与可恢复性；
* 一批事件 = 一个事务，结束时 `wal_checkpoint(TRUNCATE)` 收缩 WAL；
* `dedupe_key` 上建 `UNIQUE`，作为内存去重之外的第二道防线；
* **不使用 FTS5**：其 `unicode61` 分词器会把连续中文当成单个 token（搜「主播」
  匹配不到「主播好」），而 `trigram` 分词器又要求查询 ≥3 字符 —— 中文弹幕关键词
  通常只有 2 个字。关键词检索统一走 `LIKE` 子串匹配（见 `store.py`）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path

from ..config import Config
from ..models import Event, SessionMeta
from .base import Sink

log = logging.getLogger("bili_danmaku.sinks.sqlite")

#: 结构期望版本。**只表示"期望变了"，不驱动迁移**（迁移见 `MIGRATIONS`）；
#: 新增索引靠 `SCHEMA` 里的 `CREATE INDEX IF NOT EXISTS` 自愈，不必动这里。
SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS session (
    id           TEXT PRIMARY KEY,
    room_id      INTEGER NOT NULL,
    short_id     INTEGER,
    anchor_uid   INTEGER,
    token_source TEXT,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL,
    room_id      INTEGER NOT NULL,
    recv_ts      REAL    NOT NULL,
    server_ts    INTEGER,
    latency_ms   INTEGER,
    type         TEXT    NOT NULL,
    cmd          TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'live',
    msg_id       TEXT,
    uid          INTEGER,
    uname        TEXT,
    text         TEXT,
    price        INTEGER,
    guard_level  INTEGER,
    user_level   INTEGER,
    medal_name   TEXT,
    medal_level  INTEGER,
    field_source TEXT,
    dedupe_key   TEXT UNIQUE,
    raw_json     TEXT,
    extra_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_room_ts ON event(room_id, recv_ts);
CREATE INDEX IF NOT EXISTS idx_event_type_ts ON event(type, recv_ts);
CREATE INDEX IF NOT EXISTS idx_event_uid     ON event(uid);
CREATE INDEX IF NOT EXISTS idx_event_msgid   ON event(msg_id);
CREATE INDEX IF NOT EXISTS idx_event_session ON event(session_id);
-- V3-a：时间轴密度聚合用。把 type 也放进索引，聚合才能全程走覆盖索引。
-- 实测 20 万行：`GROUP BY recv_ts, type` 从 127 ms 降到 38 ms。只用
-- `(room_id, recv_ts)` 时索引覆盖不到 type，SQLite 会退化成临时 B 树。
CREATE INDEX IF NOT EXISTS idx_event_room_ts_type ON event(room_id, recv_ts, type);
-- V3-b 后续：全库发言榜（`/api/stats` 的 top_senders）用。
-- 语句是 `SELECT uid, uname, COUNT(*) FROM event WHERE uid IS NOT NULL
-- GROUP BY uid ORDER BY 2 DESC` —— 只建 `(uid)` 时 SQLite 每行都要**回表**取 uname，
-- 实测全库 **27.6 ms**；改成 `(uid, uname)` 覆盖索引后 **1.5 ms**（↓18 倍）。
--
-- 为什么用**新索引名**而不是把 idx_event_uid 改成两列：`CREATE INDEX IF NOT EXISTS`
-- 对已存在的同名索引不会重建 ⇒ 老库永远拿不到升级。新名字才能靠
-- "每次开库都跑一遍 SCHEMA"这条自愈机制补上（与 V3-a 的 idx_event_room_ts_type 同理）。
--
-- `idx_event_uid` 保留：它与本索引前缀重复，但删它要走 ALTER/DROP 迁移，
-- 收益只是几十 KB 空间，不值得在"自愈路径"之外多开一条迁移。
CREATE INDEX IF NOT EXISTS idx_event_uid_uname ON event(uid, uname);

-- 2026-10-11：`room_anchor_names()`（`/api/room-names`，房间下拉列表的"曾采集"昵称）用。
--
-- 它的语句是 `SELECT room_id, uname, COUNT(*) AS n FROM event
-- WHERE room_id IN (…) AND uname IS NOT NULL AND uname != ''
-- GROUP BY room_id, uname ORDER BY n DESC, uname ASC`
-- —— 与上面 `idx_event_uid_uname` 是**同一种病**：分组键里有 `uname`，
-- 而当时所有索引都不含它，于是每一行都要**回表**取 uname 再做分组。
--
-- 实测（库 1.6 GB / 383,248 行 / schema v3，
-- 临时副本上跑所以是**冷页缓存**口径，只看相对倍数）：
--
--   | 写法 | 耗时 | 返回值与改前一致 |
--   |---|---|---|
--   | 改前（回表 + GROUP BY 临时树） | **8061 ms** | — |
--   | **本索引（覆盖索引，免回表）** | **68 ms** | ✅ 逐字一致 |
--   | 窗口函数 `ROW_NUMBER()` | 109 ms | ✅ 但 SQL 复杂得多，不值得 |
--   | `MAX(uname)` 改写 | 112 ms | ❌ 少一个房间（语义错） |
--   | 按 `(room_id, uname)` 序流式取第一组 | 97 ms | ❌ 并列时取值变了 |
--   | Python 侧流式计数 | 370 ms | ✅ 不用 SQL 聚合反而更慢 |
--   | 限定"最近 N 条"再聚合 | 40~389 ms | ❌ 换了统计口径 |
--
-- 生产热态口径：改前稳定 **1.2 s**，且它是**用户点开
-- 房间下拉框**这条交互路径上的调用 ⇒ 值得消掉。**注意本轮只加索引、不改 SQL**：
-- 上面四种"看起来更快"的改写都有语义代价，实测数据把它们全部否掉了。
--
-- 为什么用新索引名：`CREATE INDEX IF NOT EXISTS` 对已存在的**同名**索引不会重建
-- ⇒ 老库永远拿不到升级；新名字才能靠"每次 `open()` 都跑一遍 SCHEMA"这条自愈机制补上
-- （与 `idx_event_room_ts_type`、`idx_event_uid_uname` 同理）。
--
-- 那个**一次性代价**（实测）：
--   * 索引本身占 **9.9 MB**（page_count 差值法）；
--   * 老库首次 `open()` 会在 `executescript(SCHEMA)` 里真的建一次索引，实测
--     **2.3 s（空闲、页缓存热）~ 15.8 s（并发压测、页缓存冷）** —— 这台
--     库 1.6 GB / 383,248 行，**随库大小缩放**；新建库/小库/已带索引的库都是 **8 ms**；
--   * 它发生在 **`sink.open()`**（= 添加房间/开始采集那一刻），**不是**面板启动路径
--     —— 启动时还没有采集器，所以两种库的面板 `/api/health` 就绪都是 **1.56 s**。
--   ⚠️ 这一项**只影响老库的第一次采样**，不会随会话数累积（之后每次开库都是 8 ms）。
CREATE INDEX IF NOT EXISTS idx_event_room_uname ON event(room_id, uname);

-- P0-3：消除"把整个结果集物化到临时 B 树"的两类查询。
--
-- 此前所有索引的最左列都是 room_id / type / uid，于是**只按时间窗口**过滤的查询
-- （面板默认"全部房间"、历史翻页不带 room_id、时间轴密度）只能全表扫 + 临时 B 树排序。
-- 用 `EXPLAIN QUERY PLAN` 实测（2 万行）：
--
--   | 查询 | 加索引前 | 加索引后 |
--   |---|---|---|
--   | `… WHERE recv_ts >= ? ORDER BY recv_ts DESC, id DESC` | 全扫 + `USE TEMP B-TREE FOR ORDER BY` | `SEARCH … USING COVERING INDEX idx_event_ts (recv_ts>?)`（**无临时树**）|
--
-- `idx_event_ts` 把 `id` 也放进索引，正是为了同时满足 `ORDER BY recv_ts DESC, id DESC`
-- 这条"历史翻页"的排序（纯 `(recv_ts)` 仍会退化成临时树）。
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(recv_ts, id);

-- `latency_percentiles`（`/api/stats` **每 3 秒跑两次**：P50/P95）的语句是
-- `ORDER BY latency_ms LIMIT 1 OFFSET n`，之前完全没有可用索引
-- ⇒ `SCAN event` + `USE TEMP B-TREE FOR ORDER BY`，临时树要装下**整个结果集**的排序键
-- （20 万行就是几 MB，超出 page cache 还会落盘）。
--
-- ⚠️ 索引形状是实测挑的，**不是** `(recv_ts, latency_ms)`：
-- 实测对比了 6 种形状 × 3 种过滤组合，
-- **只有单列 `(latency_ms)` 在"无过滤 / 带 recv_ts 窗口 / 带 room_id+recv_ts"三种情况下
-- 都不需要临时 B 树**；`(recv_ts, latency_ms)` 只在无过滤时有效，
-- 一带 `recv_ts` 窗口就退化（因为它的第一列是 `recv_ts`，喂不了 `ORDER BY latency_ms`）。
CREATE INDEX IF NOT EXISTS idx_event_latency ON event(latency_ms);
"""

#: 旧库升级：新增列只能靠 ALTER TABLE（`CREATE TABLE IF NOT EXISTS` 对已存在的表无效）。
#: **新增索引不需要写在这里**：`SCHEMA` 里的 `CREATE INDEX IF NOT EXISTS` 每次
#: `open()` 都会执行，老库下次启动时会自动补上索引（V3-a 的
#: `idx_event_room_ts_type` 就是靠这条自愈的）。提升 `SCHEMA_VERSION` 只表示
#: "结构期望变了"，不驱动迁移。
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("extra_json", "ALTER TABLE event ADD COLUMN extra_json TEXT"),
)

INSERT_SQL = """
INSERT OR IGNORE INTO event (
    session_id, room_id, recv_ts, server_ts, latency_ms,
    type, cmd, source, msg_id, uid, uname, text,
    price, guard_level, user_level, medal_name, medal_level,
    field_source, dedupe_key, raw_json, extra_json
) VALUES (?,?,?,?,?, ?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?)
"""


class SqliteSink(Sink):
    name = "sqlite"
    #: `raw_json` 列会把原始帧存下来 → 可以参与"落库后释放 raw"（见 `Sink.persists_raw`）
    persists_raw = True

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._conn: sqlite3.Connection | None = None
        self.ignored = 0

    # ------------------------------------------------------------------ #
    @property
    def db_path(self) -> Path:
        configured = (self.config.sinks.sqlite_path or "").strip()
        if configured:
            return Path(configured)
        return Path(self.out_dir) / "danmaku.db"

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteSink 尚未 open()")
        return self._conn

    # ------------------------------------------------------------------ #
    def open(self, meta: SessionMeta) -> None:
        super().open(meta)
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)

        # `check_same_thread=False` 是必需的：`open()` 由 Bus 在事件循环里调用，
        # 而 `write()` 由 `Bus._flush_off_loop()` 丢到工作线程执行 ——
        # 默认的线程检查会让每次写入都抛
        # `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used
        # in that same thread`（改 to_thread 后立刻在面板的多房间用例里暴露）。
        # 本项目每个 sink 只有**一个** worker 任务在串行使用这条连接
        # （`_run` 里 await 完 flush 才会进下一批），不存在并发访问。
        self._conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()

        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO session
                (id, room_id, short_id, anchor_uid, token_source, started_at, ended_at, note)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                meta.session_id,
                meta.room_id,
                meta.short_id,
                meta.anchor_uid,
                meta.token_source,
                meta.started_at or time.time(),
                None,
                meta.note,
            ),
        )
        self._conn.commit()
        log.info("SQLite 输出: %s", path)

    # ------------------------------------------------------------------ #
    def _migrate(self) -> None:
        """把旧库补齐到当前 schema。

        `CREATE TABLE IF NOT EXISTS` 对**已存在**的表什么都不做，所以给 event 表加列
        必须单独 ALTER。老库（schema_version=1）缺少 `extra_json`，
        不补的话新版写入会直接抛 `no such column`。
        """
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(event)")}
        for column, statement in MIGRATIONS:
            if column in existing:
                continue
            self._conn.execute(statement)
            log.info("SQLite 旧库升级：event 表新增列 %s", column)

    # ------------------------------------------------------------------ #
    def _row(self, event: Event) -> tuple:
        # `Event.raw` 已经是 JSON 文本（见 `models.Event.raw`），直接落列即可，
        # **不再 `json.dumps` 一次** —— 这条路径每条事件都要走，省一次序列化。
        keep_raw = self.config.sinks.keep_raw and event.raw is not None
        return (
            # 以 sink 打开时的会话为准：保证每条事件都指向确实存在的 session 行，
            # 不会因为生产者给的 session_id 不一致而产生孤儿数据
            self.meta.session_id,
            event.room_id,
            event.recv_ts or time.time(),
            event.server_ts,
            event.latency_ms,
            event.type,
            event.cmd,
            event.source,
            event.msg_id,
            event.uid,
            event.uname,
            event.text,
            event.price,
            event.guard_level,
            event.user_level,
            event.medal.name if event.medal else None,
            event.medal.level if event.medal else None,
            event.field_source,
            event.dedupe_key(),
            event.raw if keep_raw else None,
            # 只存"有价值的结构化补充"：目前是表情占位符 → 图片元数据。
            # 不依赖 raw（--no-raw 时也要能用），词云与面板都读它。
            json.dumps(event.extra, ensure_ascii=False) if event.extra else None,
        )

    def write(self, events: Sequence[Event]) -> None:
        connection = self.connection
        inserted = 0
        try:
            for event in events:
                cursor = connection.execute(INSERT_SQL, self._row(event))
                # INSERT OR IGNORE 命中 UNIQUE 冲突时 rowcount 为 0
                if cursor.rowcount:
                    inserted += 1
                else:
                    self.ignored += 1
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise
        self.record(inserted)

    def flush(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "UPDATE session SET ended_at = ? WHERE id = ?",
                (time.time(), self.meta.session_id),
            )
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:  # noqa: BLE001
            log.warning("SQLite 收尾失败: %s", exc)
        finally:
            self._conn.close()
            self._conn = None
