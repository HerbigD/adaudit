"""出口节点：把 initial / revised 收敛成 final。

方案 §5：无论走哪条路，图结束前 final 必定有值，下游（批次报告、反馈回流）
只消费 final。快路径（direct）与验证路径（direct_verified）共用这个收敛点。
"""

from __future__ import annotations

from graph import events
from graph.state import AuditState


async def output(state: AuditState) -> dict:
    with events.step("output") as t:
        revised = state.get("revised")
        initial = state.get("initial")
        final = revised or initial
        route = state.get("route_2") or state.get("route_1")

        if final is None:
            t.status = "error"
            t.summary = "无可用分类结果"
            return {"trace": [t]}

        t.summary = (
            f"final=[{final.specific_code}] "
            f"conf={final.specific_confidence:.2f} route={route} "
            f"level={final.display_level}"
        )
        # `done` 事件由 services/runner 在落库后统一发出（单一出口，避免重复推送）
        return {"final": final, "trace": [t]}
