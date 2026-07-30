"""SQLite 连接 + 建表（方案 §2：4 张表 + 1 个向量库）。

注意：LangGraph 的 checkpointer 也写同一个 .db 文件（另建 checkpoints 表），
所以 interrupt 挂起的图实例和业务表天然在同一份数据里，重启不丢人工队列。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 审计主表：一张广告一行，status 驱动队列与看板
CREATE TABLE IF NOT EXISTS audits (
  id            TEXT PRIMARY KEY,          -- uuid，同时是 LangGraph thread_id
  batch_id      TEXT REFERENCES batches(id),
  image_path    TEXT NOT NULL,
  status        TEXT NOT NULL,             -- running|direct|direct_verified|pending_human|done|failed
  initial_json  TEXT,
  revised_json  TEXT,
  final_json    TEXT,
  route_1       TEXT,
  route_2       TEXT,
  human_choice  TEXT,
  trace_json    TEXT,                      -- list[StepTrace]，含搜索成本与兜底原因
  created_at    TEXT,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audits_status ON audits(status);
CREATE INDEX IF NOT EXISTS idx_audits_batch  ON audits(batch_id);

-- 批次表：一批广告 → 一份报告
CREATE TABLE IF NOT EXISTS batches (
  id         TEXT PRIMARY KEY,
  name       TEXT,
  status     TEXT,                         -- processing|review_pending|report_ready
  stats_json TEXT,                         -- 聚合统计（报告数字的唯一来源）
  report_md  TEXT,
  created_at TEXT
);

-- 产品知识缓存库元数据（向量在 Chroma，这里存结构化档案）
CREATE TABLE IF NOT EXISTS product_cache (
  id             TEXT PRIMARY KEY,
  brand          TEXT NOT NULL,
  product_name   TEXT NOT NULL,
  nutrition_json TEXT,
  verdict_json   TEXT,
  source_urls    TEXT,
  hit_count      INTEGER DEFAULT 0,
  -- auto = 搜索自动沉淀；human_verified = 人工裁定确认过（单向棘轮，auto 不能覆盖它）
  provenance     TEXT NOT NULL DEFAULT 'auto',
  revision       INTEGER NOT NULL DEFAULT 1,
  superseded_at  TEXT,
  superseded_by  TEXT,          -- 触发 supersede 的 audit_id
  created_at     TEXT,
  last_hit_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_brand ON product_cache(brand);
-- 唯一键：brand + product_name（大小写不敏感）。supersede 走这把键 upsert，不做版本分叉。
CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_key
  ON product_cache(lower(brand), lower(product_name));

-- eval 评测集（人工标注 + 人工修正回流都进这里）
CREATE TABLE IF NOT EXISTS eval_samples (
  id                TEXT PRIMARY KEY,
  image_path        TEXT NOT NULL,
  gold_general      TEXT,
  gold_specific     TEXT,
  source            TEXT,                  -- manual_label | human_feedback
  is_confusing_pair INTEGER DEFAULT 0,
  created_at        TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


# 建表后补加的列：老库直接 ALTER，不用重建（骨架期数据可丢，但流程要跑通）
MIGRATIONS: list[tuple[str, str, str]] = [
    ("product_cache", "provenance", "TEXT NOT NULL DEFAULT 'auto'"),
    ("product_cache", "revision", "INTEGER NOT NULL DEFAULT 1"),
    ("product_cache", "superseded_at", "TEXT"),
    ("product_cache", "superseded_by", "TEXT"),
]


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if k.endswith("_json") and isinstance(v, str) and v:
            try:
                d[k[:-5]] = json.loads(v)
            except json.JSONDecodeError:
                d[k[:-5]] = None
            d.pop(k)
        elif k.endswith("_json"):
            d[k[:-5]] = None
            d.pop(k)
    return d


# --------------------------------------------------------------------------- #
# audits
# --------------------------------------------------------------------------- #
def create_audit(image_path: str, batch_id: str | None = None) -> str:
    audit_id = new_id()
    ts = now()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO audits (id,batch_id,image_path,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (audit_id, batch_id, image_path, "queued", ts, ts),
        )
    return audit_id


def update_audit(audit_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with cursor() as cur:
        cur.execute(f"UPDATE audits SET {cols} WHERE id=?", (*fields.values(), audit_id))


def get_audit(audit_id: str) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM audits WHERE id=?", (audit_id,))
        return row_to_dict(cur.fetchone())


def list_audits(
    *, status: str | None = None, batch_id: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM audits WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if batch_id:
        sql += " AND batch_id=?"
        args.append(batch_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with cursor() as cur:
        cur.execute(sql, args)
        return [row_to_dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# batches
# --------------------------------------------------------------------------- #
def create_batch(name: str | None) -> str:
    batch_id = new_id()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO batches (id,name,status,created_at) VALUES (?,?,?,?)",
            (batch_id, name or f"batch-{batch_id[:6]}", "processing", now()),
        )
    return batch_id


def update_batch(batch_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with cursor() as cur:
        cur.execute(f"UPDATE batches SET {cols} WHERE id=?", (*fields.values(), batch_id))


def get_batch(batch_id: str) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM batches WHERE id=?", (batch_id,))
        return row_to_dict(cur.fetchone())


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,))
        return [row_to_dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# eval_samples
# --------------------------------------------------------------------------- #
def add_eval_sample(
    image_path: str,
    gold_general: str | None,
    gold_specific: str | None,
    source: str,
    is_confusing_pair: bool = False,
) -> str:
    sid = new_id()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO eval_samples"
            " (id,image_path,gold_general,gold_specific,source,is_confusing_pair,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, image_path, gold_general, gold_specific, source, int(is_confusing_pair), now()),
        )
    return sid
