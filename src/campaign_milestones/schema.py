"""SQLite 数仓结构：仅追加的原始层。

设计要点：

- ``ingest_batch`` 记录每一次追加批次的记录时间（``ingested_at``），
  这是 as-of 历史查询的时间轴。
- ``raw_*`` 表逐行保存来源记录，并冗余该行的 ``ingested_at``，
  任何 UPDATE/DELETE 都会被触发器拒绝，保证历史时点口径可重放。
- 派生口径（当前有效修订、有效范围、回执状态）不在库内物化，
  由 :mod:`campaign_milestones.queries` 按 as-of 时点即时解析，
  避免派生数据与原始记录不一致。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_RAW_TABLES = (
    "ingest_batch",
    "raw_stage_record",
    "raw_objective_record",
    "raw_scope_record",
    "raw_execution_record",
)

_DDL_TABLES = """
CREATE TABLE IF NOT EXISTS ingest_batch (
    batch_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_label  TEXT NOT NULL,
    ingested_at  TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS raw_stage_record (
    batch_id     INTEGER NOT NULL REFERENCES ingest_batch (batch_id),
    record_id    TEXT NOT NULL UNIQUE,
    stage_id     TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    valid_from   TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    scope_ref    TEXT NOT NULL,
    stage_state  TEXT NOT NULL,
    ingested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_stage ON raw_stage_record (stage_id, revision, ingested_at);

CREATE TABLE IF NOT EXISTS raw_objective_record (
    batch_id        INTEGER NOT NULL REFERENCES ingest_batch (batch_id),
    record_id       TEXT NOT NULL UNIQUE,
    objective_id    TEXT NOT NULL,
    stage_id        TEXT NOT NULL,
    stage_revision  INTEGER NOT NULL,
    measure_code    TEXT NOT NULL,
    expected_count  INTEGER NOT NULL,
    confirmed_at    TEXT,
    objective_state TEXT NOT NULL,
    ingested_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_objective ON raw_objective_record (objective_id, stage_id, ingested_at);

CREATE TABLE IF NOT EXISTS raw_scope_record (
    batch_id        INTEGER NOT NULL REFERENCES ingest_batch (batch_id),
    record_id       TEXT NOT NULL UNIQUE,
    scope_record_id TEXT NOT NULL,
    objective_id    TEXT NOT NULL,
    stage_revision  INTEGER NOT NULL,
    work_item_id    TEXT NOT NULL,
    scope_state     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    ingested_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_scope ON raw_scope_record (objective_id, work_item_id, ingested_at);

CREATE TABLE IF NOT EXISTS raw_execution_record (
    batch_id       INTEGER NOT NULL REFERENCES ingest_batch (batch_id),
    record_id      TEXT NOT NULL UNIQUE,
    execution_id   TEXT NOT NULL,
    objective_id   TEXT NOT NULL,
    work_item_id   TEXT NOT NULL,
    team_id        TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    reported_at    TEXT NOT NULL,
    reported_count INTEGER NOT NULL,
    review_state   TEXT NOT NULL,
    ingested_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_execution_item ON raw_execution_record (objective_id, work_item_id, ingested_at);
CREATE INDEX IF NOT EXISTS idx_raw_execution_exec ON raw_execution_record (execution_id, ingested_at);
"""


def _append_only_ddl() -> str:
    """为原始层各表生成禁止 UPDATE/DELETE 的触发器。"""
    chunks = []
    for table in _RAW_TABLES:
        chunks.append(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update\n"
            f"BEFORE UPDATE ON {table}\n"
            f"BEGIN SELECT RAISE(ABORT, '{table} 仅允许追加，禁止修改历史记录'); END;\n"
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete\n"
            f"BEFORE DELETE ON {table}\n"
            f"BEGIN SELECT RAISE(ABORT, '{table} 仅允许追加，禁止删除历史记录'); END;"
        )
    return "\n".join(chunks)


DDL = _DDL_TABLES + _append_only_ddl()


def init_db(conn: sqlite3.Connection) -> None:
    """在连接上建表与触发器（幂等）。"""
    conn.executescript(DDL)


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数仓并初始化结构。``:memory:`` 可用于临时库。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn
