"""人工复核节点 —— 用 LangGraph 的 interrupt() 实现。

图在此**挂起**（状态由 checkpointer 持久化到 SQLite，重启不丢队列），
前端双选项 UI 展示 original vs prediction，人工裁定后携带 human_choice resume。
"""

from __future__ import annotations

from langgraph.types import interrupt

from graph import events
from graph.state import AuditState, Classification
from services import taxonomy


async def human_review(state: AuditState) -> dict:
    initial = state.get("initial")
    revised = state.get("revised")

    payload = {
        "audit_id": state.get("audit_id"),
        "initial": initial.model_dump(mode="json") if initial else None,
        "revised": revised.model_dump(mode="json") if revised else None,
        "reason": _reason(state),
    }
    events.emit("need_human", **{k: v for k, v in payload.items() if k != "audit_id"})

    # 图在这里挂起。resume payload 形如：
    #   {"choice": "original" | "prediction" | "manual", "manual_code": 12}
    decision = interrupt(payload)

    with events.step("human_review") as t:
        choice = (decision or {}).get("choice", "original")
        manual_code = taxonomy.normalize((decision or {}).get("manual_code"))

        if choice == "prediction" and revised is not None:
            final = revised.model_copy(update={"source": "human"})
        elif choice == "manual" and manual_code is not None:
            base = revised or initial
            final = Classification(
                product_name=base.product_name if base else None,
                brand=base.brand if base else None,
                name_brand_identifiable=base.name_brand_identifiable if base else False,
                general_id=taxonomy.general_id_of(manual_code),
                specific_code=manual_code,
                leaf_vs_parent="leaf",
                specific_confidence=1.0,
                general_confidence=1.0,
                reasoning="人工手动指定类别",
                source="human",
                model="human",
                adapter="human",
            )
        else:
            choice = "original"
            final = (
                initial.model_copy(update={"source": "human"})
                if initial
                else Classification(
                    general_id=9,
                    specific_code=33,
                    specific_confidence=0.5,
                    general_confidence=0.5,
                    reasoning="无初分类结果，人工兜底",
                    source="human",
                    adapter="human",
                )
            )

        t.adapter = "human"
        t.summary = f"人工采纳 {choice} → {final.label()}"
        return {
            "human_choice": choice,
            "manual_code": manual_code,
            "final": final,
            "trace": [t],
        }


def _reason(state: AuditState) -> str:
    """卡点原因：给复核队列页展示（低置信 / 搜索失败 / 证据冲突，来自 trace）。"""
    for t in reversed(state.get("trace") or []):
        if t.fallback_reason:
            return {
                "no_result": "搜索无结果",
                "timeout": "搜索超时",
                "budget_exceeded": "搜索预算耗尽",
                "conflict": "证据冲突",
                "invalid_output": "感知输出非法",
                "error": "取证异常",
            }.get(t.fallback_reason, t.fallback_reason)
    initial = state.get("initial")
    if initial and not initial.name_brand_identifiable:
        return "无法识别产品名/品牌"
    if initial and initial.leaf_vs_parent == "parent":
        return "父类已定、细类待定"
    return "置信度不足"
