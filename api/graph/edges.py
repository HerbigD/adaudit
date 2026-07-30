"""条件边函数。

设计要点：**决策逻辑（decide_*）与条件边（route_*）分离**。
decide_* 是纯函数，由节点调用并把结果显式写进 state；条件边只是读 state。
好处：trace / eval 归因 / UI 都能直接读到"为什么走了这条路"，
而且 decide_* 可以脱离图单测（构造不同置信度即可）。
"""

from __future__ import annotations

from config import settings
from graph.state import AuditState, Route1, Route2


# --------------------------------------------------------------------------- #
# 条件边① f(initial.confidence, 名称品牌可识别) → direct | search | human
# --------------------------------------------------------------------------- #
def decide_route_1(state: AuditState) -> Route1:
    initial = state.get("initial")
    if initial is None:
        return "human"                                   # 感知失败 → 人工

    high_specific = initial.specific_confidence >= settings.direct_threshold
    if high_specific:
        return "direct"                                  # 多数样本走这里，控制成本

    has_anchor = bool(
        initial.name_or_brand_legible and (initial.brand or initial.product_name)
    )
    # 取证有锚点才有意义；搜索都没关键词，别浪费预算
    return "search" if has_anchor else "human"


def route_1(state: AuditState) -> Route1:
    return state.get("route_1") or decide_route_1(state)


# --------------------------------------------------------------------------- #
# cache_lookup 后的分流：hit → 直接裁决（不发网络调用）；miss → 搜索
# --------------------------------------------------------------------------- #
def cache_hit(state: AuditState) -> str:
    return "hit" if state.get("cache_hit") else "miss"


# --------------------------------------------------------------------------- #
# 条件边② f(revised.confidence, 搜索健康度) → direct_verified | human
# --------------------------------------------------------------------------- #
def decide_route_2(state: AuditState) -> Route2:
    revised = state.get("revised")
    if revised is None:
        return "human"                                   # 取证失败/无重裁决
    if revised.conflict:
        return "human"                                   # 证据冲突 → 人工
    if not state.get("evidence"):
        return "human"
    return (
        "direct_verified"
        if revised.specific_confidence >= settings.verified_threshold
        else "human"
    )


def route_2(state: AuditState) -> Route2:
    return state.get("route_2") or decide_route_2(state)
