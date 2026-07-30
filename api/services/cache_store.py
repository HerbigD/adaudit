"""产品知识缓存库：SQLite（结构化档案）+ Chroma（语义召回）混合检索 + 重排。

方案 §4：命中即免搜索免人工 —— 这是 demo 里"记忆生效的哇时刻"，
也是看板上"缓存命中率上升 / 人工复核率下降"两条曲线的来源。
"""

from __future__ import annotations

import json
from typing import Any

from config import settings
from db import cursor, new_id, now
from graph.state import Classification, Evidence
from services import vectorstore

COLLECTION = "products"

# 混合检索权重：品牌精确匹配为主，语义召回补位（方案 §4「混合检索：品牌名精确匹配 + 语义向量召回 + 重排」）
W_EXACT_BRAND = 0.55
W_NAME_OVERLAP = 0.20
W_SEMANTIC = 0.25


def _doc(brand: str, product_name: str) -> str:
    return f"{brand} | {product_name}"


def _token_overlap(a: str, b: str) -> float:
    ta, tb = set(a.lower().split()), set(b.lower().split())
    return len(ta & tb) / max(1, len(ta | tb))


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
        ids = res.get("ids", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for cid, dist in zip(ids, dists):
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
    return out


def _bump_hit(cache_id: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE product_cache SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
            (now(), cache_id),
        )


def to_evidence(record: dict[str, Any]) -> list[Evidence]:
    """缓存档案也是 evidence —— adjudicate 节点因此能被两条路径复用。"""
    nut = record.get("nutrition") or {}
    return [
        Evidence(
            source="cache",
            url=(record.get("source_urls") or [None])[0],
            title=f"{record['brand']} {record['product_name']}（缓存档案）",
            snippet=json.dumps(nut, ensure_ascii=False),
            confidence=0.9,
            **{k: nut.get(k) for k in
               ("sugar_g", "fat_g", "sat_fat_g", "fibre_g", "salt_g", "energy_kj")},
        )
    ]


def upsert(
    brand: str,
    product_name: str,
    evidence: list[Evidence],
    verdict: Classification | None,
) -> str:
    """搜索成功的产品档案写入缓存库（方案 §3 步骤③末句）。"""
    nutrition: dict[str, float] = {}
    urls: list[str] = []
    for ev in evidence:
        for f in ("sugar_g", "fat_g", "sat_fat_g", "fibre_g", "salt_g", "energy_kj"):
            v = getattr(ev, f)
            if v is not None:
                nutrition.setdefault(f, v)
        if ev.url:
            urls.append(ev.url)

    cache_id = new_id()
    with cursor() as cur:
        cur.execute(
            "SELECT id FROM product_cache WHERE lower(brand)=lower(?) AND lower(product_name)=lower(?)",
            (brand, product_name),
        )
        row = cur.fetchone()
        if row:
            cache_id = row["id"]
            cur.execute(
                "UPDATE product_cache SET nutrition_json=?, verdict_json=?, source_urls=? WHERE id=?",
                (
                    json.dumps(nutrition),
                    verdict.model_dump_json() if verdict else None,
                    ",".join(urls),
                    cache_id,
                ),
            )
        else:
            cur.execute(
                "INSERT INTO product_cache"
                " (id,brand,product_name,nutrition_json,verdict_json,source_urls,hit_count,created_at,last_hit_at)"
                " VALUES (?,?,?,?,?,?,0,?,?)",
                (
                    cache_id,
                    brand,
                    product_name,
                    json.dumps(nutrition),
                    verdict.model_dump_json() if verdict else None,
                    ",".join(urls),
                    now(),
                    now(),
                ),
            )

    try:
        vectorstore.collection(COLLECTION).upsert(
            ids=[cache_id],
            documents=[_doc(brand, product_name)],
            metadatas=[{"brand": brand, "product_name": product_name}],
        )
    except Exception:  # noqa: BLE001
        pass
    return cache_id


def stats() -> dict[str, Any]:
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) n, COALESCE(SUM(hit_count),0) hits FROM product_cache")
        row = cur.fetchone()
    return {"products": row["n"], "total_hits": row["hits"], "backend": vectorstore.backend()}
