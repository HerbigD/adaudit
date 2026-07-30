"""重裁决节点：输入 initial + evidence，产出 revised。

单独成节点的理由（方案 §5）：**它是 LLM 调用，搜索是工具调用**——失败模式、
重试策略、成本完全不同；而且 cache 命中路径也要复用它（缓存档案也是 evidence）。
revised 与 initial 并列为两个独立字段，双选项 UI 就靠这两个字段并存。

这里也是**叶子回落点**：粒度自适应在初分类时把 specific 置空按父类输出，
拿到营养证据后必须落回具体叶子（方案 §2 末句"最后要落回到子类上"）。
"""

from __future__ import annotations

import json

from config import settings
from graph import edges, events
from graph.state import AuditState, Classification, Evidence
from services import cache_store, nutrition, taxonomy, vlm


def _evidence_block(evidence: list[Evidence]) -> str:
    """Evidence 是契约不是文本：这里输出的是结构化读数 + id，裁决结论要引用得到 id。"""
    lines = []
    for ev in evidence:
        facts = {
            nv.nutrient: {"value": nv.value, "unit": nv.unit, "normalized": nv.normalized}
            for nv in ev.nutrients
        }
        tags = [f"source_type={ev.source_type}", f"provenance={ev.provenance}", f"tier={ev.query_tier}"]
        if ev.cache_provenance:
            tags.append(f"cache={ev.cache_provenance}")
        if ev.is_degraded:
            tags.append("DEGRADED(no nutrition panel)")
        lines.append(
            f"[{ev.id}] {' '.join(tags)} url={ev.source_url or '-'}\n"
            f"    nutrients={json.dumps(facts, ensure_ascii=False)}\n"
            f"    hint={ev.conclusion_hint or '-'}\n"
            f"    snippet={(ev.snippet or '')[:300]}"
        )
    return "\n".join(lines) or "(no evidence)"


async def adjudicate_with_evidence(state: AuditState) -> dict:
    with events.step("adjudicate_with_evidence") as t:
        initial = state.get("initial")
        evidence = state.get("evidence") or []
        search_status = state.get("search_status")

        if initial is None:
            t.status = "skipped"
            return {"route_2": "human", "trace": [t]}

        if not evidence:
            # 取证失败：不重裁决，保持 initial，让条件边②把它导向人工
            t.status = "fallback"
            t.fallback_reason = "no_result"
            t.summary = "无证据，跳过重裁决"
            return {"route_2": "human", "trace": [t]}

        # 冲突已在 web_search 节点按 Day5 §6 判定过，这里只消费结论
        conflict = search_status == "conflict"
        degraded = search_status == "degraded" or all(e.is_degraded for e in evidence)
        await events.emit_log("adjudicate_with_evidence", "正在结合营养证据重新裁决…")

        user_prompt = (
            f"INITIAL CLASSIFICATION:\n{initial.model_dump_json(indent=2)}\n\n"
            f"EVIDENCE (search_status={search_status}):\n{_evidence_block(evidence)}\n\n"
            + (
                "NOTE: the evidence below is DEGRADED — no parsable nutrition panel was found. "
                "You must state in `reasoning` that the call is based on unstructured evidence, "
                "and keep confidence low.\n\n"
                if degraded
                else ""
            )
            + "Re-adjudicate now."
        )

        try:
            text = await vlm.complete(taxonomy.build_adjudicate_prompt(), user_prompt)
            revised = vlm.parse_classification(
                text,
                source="adjudicator",
                model=settings.llm_provider,
                adapter=settings.llm_provider,
            )
        except Exception:  # noqa: BLE001 — mock 模式或裁决失败都走规则兜底
            revised = _rule_based(initial, evidence, conflict, degraded)

        revised = revised.model_copy(
            update={
                "conflict": conflict,
                # 引用的是 Evidence.id（ev_001…），不是下标 —— groundedness 要能核到具体条目
                "evidence_refs": [e.id for e in evidence if e.id],
            }
        )
        t.adapter = revised.adapter
        if conflict:
            t.fallback_reason = "conflict"
            t.status = "fallback"

        # ---------- Day 3 加固 1：auto 档案写入护栏 ----------
        ok, why = cache_store.should_cache(revised, evidence, search_status)
        if ok:
            result = cache_store.upsert(
                revised.brand,
                revised.product_name,
                evidence,
                revised,
                provenance="auto",
                audit_id=state.get("audit_id"),
            )
            t.extra["cache_write"] = result
        else:
            t.extra["cache_write"] = {"action": "skipped", "reason": why}

        route = edges.decide_route_2({**state, "revised": revised})
        t.summary = (
            f"{initial.label()} → {revised.label()}"
            f"｜conf {initial.specific_confidence:.2f}→{revised.specific_confidence:.2f}"
            + ("｜证据冲突" if conflict else "")
            + f"｜缓存:{t.extra['cache_write']['action']}"
            + f" → route_2={route}"
        )
        return {"revised": revised, "route_2": route, "trace": [t]}


def _pick_nutrient(evidence: list[Evidence], nutrient: str) -> float | None:
    """按 source_type 优先级取高可信来源的 normalized 值（Day5 §6 设计理由末句）。"""
    ranked = sorted(
        evidence, key=lambda e: nutrition.SOURCE_RANK.get(e.source_type, 9)
    )
    for e in ranked:
        v = e.get(nutrient)
        if v is not None:
            return v
    return None


def _rule_based(
    initial: Classification,
    evidence: list[Evidence],
    conflict: bool,
    degraded: bool = False,
) -> Classification:
    """无 LLM 时的确定性兜底裁决 —— adapter 打 `rule-fallback`，eval runner 会据此拒绝跑批。

    只处理 taxonomy.json 里 confusing_pairs 声明的维度，且**阈值是占位值**，
    接真实营养分级模型前不可用于出指标。
    """
    sugar = _pick_nutrient(evidence, "sugar")
    fibre = _pick_nutrient(evidence, "fiber")
    fat = _pick_nutrient(evidence, "fat")
    sodium = _pick_nutrient(evidence, "sodium")
    # 阈值沿用盐口径（g/100g）：钠 × 2.5 ≈ 盐
    salt = sodium * 2.5 if sodium is not None else None

    # parent 级输入：从候选里挑；leaf 级输入：以自身为起点
    pool = initial.candidate_codes or (
        [initial.specific_code] if initial.specific_code is not None else []
    )
    code = pool[0] if pool else initial.specific_code
    bits: list[str] = []

    def pick(a: int, b: int, hi_cond: bool, note: str) -> int:
        bits.append(note)
        return b if hi_cond else a

    pool_set = set(pool)
    if {2, 12} & pool_set and sugar is not None:
        code = pick(2, 12, sugar >= 15 or (fibre is not None and fibre < 3),
                    f"糖 {sugar}g/100g" + (f"、纤维 {fibre}g" if fibre is not None else ""))
    elif {5, 19} & pool_set and fat is not None:
        code = pick(5, 19, fat >= 3.0, f"脂肪 {fat}g/100g")
    elif {8, 23} & pool_set and (fat is not None or salt is not None):
        code = pick(8, 23, (fat or 0) >= 10 or (salt or 0) >= 1.2,
                    "、".join(x for x in [f"脂肪 {fat}g" if fat is not None else "",
                                         f"盐 {salt}g" if salt is not None else ""] if x))
    elif {16, 17} & pool_set and (sugar is not None or salt is not None):
        code = pick(17, 16, (sugar or 0) >= 15,
                    "、".join(x for x in [f"糖 {sugar}g" if sugar is not None else "",
                                         f"盐 {salt}g" if salt is not None else ""] if x))
    # taxonomy v1.0-codebook 后混淆对从 (11,18)/(11,25) 变成 (18,25)/(25,29)。
    # **阈值一个没动**（仍是 2.5 g/100ml），只是把分支挂到现行的对上 ——
    # 旧分支在新语义下会得出"糖高 → 瓶装水"这种反向结论，留着比删掉更危险。
    elif {18, 25} & pool_set and sugar is not None:
        code = pick(18, 25, sugar >= 2.5, f"糖 {sugar}g/100ml")
    elif {25, 29} & pool_set and sugar is not None:
        code = pick(29, 25, sugar >= 2.5, f"糖 {sugar}g/100ml")

    changed = code != initial.specific_code
    conf = 0.55 if conflict else (0.88 if bits else 0.62)
    if degraded:
        conf = min(conf, 0.60)          # 降级证据不配拿高置信
    gid = taxonomy.general_id_of(code) if code else initial.general_id
    return Classification(
        product_name=initial.product_name,
        brand=initial.brand,
        name_brand_identifiable=initial.name_brand_identifiable,
        ad_language=initial.ad_language,
        country=initial.country,
        general_id=gid,
        specific_code=code,
        candidate_codes=pool,
        leaf_vs_parent="leaf" if code is not None else "parent",
        specific_confidence=conf,
        general_confidence=max(initial.general_confidence, conf),
        reasoning=(
            ("【基于非结构化证据】" if degraded else "")
            + "依据检索到的营养证据"
            + ("重判" if changed else "确认初判")
            + f"：{'、'.join(bits) if bits else '证据未覆盖判定阈值'}"
        ),
        source="adjudicator",
        model="rule-fallback",
        adapter="rule-fallback",
    )
