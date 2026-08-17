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
    audit_id: str | None = None,
) -> str:
    """一次写两处：eval 集（标注扩充）+ 记忆库（向量化）。

    给了 `audit_id` 就**两处都按它幂等**：eval 行按 audit_id upsert，
    向量 id 直接用 audit_id。不给则退回一次性 uuid（旧行为，供图外调用）。

    为什么必须幂等：`resume` 会重新驱动整张图，人工也可能改主意再裁一次。
    不去重的话，同一条修正会在 eval 集里堆成好几行 —— 而 eval 集是要拿去算
    准确率的，重复样本等于给某几张图加权。
    """
    is_pair = taxonomy.is_confusing_pair(
        corrected.specific_code, rejected.specific_code if rejected else None
    )
    sample_id = add_eval_sample(
        image_path=image_path,
        gold_general=corrected.general_category,
        gold_specific=str(corrected.specific_code) if corrected.specific_code else None,
        source="human_feedback",
        is_confusing_pair=is_pair,
        audit_id=audit_id,
    )
    vector_id = audit_id or sample_id
    doc = _doc(corrected.brand, corrected.product_name, corrected.reasoning)
    try:
        vectorstore.collection(COLLECTION).upsert(
            ids=[vector_id],
            documents=[doc or image_path],
            metadatas=[
                {
                    "corrected_code": corrected.specific_code or -1,
                    "corrected_general": corrected.general_category,
                    "rejected_code": (rejected.specific_code if rejected else None) or -1,
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
    """返回可直接拼进 system prompt 的 few-shot 文本块。

    `MEMORY_ENABLED=false` 时**返回空**而不是绕过调用点 ——
    开关对比实验要的是"同一条代码路径，只有注入内容不同"，
    在调用点上加 if 会让两臂走不同的代码，比出来的差异不干净。
    """
    if not settings.memory_enabled:
        return []
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
