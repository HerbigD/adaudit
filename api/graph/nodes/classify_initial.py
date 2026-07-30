"""感知节点：VLM 看图 → 结构化输出 → 运行时校验。

三件事打包在一个节点里（方案 §5）：校验失败的重试不需要走图的调度，
所以重试逻辑在 services/vlm.py 内部完成，节点只处理"彻底失败"的降级。
"""

from __future__ import annotations

from graph import edges, events
from graph.state import AuditState, Classification
from services import memory, vlm


async def classify_initial(state: AuditState) -> dict:
    with events.step("classify_initial") as t:
        await events.emit_log("classify_initial", "正在识别广告中的产品与品牌…")

        # few-shot 修正记忆注入：相似广告出现时把人工修正样例带进 prompt
        shots = memory.retrieve(state.get("ad_image", ""))
        if shots:
            await events.emit_log("classify_initial", f"注入 {len(shots)} 条历史修正样例")

        try:
            initial: Classification | None = await vlm.classify(
                state["ad_image"], few_shots=shots or None
            )
        except Exception as exc:  # noqa: BLE001 — 感知失败不炸图，转人工
            t.status = "fallback"
            t.fallback_reason = "invalid_output"
            t.summary = f"VLM 失败：{exc}"
            events.emit("classified", initial=None, error=str(exc))
            return {"initial": None, "route_1": "human", "trace": [t], "error": str(exc)}

        # 路由决策显式落进 state（条件边只读不算）
        route = edges.decide_route_1({**state, "initial": initial})
        t.summary = (
            f"[{initial.specific_code}] conf={initial.specific_confidence:.2f} "
            f"brand={initial.brand or '-'} → route_1={route}"
        )
        # TODO(W4): 从 provider 回传 token 用量，填 t.tokens_in / t.tokens_out / t.cost_usd
        events.emit("classified", initial=initial.model_dump(mode="json"), route_1=route)
        return {"initial": initial, "route_1": route, "trace": [t]}
