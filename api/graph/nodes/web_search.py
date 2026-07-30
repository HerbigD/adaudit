"""联网取证节点。内部含预算控制：搜索次数上限、超时熔断、预算内重试一次。

关键设计（方案 §5 兜底边）：无结果 / 超时 / 预算超限**不作为异常抛出**，
而是写进 state 的 trace 并让条件边②把它当作正常路由结果导向 human_review。
异常路径产品化。
"""

from __future__ import annotations

from graph import events
from graph.state import AuditState
from services import nutrition, search


async def web_search(state: AuditState) -> dict:
    with events.step("web_search") as t:
        initial = state.get("initial")
        if initial is None:
            t.status = "skipped"
            return {"trace": [t]}

        async def on_log(msg: str) -> None:
            await events.emit_log("web_search", msg)

        outcome = await search.search_product(
            initial.brand, initial.product_name, on_log=on_log
        )
        t.queries_used = outcome.queries_used

        if outcome.status != "ok":
            t.status = "fallback"
            t.fallback_reason = outcome.status  # no_result | timeout | budget_exceeded | error
            t.summary = outcome.detail or f"搜索未成功：{outcome.status}"
            await events.emit_log("web_search", f"搜索兜底：{t.summary} → 转人工复核")
            return {"trace": [t]}

        evidence = nutrition.extract(outcome.hits)
        t.summary = f"{len(evidence)} 条证据｜{nutrition.summarize(evidence)}"
        await events.emit_log("web_search", f"提取证据：{nutrition.summarize(evidence)}")
        return {"evidence": evidence, "trace": [t]}
