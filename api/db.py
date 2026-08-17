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
  -- 回流来源的 audit。**幂等键**：同一次审计重复回流只应留一行。
  -- 人工导入的金标没有 audit_id，所以是**部分**唯一索引（WHERE 非空）。
  audit_id          TEXT,
  created_at        TEXT
);

-- 缓存命中观测台（Day7 · OPEN-RISK-01 的观察指标）
--
-- 为什么单独一张表，而不是每次从 trace_json 里刨：
-- ① 改判率要跨批次看趋势，逐条解析 JSON 既慢、又会随 trace 结构变动静默失效；
-- ② 这张表是"缓存到底可不可信"的**证据**，它得能被直接 SELECT 出来给人看；
-- ③ 一次审计最多一次缓存命中，audit_id 作主键天然幂等 —— resume 会重跑 _persist，
--    靠主键 upsert 保证重复调用不会把一次命中记成两次。
--
-- overturned 三态：1=人工改判 / 0=人工确认 / NULL=还没走到人工。
-- **NULL 不等于 0**：把"没人看过"算成"人工确认了"会让改判率虚低，
-- 而这个指标存在的意义恰恰是发现缓存在悄悄喂错答案。
CREATE TABLE IF NOT EXISTS cache_hit_log (
  audit_id     TEXT PRIMARY KEY,
  cache_id     TEXT,
  score        REAL,
  provenance   TEXT,                       -- auto | human_verified
  match_mode   TEXT,                       -- legacy | strict（留作两态对比）
  -- chroma | difflib。命中率在两种 backend 下**不可比**（语义分占 0.25，
  -- 而它是命中的必要条件），所以每行都要能答出自己是在哪个 backend 上产生的。
  cache_backend TEXT,
  route_1      TEXT,
  route_2      TEXT,
  human_choice TEXT,
  cached_code  INTEGER,                    -- 基于缓存证据得出的叶子（revised）
  final_code   INTEGER,
  overturned   INTEGER,                    -- 1 / 0 / NULL，见上
  created_at   TEXT,
  updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_hit_cache ON cache_hit_log(cache_id);
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
# 建在**新增列**上的索引必须等 ALTER 跑完再建。
#
# 踩过的坑：把 `idx_eval_audit` 直接写进 SCHEMA 里，新库没问题（CREATE TABLE
# 已含该列），**老库直接炸** —— `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，
# 列还没加上，索引就先建了：`no such column: audit_id`。
# 而测试用的永远是新库，所以这个 bug 只会在真实机器上出现。
POST_MIGRATION_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_audit
  ON eval_samples(audit_id) WHERE audit_id IS NOT NULL;
"""

MIGRATIONS: list[tuple[str, str, str]] = [
    ("cache_hit_log", "cache_backend", "TEXT"),
    ("eval_samples", "audit_id", "TEXT"),
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
        conn.executescript(POST_MIGRATION_SCHEMA)
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
    audit_id: str | None = None,
) -> str:
    """写一条评测样本。给了 `audit_id` 就按它幂等 —— 同一次审计重复回流只留一行。

    幂等键选 audit_id 而不是 image_path：同一张图可以被审计多次
    （比如换了模型重跑），那是两条独立的人工裁定，不该互相覆盖。
    """
    with cursor() as cur:
        if audit_id:
            cur.execute("SELECT id FROM eval_samples WHERE audit_id=?", (audit_id,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE eval_samples SET image_path=?, gold_general=?, gold_specific=?,"
                    " source=?, is_confusing_pair=? WHERE audit_id=?",
                    (image_path, gold_general, gold_specific, source,
                     int(is_confusing_pair), audit_id),
                )
                return row["id"]
        sid = new_id()
        cur.execute(
            "INSERT INTO eval_samples"
            " (id,image_path,gold_general,gold_specific,source,is_confusing_pair,"
            "  audit_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, image_path, gold_general, gold_specific, source,
             int(is_confusing_pair), audit_id, now()),
        )
    return sid


# --------------------------------------------------------------------------- #
# cache_hit_log（Day7 · OPEN-RISK-01 观察指标）
# --------------------------------------------------------------------------- #
def log_cache_hit(
    audit_id: str,
    cache_id: str | None,
    score: float,
    provenance: str | None,
    match_mode: str,
    cache_backend: str | None = None,
) -> None:
    """命中当下就落一行。路由与人工结果稍后由 `finalize_cache_hit` 补。

    用 upsert 而不是 insert：resume 会让整条链路重跑一遍，
    insert 会撞主键或把一次命中记成两次。
    """
    with cursor() as cur:
        cur.execute(
            "INSERT INTO cache_hit_log"
            " (audit_id,cache_id,score,provenance,match_mode,cache_backend,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(audit_id) DO UPDATE SET"
            "  cache_id=excluded.cache_id, score=excluded.score,"
            "  provenance=excluded.provenance, match_mode=excluded.match_mode,"
            "  cache_backend=excluded.cache_backend, updated_at=excluded.updated_at",
            (audit_id, cache_id, score, provenance, match_mode, cache_backend, now(), now()),
        )


def finalize_cache_hit(
    audit_id: str,
    *,
    route_1: str | None,
    route_2: str | None,
    human_choice: str | None,
    cached_code: int | None,
    final_code: int | None,
) -> None:
    """补齐命中之后发生了什么。没有对应命中行就什么都不做（未命中的审计不该出现在表里）。

    `overturned` 只在人工真的裁定过之后才有值：
    人工裁定了且 final 与缓存给出的叶子不一致 → 1；一致 → 0；没走到人工 → 保持 NULL。
    """
    overturned = None
    if human_choice:
        overturned = int(final_code != cached_code)
    with cursor() as cur:
        cur.execute(
            "UPDATE cache_hit_log SET route_1=?, route_2=?, human_choice=?,"
            " cached_code=?, final_code=?, overturned=?, updated_at=? WHERE audit_id=?",
            (route_1, route_2, human_choice, cached_code, final_code,
             overturned, now(), audit_id),
        )


def cache_hit_rows(audit_ids: list[str] | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM cache_hit_log"
    args: list = []
    if audit_ids is not None:
        if not audit_ids:
            return []
        sql += f" WHERE audit_id IN ({','.join('?' * len(audit_ids))})"
        args = list(audit_ids)
    with cursor() as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]
