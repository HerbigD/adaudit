"""回流节点：一次写三处 —— eval 集 / few-shot 修正记忆 / 产品缓存库。

放图内而不是图外的理由（方案 §5）：写在图内就进了 trace，
且 interrupt resume 后在同一事务语境完成闭环。

Day 3 加固 2：人工裁定对同产品的 `auto` 档案执行 **supersede**。
实现方式：按 `(lower(brand), lower(product_name))` 唯一键 upsert，
`provenance` 升为 `human_verified`、`revision` 自增、`superseded_at/by` 记录来源 audit，
**不做版本分叉**（理由见 services/cache_store.py 模块 docstring）。
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
        sample_id = memory.remember(
            state.get("ad_image", ""), final, rejected=rejected,
            audit_id=state.get("audit_id"),      # 幂等键：重复回流只留一行
        )
        wrote += ["eval 集", "修正记忆库"]

        # 3) 产品知识缓存库：人工裁定优先级最高，覆盖同产品的 auto 档案
        cache_result = {"action": "skipped", "reason": "缺少 brand/product_name 唯一键"}
        if final.brand and final.product_name:
            if final.specific_code is None:
                cache_result = {"action": "skipped", "reason": "叶子未定，不入库"}
            else:
                cache_result = cache_store.supersede_with_human_verdict(
                    final.brand,
                    final.product_name,
                    evidence,
                    final,
                    audit_id=state.get("audit_id"),
                )
                wrote.append(f"产品缓存库（{cache_result['action']}）")

        t.extra = {
            "cache_write": cache_result,
            "human_choice": state.get("human_choice"),
            # 三处写入各留一个可查的凭据，验收时不用去猜"到底写没写"
            "eval_sample_id": sample_id,
            "memory_vector_id": state.get("audit_id") or sample_id,
        }
        t.summary = "已回流：" + " + ".join(wrote)
        await events.emit_log("feedback_ingest", t.summary)
        return {"trace": [t]}
