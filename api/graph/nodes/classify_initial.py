"""感知节点：VLM 看图 → 结构化输出 → 运行时校验 → 粒度自适应。

三件事打包在一个节点里（方案 §5）：校验失败的重试不需要走图的调度，
所以重试在 services/vlm.py 内部完成，节点只处理"彻底失败"的降级。

粒度自适应（Day 3 正文 2）：叶子置信低而父类置信高时，`specific_code` 置空、
按父类输出，`reasoning` 说明哪个营养指标能定夺 —— 由 `vlm.apply_granularity_policy`
统一执行，prompt 里的规则 4 与它互为保险。
"""

from __future__ import annotations

from graph import edges, events
from graph.state import AuditState, Classification
from config import settings
from services import memory, taxonomy, usage, vlm


async def classify_initial(state: AuditState) -> dict:
    with events.step("classify_initial") as t:
        await events.emit_log("classify_initial", "正在识别广告中的产品与品牌…")

        # few-shot 修正记忆注入：相似广告出现时把人工修正样例带进 prompt
        shots = memory.retrieve(state.get("ad_image", ""))
        # 注入证据落 trace：开/关对比要能证明"确实注进去了什么"，
        # 只记条数不够 —— 条数相同但内容不同的两次跑批看起来会一样。
        t.extra["memory_enabled"] = settings.memory_enabled
        t.extra["few_shots_injected"] = len(shots)
        if shots:
            t.extra["few_shots"] = [s[:160] for s in shots]
            await events.emit_log("classify_initial", f"注入 {len(shots)} 条历史修正样例")

        try:
            with usage.collect() as u:
                initial: Classification = await vlm.classify(
                    state["ad_image"], few_shots=shots or None
                )
            t.tokens_in, t.tokens_out, t.cost_usd = u.tokens_in, u.tokens_out, u.cost
        except usage.BudgetExceeded as exc:
            # 成本熔断：不是模型出错，是我们主动拒调。作为正常路由结果转人工。
            t.status = "fallback"
            t.fallback_reason = "budget_exceeded"
            t.adapter = vlm.get_vlm().adapter
            t.summary = f"成本熔断，拒绝调用：{exc}"
            t.extra["budget"] = usage.snapshot()
            await events.emit_log("classify_initial", f"⛔ {t.summary}")
            events.emit("classified", initial=None, error=str(exc), route_1="human")
            return {"initial": None, "route_1": "human", "trace": [t], "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — 感知失败不炸图，转人工
            t.status = "fallback"
            t.fallback_reason = "invalid_output"
            t.adapter = vlm.get_vlm().adapter
            t.summary = f"VLM 失败：{exc}"
            events.emit("classified", initial=None, error=str(exc), route_1="human")
            return {"initial": None, "route_1": "human", "trace": [t], "error": str(exc)}

        # 路由决策显式落进 state（条件边只读不算）
        route = edges.decide_route_1({**state, "initial": initial})

        t.adapter = initial.adapter
        # `update` 而不是 `=`：上面已经往 extra 里写了 few-shot 注入证据，
        # 整体赋值会把它**静默抹掉**。这类"后写的覆盖先写的"在 trace 上尤其难查——
        # 字段不见了不会报错，只是验收时发现证据不在。
        t.extra.update({
            "leaf_vs_parent": initial.leaf_vs_parent,
            "candidate_codes": initial.candidate_codes,
            "general_id": initial.general_id,
            "taxonomy_version": taxonomy.load().version,
        })
        t.summary = (
            f"{initial.label()}｜leaf={initial.specific_confidence:.2f} "
            f"parent={initial.general_confidence:.2f}｜brand={initial.brand or '-'}"
            f" → route_1={route}"
        )
        events.emit(
            "classified", initial=initial.model_dump(mode="json"), route_1=route
        )
        return {"initial": initial, "route_1": route, "trace": [t]}
