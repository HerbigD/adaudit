"""缓存取证节点。

单独成节点的理由（方案 §5）：cache 命中时**根本不该发起网络调用**，
拆开后条件边可以在命中时直接跳到 adjudicate；
eval 的"缓存命中率"指标 = 数 web_search 节点被跳过多少次。
"""

from __future__ import annotations

from graph import events
from graph.state import AuditState
from services import cache_store


async def cache_lookup(state: AuditState) -> dict:
    with events.step("cache_lookup") as t:
        initial = state.get("initial")
        if initial is None:
            t.status = "skipped"
            t.summary = "无初分类结果"
            return {"cache_hit": False, "trace": [t]}

        await events.emit_log("cache_lookup", "正在查询产品知识缓存库…")
        record, score = cache_store.lookup(initial.brand, initial.product_name)

        if record and score >= _threshold():
            evidence = cache_store.to_evidence(record)
            t.summary = f"缓存命中 {record['brand']} / {record['product_name']}（score={score:.2f}）"
            await events.emit_log("cache_lookup", f"缓存命中：{record['product_name']} — 免搜索")
            return {"cache_hit": True, "evidence": evidence, "trace": [t]}

        t.status = "ok"
        t.summary = f"缓存未命中（best score={score:.2f}）"
        await events.emit_log("cache_lookup", "缓存未命中，转联网搜索")
        return {"cache_hit": False, "trace": [t]}


def _threshold() -> float:
    from config import settings

    return settings.cache_hit_threshold
