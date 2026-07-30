"""联网搜索封装 + 预算 / 超时 / 重试。

方案 §7 红线：每张广告搜索预算上限（默认 3 次查询）+ 单查询超时（10s）+ 预算内重试 1 次；
超限**不抛异常**，而是返回 SearchOutcome(status=...) 让条件边把它当作正常路由结果 → human_review。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Literal

from config import settings

SearchStatus = Literal["ok", "no_result", "timeout", "budget_exceeded", "error"]


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str


@dataclass
class SearchOutcome:
    status: SearchStatus
    hits: list[SearchHit] = field(default_factory=list)
    queries_used: int = 0
    detail: str = ""
    logs: list[str] = field(default_factory=list)


def build_queries(brand: str | None, product_name: str | None) -> list[str]:
    """搜索有锚点才有意义：品牌 / 产品名组合出最多 search_max_queries 条查询。"""
    anchor = " ".join(x for x in (brand, product_name) if x).strip()
    if not anchor:
        return []
    queries = [
        f"{anchor} nutrition information per 100g",
        f"{anchor} 营养成分表",
        f"{anchor} official product page ingredients",
    ]
    return queries[: settings.search_max_queries]


async def _search_once(query: str) -> list[SearchHit]:
    """单次查询。

    TODO(W4): 接 MCP 联网搜索工具（方案 §6「搜索：MCP 联网工具」）。
    接入点建议：一个 MCP client 单例，这里只负责 query -> hits 的形状转换。
    """
    if settings.app_env == "mock":
        await asyncio.sleep(0.5)
        rng = random.Random(query)
        if "nobrand" in query.lower():
            return []
        return [
            SearchHit(
                url=f"https://example-brand.com/products/{rng.randrange(1000)}",
                title=f"[mock] {query[:40]} — Nutrition facts",
                snippet=(
                    "Per 100g: Energy 1580kJ, Fat 4.2g, of which saturates 1.1g, "
                    "Carbohydrate 78g, of which sugars 24.6g, Fibre 3.1g, Salt 0.42g."
                ),
            )
        ]
    raise NotImplementedError("接入 MCP 搜索工具后实现")


async def search_product(
    brand: str | None,
    product_name: str | None,
    *,
    on_log=None,
) -> SearchOutcome:
    """带预算控制的多查询搜索。on_log(msg) 用于把过程推给 SSE（node_log 事件）。"""
    outcome = SearchOutcome(status="no_result")
    queries = build_queries(brand, product_name)
    if not queries:
        outcome.status = "no_result"
        outcome.detail = "没有可用的品牌/产品名锚点"
        return outcome

    for query in queries:
        if outcome.queries_used >= settings.search_max_queries:
            outcome.status = "budget_exceeded"
            outcome.detail = f"搜索预算 {settings.search_max_queries} 次已用尽"
            break

        msg = f"正在搜索「{brand or product_name}」营养成分…"
        outcome.logs.append(msg)
        if on_log:
            await on_log(msg)

        for attempt in range(settings.search_retries + 1):
            outcome.queries_used += 1
            try:
                hits = await asyncio.wait_for(
                    _search_once(query), timeout=settings.search_timeout_s
                )
            except asyncio.TimeoutError:
                if attempt >= settings.search_retries:
                    outcome.status = "timeout"
                    outcome.detail = f"查询超时 >{settings.search_timeout_s}s: {query}"
                continue
            except Exception as exc:  # noqa: BLE001
                outcome.status = "error"
                outcome.detail = f"{type(exc).__name__}: {exc}"
                break
            if hits:
                outcome.hits.extend(hits)
                outcome.status = "ok"
                break
        if outcome.status == "ok":
            break

    if outcome.status == "ok" and on_log:
        await on_log(f"搜索命中 {len(outcome.hits)} 条结果")
    return outcome
