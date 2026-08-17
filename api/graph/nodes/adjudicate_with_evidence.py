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
from services import cache_store, taxonomy, usage, vlm


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
            with usage.collect() as u:
                text = await vlm.complete(taxonomy.build_adjudicate_prompt(), user_prompt)
                revised = vlm.parse_classification(
                    text,
                    source="adjudicator",
                    model=settings.llm_model or settings.qwen_model,
                    adapter=settings.llm_provider,
                )
            t.tokens_in, t.tokens_out, t.cost_usd = u.tokens_in, u.tokens_out, u.cost
        except usage.BudgetExceeded as exc:
            # 熔断时不静默退回规则兜底 —— 那会让一条"省钱的降级"看起来像正常裁决
            t.status = "fallback"
            t.fallback_reason = "budget_exceeded"
            t.summary = f"成本熔断，拒绝裁决：{exc}"
            t.extra["budget"] = usage.snapshot()
            await events.emit_log("adjudicate_with_evidence", f"⛔ {t.summary}")
            return {"route_2": "human", "trace": [t]}
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


_CHEESE_HINT = ("cheese", "奶酪", "芝士", "干酪", "paneer", "haloumi", "halloumi", "feta")


def _looks_like_cheese(initial: Classification) -> bool:
    """5/19 的切分点奶酪是 15g、其余是 3g —— 只能从品名判断是不是奶酪。"""
    blob = f"{initial.product_name or ''} {initial.brand or ''}".lower()
    return any(k in blob for k in _CHEESE_HINT)


def _rule_based(
    initial: Classification,
    evidence: list[Evidence],
    conflict: bool,
    degraded: bool = False,
) -> Classification:
    """无 LLM 时的确定性兜底裁决 —— adapter 打 `rule-fallback`，eval runner 会据此拒绝跑批。

    ## Day6 B1+B2+B3：阈值换成 Annex 4 官方切分点

    此前这里是我按常见 nutrient profiling 惯例**猜的占位值**（糖 15 / 脂 3 /
    脂 10·盐 1.2 / 糖 2.5），并且拿 `sodium × 2.5` 换成盐再比。两处都已删除：

    - 数值统一由 `services/nutrient_rules.decide()` 执行，来源是 `taxonomy.json`
      的 `thresholds`（Annex 4 逐字摘录）。本文件不再写任何阈值。
    - **一律在钠空间比较**，不再换算成盐（Annex 4 全部用 sodium）。营养标签只给盐
      时的 `salt_g × 400 → sodium_mg` 换算在 `nutrient_rules` 里，方向是"盐→钠"。
    - 8/23 与 9 按 **per serve** 判；份量不明就返回 `uncertain`，**不拿 per-100g 顶替**，
      由本函数把它导向人工（`specific_code` 置空 + parent 输出 + 低置信）。

    仍然打 `rule-fallback` adapter：换了权威阈值不等于换成了真实模型裁决，
    eval 的双闸照旧拦它。
    """
    from services import nutrient_rules

    # parent 级输入：从候选里挑；leaf 级输入：以自身为起点
    pool = initial.candidate_codes or (
        [initial.specific_code] if initial.specific_code is not None else []
    )
    blob = f"{initial.product_name or ''} {initial.brand or ''}"
    verdict = nutrient_rules.decide(
        pool,
        evidence,
        is_cheese=_looks_like_cheese(initial),
        # Annex 4 的 7/24（10g 脂肪）只管 savoury sauces，8/24（2g 脂肪）只管汤。
        # 形态判不出时传 None，`decide` 会转人工而不是硬套阈值 —— 否则
        # 橄榄油（~100g 脂肪/100g）会被判进 24。
        is_sauce=nutrient_rules.sauce_form(blob),
        is_soup=nutrient_rules.soup_form(blob),
    )

    if verdict.ok:
        code = verdict.code
        conf = 0.55 if conflict else 0.88
        detail = verdict.reason
    else:
        # 证据不够按 Annex 4 判 —— 保持父类、压低置信，让条件边②把它导向人工。
        # 这里**不许**回落到 pool[0]：随手挑一个候选会把"判不了"伪装成"判出来了"。
        code = None
        conf = 0.40
        detail = verdict.reason

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
            + ("【Annex 4 规则判定】" if verdict.ok else "【Annex 4 判据不足，转人工】")
            + detail
            + (f"（读数 {verdict.used}）" if verdict.used else "")
        ),
        source="adjudicator",
        model="rule-fallback",
        adapter="rule-fallback",
    )
