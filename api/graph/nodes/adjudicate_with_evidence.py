"""重裁决节点：输入 initial + evidence，产出 revised。

单独成节点的理由（方案 §5）：**它是 LLM 调用，搜索是工具调用**——失败模式、
重试策略、成本完全不同；而且 cache 命中路径也要复用它（缓存档案也是 evidence）。
revised 与 initial 并列为两个独立字段，双选项 UI 就靠这两个字段并存。
"""

from __future__ import annotations

import json

from config import settings
from graph import edges, events
from graph.state import AuditState, Classification, Evidence
from services import cache_store, nutrition, taxonomy, vlm


def _evidence_block(evidence: list[Evidence]) -> str:
    lines = []
    for i, ev in enumerate(evidence):
        facts = {
            k: getattr(ev, k)
            for k in ("sugar_g", "fat_g", "sat_fat_g", "fibre_g", "salt_g", "energy_kj")
            if getattr(ev, k) is not None
        }
        lines.append(
            f"[{i}] source={ev.source} url={ev.url or '-'}\n"
            f"    nutrition(per 100g/ml)={json.dumps(facts)}\n"
            f"    snippet={(ev.snippet or '')[:300]}"
        )
    return "\n".join(lines) or "(no evidence)"


async def adjudicate_with_evidence(state: AuditState) -> dict:
    with events.step("adjudicate_with_evidence") as t:
        initial = state.get("initial")
        evidence = state.get("evidence") or []

        if initial is None:
            t.status = "skipped"
            return {"route_2": "human", "trace": [t]}

        if not evidence:
            # 取证失败：不重裁决，保持 initial，让条件边②把它导向人工
            t.status = "fallback"
            t.fallback_reason = "no_result"
            t.summary = "无证据，跳过重裁决"
            return {"route_2": "human", "trace": [t]}

        conflict = nutrition.has_conflict(evidence)
        await events.emit_log("adjudicate_with_evidence", "正在结合营养证据重新裁决…")

        user_prompt = (
            f"INITIAL CLASSIFICATION:\n{initial.model_dump_json(indent=2)}\n\n"
            f"EVIDENCE:\n{_evidence_block(evidence)}\n\n"
            "Re-adjudicate now."
        )

        try:
            text = await vlm.complete(taxonomy.build_adjudicate_prompt(), user_prompt)
            revised = vlm.parse_classification(
                text, source="adjudicator", model=settings.llm_provider
            )
        except Exception:  # noqa: BLE001 — mock 模式或裁决失败都走规则兜底
            revised = _rule_based(initial, evidence, conflict)

        revised.conflict = conflict
        revised.evidence_refs = list(range(len(evidence)))
        if conflict:
            t.fallback_reason = "conflict"
            t.status = "fallback"

        # 搜索成功的产品档案写入缓存库（方案 §3 步骤③末句）：
        # 放这里而不是 feedback_ingest —— 快路径样本不经人工复核，
        # 若只在回流节点写，缓存永远只积累"被人工看过"的产品，命中率上不来。
        if (
            not conflict
            and revised.brand
            and revised.product_name
            and any(e.source == "web" for e in evidence)
        ):
            cache_store.upsert(revised.brand, revised.product_name, evidence, revised)

        route = edges.decide_route_2({**state, "revised": revised})
        t.summary = (
            f"[{initial.specific_code}] → [{revised.specific_code}] "
            f"conf {initial.specific_confidence:.2f}→{revised.specific_confidence:.2f}"
            + ("｜证据冲突" if conflict else "")
            + f" → route_2={route}"
        )
        return {"revised": revised, "route_2": route, "trace": [t]}


def _rule_based(
    initial: Classification, evidence: list[Evidence], conflict: bool
) -> Classification:
    """无 LLM 时的确定性兜底裁决。

    只处理三组已知混淆对的阈值判定 —— 与 eval 的 confusion matrix 重点一致。
    TODO(W4): 接上真实 LLM 后，此函数退化为 LLM 失败时的降级路径。
    """
    sugar = next((e.sugar_g for e in evidence if e.sugar_g is not None), None)
    fibre = next((e.fibre_g for e in evidence if e.fibre_g is not None), None)
    fat = next((e.fat_g for e in evidence if e.fat_g is not None), None)
    salt = next((e.salt_g for e in evidence if e.salt_g is not None), None)

    code = initial.specific_code
    bits: list[str] = []

    # [2] 低糖高纤谷物 vs [12] 高糖/低纤谷物
    if code in (2, 12) and sugar is not None:
        code = 12 if (sugar >= 15 or (fibre is not None and fibre < 3)) else 2
        bits.append(f"糖 {sugar}g/100g" + (f"、纤维 {fibre}g" if fibre is not None else ""))
    # [5] 低脂乳制品 vs [19] 全脂乳制品
    elif code in (5, 19) and fat is not None:
        code = 19 if fat >= 3.0 else 5
        bits.append(f"脂肪 {fat}g/100g")
    # [8] 低脂低盐餐食 vs [23] 高脂高盐餐食
    elif code in (8, 23) and (fat is not None or salt is not None):
        code = 23 if (fat or 0) >= 10 or (salt or 0) >= 1.2 else 8
        bits.append(
            "、".join(x for x in [f"脂肪 {fat}g" if fat is not None else "",
                                 f"盐 {salt}g" if salt is not None else ""] if x)
        )

    changed = code != initial.specific_code
    conf = 0.55 if conflict else (0.88 if bits else 0.62)
    return Classification(
        product_name=initial.product_name,
        brand=initial.brand,
        general_category=taxonomy.general_of(code),
        specific_code=code,
        specific_confidence=conf,
        general_confidence=max(initial.general_confidence, conf),
        reasoning=(
            "依据检索到的营养证据"
            + ("重判" if changed else "确认初判")
            + f"：{'、'.join(bits) if bits else '证据未覆盖判定阈值'}"
        ),
        alternative_code=initial.specific_code if changed else initial.alternative_code,
        name_or_brand_legible=initial.name_or_brand_legible,
        source="adjudicator",
        model="rule-fallback",
    )
