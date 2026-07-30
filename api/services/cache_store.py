"""产品知识缓存库：SQLite（结构化档案）+ 向量（语义召回）混合检索 + 重排。

方案 §4：命中即免搜索免人工 —— demo 里"记忆生效的哇时刻"，
也是看板上"缓存命中率上升 / 人工复核率下降"两条曲线的来源。

## provenance 与 supersede（Day 3 加固 1 & 2）

档案有两种来源：
- `auto`：搜索成功后自动沉淀。**写入有护栏**（见 `should_cache`）：
  重裁决置信度 ≥ DIRECT_THRESHOLD、search_status == "ok"、evidence 非空，三者同时满足才写。
  否则一条低质量档案会被后续所有同产品广告命中，把错误固化下来。
- `human_verified`：人工裁定确认过。

**唯一键 = (lower(brand), lower(product_name))，supersede 走这把键 upsert，不做版本分叉。**
理由：档案是"当前认定的营养事实"，不是审计流水；流水已经在 audits.trace_json 里了，
再维护一套版本链只会让 cache_lookup 面对"该取哪一版"的选择题。
用 `revision` 自增 + `superseded_at` / `superseded_by` 记录被覆盖的事实，可追溯但不分叉。

**单向棘轮**：human_verified 档案不会被 auto 写入覆盖（`upsert` 里显式拒绝）；
只有另一次人工裁定能改写它。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from config import settings
from db import cursor, new_id, now
from graph.state import Classification, Evidence, NutrientValue
from services import vectorstore

logger = logging.getLogger(__name__)

COLLECTION = "products"
Provenance = Literal["auto", "human_verified"]

# 混合检索权重：品牌精确匹配为主，语义召回补位
W_EXACT_BRAND = 0.55
W_NAME_OVERLAP = 0.20
W_SEMANTIC = 0.25

# 缓存档案里存 normalized 值（g/100g 或 g/100ml），键名即 Nutrient 字面量
NUTRIENT_FIELDS = ("sugar", "fat", "fiber", "sodium", "protein")


def _doc(brand: str, product_name: str) -> str:
    return f"{brand} | {product_name}"


def _token_overlap(a: str, b: str) -> float:
    ta, tb = set(a.lower().split()), set(b.lower().split())
    return len(ta & tb) / max(1, len(ta | tb))


# --------------------------------------------------------------------------- #
# 写入护栏
# --------------------------------------------------------------------------- #
def should_cache(
    revised: Classification | None,
    evidence: list[Evidence],
    search_status: str | None,
) -> tuple[bool, str]:
    """auto 档案的写入护栏。返回 (是否写入, 原因)，原因会进 trace。"""
    if revised is None:
        return False, "无重裁决结果"
    if not revised.brand or not revised.product_name:
        return False, "缺少 brand/product_name 唯一键"
    if search_status != "ok":
        # degraded / conflict / cache 一律不沉淀：降级证据没有结构化读数，
        # 冲突证据本身就是矛盾的，缓存命中的不必重复写
        return False, f"search_status={search_status!r} != ok"
    web_evidence = [e for e in evidence if e.provenance == "web" and e.nutrients]
    if not web_evidence:
        return False, "无带结构化读数的联网证据（缓存/降级证据不沉淀）"
    if revised.conflict:
        return False, "证据冲突"
    if revised.specific_code is None:
        return False, "叶子未定（parent 级结果不入库）"
    if revised.specific_confidence < settings.direct_threshold:
        return False, (
            f"置信度 {revised.specific_confidence:.2f} < DIRECT_THRESHOLD "
            f"{settings.direct_threshold}"
        )
    return True, "满足护栏"


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def lookup(brand: str | None, product_name: str | None) -> tuple[dict[str, Any] | None, float]:
    """返回 (缓存档案 or None, 得分)。得分 ≥ settings.cache_hit_threshold 视为命中。"""
    if not brand and not product_name:
        return None, 0.0
    query = _doc(brand or "", product_name or "")
    candidates: dict[str, dict[str, Any]] = {}

    # 1) SQLite 品牌精确匹配（大小写不敏感）
    if brand:
        with cursor() as cur:
            cur.execute(
                "SELECT * FROM product_cache WHERE lower(brand)=lower(?) LIMIT 20", (brand,)
            )
            for row in cur.fetchall():
                candidates[row["id"]] = dict(row)

    # 2) 向量语义召回
    sem: dict[str, float] = {}
    try:
        res = vectorstore.collection(COLLECTION).query(query_texts=[query], n_results=5)
        for cid, dist in zip(res.get("ids", [[]])[0], res.get("distances", [[]])[0]):
            sem[cid] = max(0.0, 1.0 - float(dist))
            if cid not in candidates:
                with cursor() as cur:
                    cur.execute("SELECT * FROM product_cache WHERE id=?", (cid,))
                    row = cur.fetchone()
                    if row:
                        candidates[cid] = dict(row)
    except Exception:  # noqa: BLE001 — 向量库不可用不应阻断主链路
        pass

    # 3) 重排
    best, best_score = None, 0.0
    for cid, rec in candidates.items():
        score = 0.0
        if brand and rec["brand"].lower() == brand.lower():
            score += W_EXACT_BRAND
        if product_name:
            score += W_NAME_OVERLAP * _token_overlap(product_name, rec["product_name"])
        score += W_SEMANTIC * sem.get(cid, 0.0)
        if score > best_score:
            best, best_score = rec, score

    if best and best_score >= settings.cache_hit_threshold:
        _bump_hit(best["id"])
        return _decode(best), best_score
    return (_decode(best) if best else None), best_score


def _decode(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    for k in ("nutrition_json", "verdict_json"):
        raw = out.pop(k, None)
        out[k[:-5]] = json.loads(raw) if raw else None
    out["source_urls"] = (out.get("source_urls") or "").split(",") if out.get("source_urls") else []
    out.setdefault("provenance", "auto")
    return out


def _bump_hit(cache_id: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE product_cache SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
            (now(), cache_id),
        )


def to_evidence(record: dict[str, Any]) -> list[Evidence]:
    """缓存档案也是 evidence —— adjudicate 节点因此能被两条路径复用。

    `provenance` 一并带进 Evidence：人工核过的档案在 UI 与 trace 上要和自动沉淀的区分开。
    """
    nut = record.get("nutrition") or {}
    prov = record.get("provenance", "auto")
    conf = 0.95 if prov == "human_verified" else 0.85
    nutrients = [
        NutrientValue(
            nutrient=k,
            value=v,
            unit="g/100g",           # 档案里存的已是 normalized 值
            normalized=v,
            confidence=conf,
        )
        for k, v in nut.items()
        if k in NUTRIENT_FIELDS and isinstance(v, (int, float))
    ]
    return [
        Evidence(
            id="ev_cache",
            product_query=f"{record['brand']} {record['product_name']}",
            source_url=(record.get("source_urls") or [""])[0] or "",
            source_title=(
                f"{record['brand']} {record['product_name']}"
                f"（缓存档案 · {'人工核验' if prov == 'human_verified' else '自动沉淀'}）"
            ),
            source_type="cache",
            snippet=json.dumps(nut, ensure_ascii=False)[:300],
            nutrients=nutrients,
            provenance="cache",
            cache_provenance=prov,
            extracted_by="cache",
        )
    ]


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def upsert(
    brand: str,
    product_name: str,
    evidence: list[Evidence],
    verdict: Classification | None,
    *,
    provenance: Provenance = "auto",
    audit_id: str | None = None,
) -> dict[str, Any]:
    """按 (brand, product_name) 唯一键 upsert。

    返回 `{"action": created|updated|superseded|refused, "id": ..., "reason": ...}`，
    调用方把它写进 trace —— 缓存有没有真的写进去，必须在轨迹里看得见。
    """
    nutrition: dict[str, float] = {}
    urls: list[str] = []
    for ev in evidence:
        for f in NUTRIENT_FIELDS:
            v = ev.get(f)
            if v is not None:
                nutrition.setdefault(f, v)
        if ev.source_url:
            urls.append(ev.source_url)

    with cursor() as cur:
        cur.execute(
            "SELECT * FROM product_cache WHERE lower(brand)=lower(?) AND lower(product_name)=lower(?)",
            (brand, product_name),
        )
        row = cur.fetchone()

        if row is None:
            cache_id = new_id()
            cur.execute(
                "INSERT INTO product_cache"
                " (id,brand,product_name,nutrition_json,verdict_json,source_urls,hit_count,"
                "  provenance,revision,created_at,last_hit_at)"
                " VALUES (?,?,?,?,?,?,0,?,1,?,?)",
                (
                    cache_id,
                    brand,
                    product_name,
                    json.dumps(nutrition),
                    verdict.model_dump_json() if verdict else None,
                    ",".join(urls),
                    provenance,
                    now(),
                    now(),
                ),
            )
            action, reason = "created", f"新建 {provenance} 档案"
        else:
            cache_id = row["id"]
            existing = row["provenance"] if "provenance" in row.keys() else "auto"
            # 单向棘轮：auto 不能覆盖 human_verified
            if existing == "human_verified" and provenance == "auto":
                logger.info("拒绝用 auto 结果覆盖人工核验档案 %s", cache_id)
                return {
                    "action": "refused",
                    "id": cache_id,
                    "reason": "已有 human_verified 档案，auto 不覆盖",
                }
            superseded = existing == "auto" and provenance == "human_verified"
            cur.execute(
                "UPDATE product_cache SET nutrition_json=?, verdict_json=?, source_urls=?,"
                " provenance=?, revision=revision+1, superseded_at=?, superseded_by=? WHERE id=?",
                (
                    json.dumps(nutrition),
                    verdict.model_dump_json() if verdict else None,
                    ",".join(urls) or row["source_urls"],
                    provenance,
                    now() if superseded else row["superseded_at"],
                    audit_id if superseded else row["superseded_by"],
                    cache_id,
                ),
            )
            action = "superseded" if superseded else "updated"
            reason = (
                "人工裁定覆盖 auto 档案" if superseded else f"更新 {provenance} 档案"
            )

    try:
        vectorstore.collection(COLLECTION).upsert(
            ids=[cache_id],
            documents=[_doc(brand, product_name)],
            metadatas=[
                {"brand": brand, "product_name": product_name, "provenance": provenance}
            ],
        )
    except Exception:  # noqa: BLE001
        pass
    return {"action": action, "id": cache_id, "reason": reason}


def supersede_with_human_verdict(
    brand: str,
    product_name: str,
    evidence: list[Evidence],
    verdict: Classification,
    audit_id: str | None = None,
) -> dict[str, Any]:
    """人工裁定后覆盖同产品的 auto 档案（feedback_ingest 调用）。"""
    return upsert(
        brand,
        product_name,
        evidence,
        verdict,
        provenance="human_verified",
        audit_id=audit_id,
    )


def stats() -> dict[str, Any]:
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(hit_count),0) hits,"
            " COALESCE(SUM(provenance='human_verified'),0) verified,"
            " COALESCE(SUM(superseded_at IS NOT NULL),0) superseded FROM product_cache"
        )
        row = cur.fetchone()
    return {
        "products": row["n"],
        "total_hits": row["hits"],
        "human_verified": row["verified"],
        "superseded": row["superseded"],
        "backend": vectorstore.backend(),
    }
