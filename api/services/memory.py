"""few-shot 修正记忆：人工修正样例向量化，相似广告出现时检索注入 prompt。

不单独建表（方案 §2 末尾）：eval_samples 里 source='human_feedback' 的记录
向量化进 Chroma 的 `memory` collection。
"""

from __future__ import annotations

from typing import Any

from config import settings
from db import add_eval_sample
from graph.state import Classification
from services import taxonomy, vectorstore

COLLECTION = "memory"


def _doc(brand: str | None, product_name: str | None, reasoning: str) -> str:
    return " | ".join(x for x in (brand, product_name, reasoning[:120]) if x)


def remember(
    image_path: str,
    corrected: Classification,
    *,
    rejected: Classification | None = None,
) -> str:
    """一次写两处：eval 集（标注扩充）+ 记忆库（向量化）。"""
    is_pair = any(
        corrected.specific_code in pair and (rejected and rejected.specific_code in pair)
        for pair in taxonomy.CONFUSING_PAIRS
    )
    sample_id = add_eval_sample(
        image_path=image_path,
        gold_general=corrected.general_category,
        gold_specific=str(corrected.specific_code),
        source="human_feedback",
        is_confusing_pair=is_pair,
    )
    doc = _doc(corrected.brand, corrected.product_name, corrected.reasoning)
    try:
        vectorstore.collection(COLLECTION).upsert(
            ids=[sample_id],
            documents=[doc or image_path],
            metadatas=[
                {
                    "corrected_code": corrected.specific_code,
                    "corrected_general": corrected.general_category,
                    "rejected_code": rejected.specific_code if rejected else -1,
                    "reasoning": corrected.reasoning[:300],
                    "brand": corrected.brand or "",
                    "product_name": corrected.product_name or "",
                }
            ],
        )
    except Exception:  # noqa: BLE001
        pass
    return sample_id


def retrieve(query: str, k: int | None = None) -> list[str]:
    """返回可直接拼进 system prompt 的 few-shot 文本块。"""
    k = k or settings.memory_topk
    try:
        res = vectorstore.collection(COLLECTION).query(query_texts=[query or " "], n_results=k)
    except Exception:  # noqa: BLE001
        return []
    metas: list[dict[str, Any]] = res.get("metadatas", [[]])[0]
    shots: list[str] = []
    for m in metas:
        if not m:
            continue
        rejected = m.get("rejected_code", -1)
        rej_txt = (
            f"（模型曾误判为 [{rejected}]）"
            if isinstance(rejected, int) and taxonomy.is_valid(rejected)
            else ""
        )
        shots.append(
            f"- 广告「{m.get('brand','')} {m.get('product_name','')}」"
            f"人工裁定为 [{m.get('corrected_code')}] {rej_txt}；依据：{m.get('reasoning','')}"
        )
    return shots


def stats() -> dict[str, Any]:
    try:
        return {"shots": vectorstore.collection(COLLECTION).count(),
                "backend": vectorstore.backend()}
    except Exception:  # noqa: BLE001
        return {"shots": 0, "backend": vectorstore.backend()}
