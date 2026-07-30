"""评测指标（方案 §7）。

1. Exact-match 准确率（33 细类）与 General 容错准确率（12 大类）
2. 搜索增强收益：低置信样本 搜索前 vs 搜索后
3. Groundedness：重裁决结论引用营养证据的比率
4. 人工复核率（随时间下降 → 看板曲线）
5. 缓存命中率 / 单条成本 / 端到端延迟
6. Confusion matrix（重点 2/12、5/19、8/23）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from services import taxonomy


@dataclass
class Prediction:
    audit_id: str
    gold_specific: int
    initial_specific: int | None
    final_specific: int | None
    initial_confidence: float
    final_confidence: float
    route_1: str | None
    route_2: str | None
    used_evidence: bool
    cache_hit: bool
    latency_ms: int = 0
    cost_usd: float = 0.0
    trace: list[dict] = field(default_factory=list)


def _acc(pairs: list[tuple[int, int | None]]) -> float:
    if not pairs:
        return 0.0
    return sum(1 for g, p in pairs if p is not None and g == p) / len(pairs)


def exact_match(preds: list[Prediction]) -> float:
    return _acc([(p.gold_specific, p.final_specific) for p in preds])


def general_match(preds: list[Prediction]) -> float:
    pairs = [
        (
            taxonomy.general_of(p.gold_specific),
            taxonomy.general_of(p.final_specific) if p.final_specific else None,
        )
        for p in preds
    ]
    if not pairs:
        return 0.0
    return sum(1 for g, q in pairs if q is not None and g == q) / len(pairs)


def search_gain(preds: list[Prediction], low_conf_threshold: float = 0.85) -> dict[str, float]:
    """低置信样本中，搜索前 vs 搜索后的准确率提升（X% → Y%）—— 慢路径的价值证明。"""
    low = [p for p in preds if p.initial_confidence < low_conf_threshold]
    before = _acc([(p.gold_specific, p.initial_specific) for p in low])
    after = _acc([(p.gold_specific, p.final_specific) for p in low])
    return {"n": len(low), "before": before, "after": after, "gain": after - before}


def groundedness(preds: list[Prediction]) -> float:
    """走了取证路径的样本里，最终结论确实引用了营养证据的比率。"""
    searched = [p for p in preds if p.route_1 == "search"]
    if not searched:
        return 0.0
    return sum(1 for p in searched if p.used_evidence) / len(searched)


def human_review_rate(preds: list[Prediction]) -> float:
    if not preds:
        return 0.0
    return sum(1 for p in preds if p.route_2 == "human" or p.route_1 == "human") / len(preds)


def cache_hit_rate(preds: list[Prediction]) -> float:
    searched = [p for p in preds if p.route_1 == "search"]
    if not searched:
        return 0.0
    return sum(1 for p in searched if p.cache_hit) / len(searched)


def confusion_matrix(preds: list[Prediction]) -> dict[int, dict[int, int]]:
    m: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for p in preds:
        if p.final_specific is not None:
            m[p.gold_specific][p.final_specific] += 1
    return {g: dict(row) for g, row in m.items()}


def confusing_pair_report(preds: list[Prediction]) -> list[dict[str, Any]]:
    """2/12、5/19、8/23 三个混淆对在搜索前后的改善。"""
    out = []
    for a, b in taxonomy.CONFUSING_PAIRS:
        subset = [p for p in preds if p.gold_specific in (a, b)]
        out.append(
            {
                "pair": f"{a}/{b}",
                "n": len(subset),
                "before": _acc([(p.gold_specific, p.initial_specific) for p in subset]),
                "after": _acc([(p.gold_specific, p.final_specific) for p in subset]),
            }
        )
    return out


def summarize(preds: list[Prediction]) -> dict[str, Any]:
    lat = [p.latency_ms for p in preds if p.latency_ms]
    return {
        "n": len(preds),
        "exact_match": exact_match(preds),
        "general_match": general_match(preds),
        "search_gain": search_gain(preds),
        "groundedness": groundedness(preds),
        "human_review_rate": human_review_rate(preds),
        "cache_hit_rate": cache_hit_rate(preds),
        "avg_latency_ms": sum(lat) / len(lat) if lat else 0,
        "total_cost_usd": sum(p.cost_usd for p in preds),
        "confusing_pairs": confusing_pair_report(preds),
    }
