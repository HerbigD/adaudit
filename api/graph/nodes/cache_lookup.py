"""缓存取证节点。

单独成节点的理由（方案 §5）：cache 命中时**根本不该发起网络调用**，
拆开后条件边可以在命中时直接跳到 adjudicate；
eval 的"缓存命中率"指标 = 数 web_search 节点被跳过多少次。

Day 3 加固 1：命中 `auto` 档案（搜索自动沉淀、未经人工核验）时在 trace 里显式记来源，
这样失败案例归因才分得清"错在感知 / 错在检索 / 错在一条没人核过的缓存"。
"""

from __future__ import annotations

import db
from config import settings
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
        mode = settings.cache_match_mode
        rejected = (record or {}).get("strict_reject_reason")

        if rejected:
            # strict 否决：得分达标但被判不是同一产品。**这不是"未命中"**——
            # 未命中是"库里没有"，否决是"库里有、但我们主动不用"。
            # 两者对下一步该做什么的指向完全不同，所以 trace 里分开记。
            t.summary = (
                f"缓存近名档案被 strict 否决（score={score:.2f}）："
                f"{record.get('brand')} / {record.get('product_name')} — {rejected}"
            )
            t.extra = {
                "match_mode": mode,
                "strict_rejected": True,
                "reject_reason": rejected,
                "near_miss_cache_id": record.get("id"),
                "score": round(score, 3),
            }
            await events.emit_log("cache_lookup", f"近名档案被否决（{rejected}）— 转联网搜索")
            return {"cache_hit": False, "cache_provenance": None, "trace": [t]}

        if record and score >= settings.cache_hit_threshold:
            provenance = record.get("provenance", "auto")
            evidence = cache_store.to_evidence(record)
            verified = provenance == "human_verified"
            tag = "人工核验" if verified else "自动沉淀·未经人工核验"
            t.summary = (
                f"缓存命中 {record['brand']} / {record['product_name']}"
                f"（score={score:.2f}, provenance={provenance}, rev={record.get('revision', 1)}）"
            )
            t.extra = {
                "cache_id": record.get("id"),
                "provenance": provenance,
                "revision": record.get("revision", 1),
                "hit_count": record.get("hit_count", 0),
                "score": round(score, 3),
                "match_mode": mode,
            }
            if not verified:
                # 不是失败，但要在轨迹里留痕：这条证据没人核过
                t.extra["unverified_cache"] = True

            # Day7：命中当下就落观测台。路由与人工结果由 runner._persist 补齐。
            # 写 DB 而不只写 trace 的理由见 db.py 里 cache_hit_log 的建表注释。
            audit_id = state.get("audit_id")
            if audit_id:
                db.log_cache_hit(
                    audit_id,
                    record.get("id"),
                    round(score, 4),
                    provenance,
                    mode,
                )
            await events.emit_log(
                "cache_lookup", f"缓存命中：{record['product_name']}（{tag}）— 免搜索"
            )
            return {
                "cache_hit": True,
                "cache_provenance": provenance,
                "search_status": "cache",
                "evidence": evidence,
                "trace": [t],
            }

        t.extra = {"match_mode": mode, "score": round(score, 3)}
        t.summary = f"缓存未命中（best score={score:.2f}）"
        await events.emit_log("cache_lookup", "缓存未命中，转联网搜索")
        return {"cache_hit": False, "cache_provenance": None, "trace": [t]}
