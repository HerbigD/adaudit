"""回流节点：一次写三处 —— eval 集 / few-shot 修正记忆 / 产品缓存库。

放图内而不是图外的理由（方案 §5）：写在图内就进了 trace，
且 interrupt resume 后在同一事务语境完成闭环。
"""

from __future__ import annotations

from graph import events
from graph.state import AuditState
from services import cache_store, memory


async def feedback_ingest(state: AuditState) -> dict:
    with events.step("feedback_ingest") as t:
        final = state.get("final")
        initial = state.get("initial")
        revised = state.get("revised")
        evidence = state.get("evidence") or []
        wrote: list[str] = []

        if final is None:
            t.status = "skipped"
            t.summary = "无 final，跳过回流"
            return {"trace": [t]}

        # 1) eval 集 + 2) few-shot 修正记忆（同一次调用写两处）
        rejected = revised if state.get("human_choice") == "original" else initial
        memory.remember(state.get("ad_image", ""), final, rejected=rejected)
        wrote += ["eval 集", "修正记忆库"]

        # 3) 产品知识缓存库：有品牌锚点且有证据才沉淀
        if final.brand and final.product_name and evidence:
            cache_store.upsert(final.brand, final.product_name, evidence, final)
            wrote.append("产品缓存库")

        t.summary = "已回流：" + " + ".join(wrote)
        await events.emit_log("feedback_ingest", t.summary)
        return {"trace": [t]}
