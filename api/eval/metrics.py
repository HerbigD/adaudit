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
    # 结果产出方（mock-vlm / rule-fallback / gemini / …）—— runner 的断言位靠它
    adapters: tuple[str, ...] = ()
    leaf_vs_parent: str = "leaf"
    # Day5 §9 / Day6 D3：按语言、国家切片看准确率是"泛化设计"叙事的数据基础。
    # 这两个字段来自模型对广告的判读（`Classification.ad_language` / `country`）。
    language: str = "en"
    country: str | None = None
    search_status: str | None = None
    # 金标侧的语言/国家（来自切分 csv）。与上面两个字段**分开存**：
    # 模型判错语言本身就是一类错误，用模型自己的判读去切片会把这类错误藏起来。
    gold_language: str | None = None
    gold_country: str | None = None
    split: str | None = None            # dev / eval / smoke，指标里必须标出来
    pairs_arm: str | None = None        # A3 消融臂
    trace: list[dict] = field(default_factory=list)

    @property
    def is_mock(self) -> bool:
        return any(a.startswith("mock") or a == "rule-fallback" for a in self.adapters)


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
    """混淆对在搜索前后的改善。对来自 `_derive_pairs` 自动推导，带 `source` 一起报。"""
    out = []
    for a, b in taxonomy.confusing_pairs():
        subset = [p for p in preds if p.gold_specific in (a, b)]
        out.append(
            {
                "pair": f"{a}/{b}",
                # A3：来源必须跟着指标走。dev_error_analysis 的对若出现在 held-out
                # 指标里，读表的人有权知道那份先验是从标注数据来的。
                "source": taxonomy.pair_source(a, b),
                "n": len(subset),
                "before": _acc([(p.gold_specific, p.initial_specific) for p in subset]),
                "after": _acc([(p.gold_specific, p.final_specific) for p in subset]),
            }
        )
    return out


def confusing_pair_report_by_tier(preds: list[Prediction]) -> dict[str, list[dict[str, Any]]]:
    """按 source 分组的混淆对报告（裁决①）。

    Tier 1（共享数值切分线）与 Tier 2（组成/形态判据）混在一张表里，
    读表的人会默认它们是同一种东西 —— 而"混淆性是定义的推论"这句话
    在两档上的强度并不一样。分开报，让强度差别可见。
    """
    rows = confusing_pair_report(preds)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["source"], []).append(r)
    return out


# --------------------------------------------------------------------------- #
# D3 · 按语言 / 国家切片（Day6 批准）
# --------------------------------------------------------------------------- #
# 泛化叙事的数据基础：整体 exact_match 是四国的加权平均，印度占大头，
# 只看总数会把"孟加拉语上明显更差"这件事平均掉。
#
# `n` 必须和准确率一起报：某一层只有 5 条时，那个 0.80 是 4/5，不是稳定估计。
# 所以每层带 `n` 和 `small_sample` 标记，而不是把判断留给读表的人。
#
# ## 裁决②（人类 07-31）：切片一律**描述性**，不做跨组对比结论
#
# 真实池分布是 Sri Lanka 74.2% / Bangladesh 10.0% / India 8.5% / Pakistan 7.3%，
# 300 条 eval 里 India≈25、Pakistan≈22。在这个量级上比较四国准确率，
# 差异几乎全部落在抽样噪声里 —— 报出来只会被当成"模型在印度更差"这种因果读法。
#
# 所以这里**不输出任何 best/worst/gap**。四国样本不均衡反映的是广告投放现实，
# 不是实验设计缺陷；小国切片仅供描述，跨国对比留作未来工作。
SMALL_SAMPLE_N = 30

SLICE_LIMITATION = (
    "切片指标为描述性，不构成跨组对比结论。四国样本量不均衡"
    "（Sri Lanka 约 74%，其余三国合计约 26%）反映的是广告投放现实；"
    "India / Pakistan 层 n 在 20–30 量级，仅供描述，跨国显著性检验留作未来工作。"
)


def _slice(preds: list[Prediction], key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Prediction]] = defaultdict(list)
    for p in preds:
        groups[str(key(p) or "unknown")].append(p)
    return {
        name: {
            "n": len(sub),
            "exact_match": exact_match(sub),
            "general_match": general_match(sub),
            "human_review_rate": human_review_rate(sub),
            # 不叫 `reliable`：那个词会被读成"这一层的数可以拿去比"，
            # 而裁决②的口径是任何一层都不拿去比。这里只陈述样本量是否偏小。
            "small_sample": len(sub) < SMALL_SAMPLE_N,
        }
        for name, sub in sorted(groups.items())
    }


def by_language(preds: list[Prediction]) -> dict[str, dict[str, Any]]:
    """优先按**金标语言**切片；金标没有语言列时退回模型判读的语言。

    退回是有代价的：用模型自己判的语言分层，等于让模型给自己划考区，
    它判错语言的那些样本会被归到错误的层里。所以 summarize 会标出用的是哪个。
    """
    return _slice(preds, lambda p: p.gold_language or p.language)


def by_country(preds: list[Prediction]) -> dict[str, dict[str, Any]]:
    return _slice(preds, lambda p: p.gold_country or p.country)


def by_search_status(preds: list[Prediction]) -> dict[str, dict[str, Any]]:
    """按取证结局切片：`degraded` / `no_result` 层的准确率就是取证失败的代价。"""
    return _slice(preds, lambda p: p.search_status)


def parent_level_share(preds: list[Prediction]) -> float:
    """粒度自适应触发率：最终只定到父类的比例。"""
    if not preds:
        return 0.0
    return sum(1 for p in preds if p.leaf_vs_parent == "parent") / len(preds)


def summarize(preds: list[Prediction]) -> dict[str, Any]:
    lat = [p.latency_ms for p in preds if p.latency_ms]
    adapters = sorted({a for p in preds for a in p.adapters})
    from collections import Counter

    splits = sorted({p.split for p in preds if p.split})
    arms = sorted({p.pairs_arm for p in preds if p.pairs_arm})
    return {
        "n": len(preds),
        "adapters": adapters,
        "contains_mock": any(p.is_mock for p in preds),
        # 这三条是"这份数字是在什么条件下得到的"的最短说明，必须跟着指标走
        "split": splits[0] if len(splits) == 1 else splits,
        "pairs_arm": arms[0] if len(arms) == 1 else arms,
        "slice_key": (
            "gold" if any(p.gold_language or p.gold_country for p in preds) else "model_predicted"
        ),
        "parent_level_share": parent_level_share(preds),
        "language_distribution": dict(Counter(p.language for p in preds)),
        "country_distribution": dict(Counter(p.country or "unknown" for p in preds)),
        "search_status_distribution": dict(
            Counter(p.search_status or "none" for p in preds)
        ),
        # D3：切片准确率。**只给分层数字，不给层间对比**（裁决②）
        "by_language": by_language(preds),
        "by_country": by_country(preds),
        "by_search_status": by_search_status(preds),
        "slice_interpretation": "descriptive_only",
        "slice_limitation": SLICE_LIMITATION,
        "exact_match": exact_match(preds),
        "general_match": general_match(preds),
        "search_gain": search_gain(preds),
        "groundedness": groundedness(preds),
        "human_review_rate": human_review_rate(preds),
        "cache_hit_rate": cache_hit_rate(preds),
        "avg_latency_ms": sum(lat) / len(lat) if lat else 0,
        "total_cost_usd": sum(p.cost_usd for p in preds),
        # 裁决①：Tier 1 与 Tier 2 分开声明，不混成一张表
        "confusing_pairs_by_tier": confusing_pair_report_by_tier(preds),
    }
