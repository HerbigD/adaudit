"""联网取证节点：查询构造 → 搜索执行 → 结果筛选 → 营养抽取 → 冲突判定 → search_status。

关键设计（方案 §5 兜底边 / Day5 §1 原则 3）：无结果 / 超时 / 预算超限 / 证据冲突
**都不作为异常抛出**，而是写进 state 的 `search_status` 与 trace，让条件边②当作正常路由结果。
异常路径产品化。

trace.extra 必含（Day5 §8）：queries / candidates_screened / evidence_ids / search_status / 失败原因。
"""

from __future__ import annotations

from config import settings
from graph import events
from graph.state import AuditState
from services import nutrition, search, taxonomy, usage


async def web_search(state: AuditState) -> dict:
    with events.step("web_search") as t:
        initial = state.get("initial")
        if initial is None:
            t.status = "skipped"
            return {"search_status": "skipped", "trace": [t]}

        async def on_log(msg: str) -> None:
            await events.emit_log("web_search", msg)

        country = initial.country or settings.default_country

        # ---------- 1. 查询构造 + 执行 ----------
        with usage.collect() as u_search:
            try:
                outcome = await search.search_product(
                    initial.brand,
                    initial.product_name,
                    ad_language=initial.ad_language,
                    country=country,
                    on_log=on_log,
                )
            except usage.BudgetExceeded as exc:
                t.status = "fallback"
                t.fallback_reason = "budget_exceeded"
                t.summary = f"成本熔断，拒绝取证：{exc}"
                t.extra["budget"] = usage.snapshot()
                await events.emit_log("web_search", f"⛔ {t.summary}")
                return {"search_status": "budget_exceeded", "trace": [t]}
        t.queries_used = outcome.queries_used
        t.extra["queries"] = [
            {
                "text": r.text,
                "tier": r.tier,
                "ms": r.ms,
                "results": r.results,
                "status": r.status,
                "attempts": r.attempts,
            }
            for r in outcome.records
        ]
        t.extra["ad_language"] = initial.ad_language
        t.extra["country"] = country

        if outcome.status != "ok":
            t.status = "fallback"
            t.fallback_reason = outcome.status
            t.summary = outcome.detail or f"搜索未成功：{outcome.status}"
            t.extra["search_status"] = outcome.status
            await events.emit_log("web_search", f"搜索兜底：{t.summary} → 转人工复核")
            return {"search_status": outcome.status, "trace": [t]}

        # ---------- 2. 候选筛选（纯 Python，零成本） ----------
        candidates, screen_stats = nutrition.screen_candidates(
            outcome.hits[: settings.search_hits_per_query],
            brand=initial.brand,
            product_name=initial.product_name,
            country=country,
        )
        t.extra["candidates_screened"] = screen_stats

        if not candidates:
            t.status = "fallback"
            t.fallback_reason = "no_result"
            t.summary = f"{screen_stats['in']} 条结果全被筛掉（黑名单/标题无重叠）"
            t.extra["search_status"] = "no_result"
            await events.emit_log("web_search", f"{t.summary} → 转人工复核")
            return {"search_status": "no_result", "trace": [t]}

        await events.emit_log(
            "web_search", f"筛出 {len(candidates)} 个候选来源，正在抽取营养成分…"
        )

        # ---------- 3. 抽取 ----------
        with usage.collect() as u_extract:
            evidence, mode = await nutrition.extract_evidence(
                candidates,
                brand=initial.brand,
                product_name=initial.product_name,
                ad_language=initial.ad_language,
                country=country,
                query=outcome.hit_query,
            )
        evidence = nutrition.assign_ids(evidence)
        # 搜索与抽取两段的真实用量合并进本节点的 trace
        t.tokens_in = u_search.tokens_in + u_extract.tokens_in
        t.tokens_out = u_search.tokens_out + u_extract.tokens_out
        t.cost_usd = round(u_search.cost + u_extract.cost, 6)
        t.extra["usage"] = {"search_calls": u_search.calls, "extract_calls": u_extract.calls}
        t.adapter = evidence[0].extracted_by if evidence else "degraded"
        t.extra["extract_mode"] = mode
        t.extra["evidence_ids"] = [e.id for e in evidence]

        if not evidence:
            t.status = "fallback"
            t.fallback_reason = "no_result"
            t.summary = "抽取未产出任何 Evidence"
            t.extra["search_status"] = "no_result"
            return {"search_status": "no_result", "trace": [t]}

        # ---------- 4. 冲突判定 ----------
        target = _target_codes(state)
        conflicted, why = nutrition.detect_conflict(evidence, target)
        t.extra["conflict_check"] = {"codes": sorted(target), "verdict": conflicted, "why": why}

        # ---------- 5. search_status 状态机 ----------
        status = decide_status(evidence, conflicted, outcome.tier, target)
        t.extra["search_status"] = status
        if status in ("degraded", "conflict"):
            t.status = "fallback"
            t.fallback_reason = status if status == "conflict" else "degraded_evidence"

        t.summary = (
            f"{len(evidence)} 条证据（{mode}, Q{outcome.tier}）｜"
            f"{nutrition.summarize(evidence)}｜status={status}"
        )
        await events.emit_log("web_search", f"提取证据：{nutrition.summarize(evidence)}")
        if conflicted:
            await events.emit_log("web_search", f"证据冲突：{why} → 转人工裁定")

        return {"search_status": status, "evidence": evidence, "trace": [t]}


def _target_codes(state: AuditState) -> set[int]:
    """本次取证要定夺的候选叶子 —— 冲突判定的第 3 个条件靠它。"""
    initial = state.get("initial")
    if initial is None:
        return set()
    codes = set(initial.candidate_codes)
    if initial.specific_code is not None:
        codes.add(initial.specific_code)
    return codes


def decide_status(evidence, conflicted: bool, tier: int, target_codes=None) -> str:
    """Day5 §7 状态机。conflict 优先级最高，其次 degraded，最后 ok。

    `ok` 的定义是"**≥1 条 Evidence 含目标维度的 normalized 值**"——
    抽到了读数但换算不出（per serving 缺份量）同样不算 ok：那种值卡不了阈值，
    给它 ok 的直出门槛等于让一个没法比大小的数字决定分类。
    """
    if conflicted:
        return "conflict"
    if all(e.is_degraded for e in evidence):
        return "degraded"
    if all(e.query_tier == 3 for e in evidence):
        return "degraded"

    dims = taxonomy.pair_nutrients(target_codes) or nutrition.NUTRIENTS
    has_usable = any(e.get(n) is not None for e in evidence for n in dims)
    return "ok" if has_usable else "degraded"
