"""数据库连接、schema 与事务助手。

设计要点：
- 单连接 + 可重入锁，所有多语句操作包在 BEGIN IMMEDIATE 事务里，
  保证 sales 与 cube_cells 永远同事务提交（增量维护的原子性）。
- WAL 模式：进程被 kill -9 后，已提交事务不丢失，下次打开自动恢复。
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  region        TEXT,                 -- 允许 NULL（真实空值，不是占位符）
  channel       TEXT,
  category      TEXT,
  amount_cents  INTEGER,              -- 允许 NULL；整数分，保证合计精确
  revoked       INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cube_versions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  masks         TEXT NOT NULL,        -- JSON 数组：本版本维护的分组集位掩码
  status        TEXT NOT NULL,        -- building | ready | active | superseded
  created_at    TEXT NOT NULL,
  activated_at  TEXT
);

-- 立方体格子。被汇总掉的维度存 NULL，由 mask（分组标记）区分
-- “真实空值”(分组标记=0, 值=NULL) 与 “该维度已汇总/全部”(分组标记=1)。
CREATE TABLE IF NOT EXISTS cube_cells (
  version_id    INTEGER NOT NULL,
  mask          INTEGER NOT NULL,     -- 分组标记位掩码：bit 置位=该维度参与分组（明细）
  region        TEXT,
  channel       TEXT,
  category      TEXT,
  cell_key      TEXT NOT NULL,        -- JSON [mask, region, channel, category]，NULL 与字符串无歧义
  sale_count    INTEGER NOT NULL,     -- 销售笔数 COUNT(*)
  amount_count  INTEGER NOT NULL,     -- 非空金额笔数 COUNT(amount)
  sum_cents     INTEGER,              -- 精确合计 SUM(amount)；amount_count=0 时为 NULL（SQL 语义）
  PRIMARY KEY (version_id, cell_key)
);
CREATE INDEX IF NOT EXISTS idx_cells_version_mask ON cube_cells(version_id, mask);

-- 构建断点：last_sale_id 与每批格子的写入在同一事务提交，崩溃后精确续建。
CREATE TABLE IF NOT EXISTS build_jobs (
  version_id    INTEGER PRIMARY KEY,
  last_sale_id  INTEGER NOT NULL DEFAULT 0,
  total_sales   INTEGER NOT NULL DEFAULT 0,
  done          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """写事务：BEGIN IMMEDIATE，异常回滚。读快照一致性也用同一事务。"""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self):
        with self.lock:
            self.conn.close()
