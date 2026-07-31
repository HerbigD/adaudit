"""D3 切片指标 与 A3 三臂消融的汇总逻辑。

这里不跑图（跑图要真实 provider），只测"拿到预测之后怎么算、怎么标注"——
而这恰恰是最容易出错、也最容易把错误藏进论文表格的一层。
"""

from __future__ import annotations

from eval import ablation, metrics


def _p(gold, final, *, lang="en", country="IN", conf=0.9, gold_lang=None, gold_country=None):
    return metrics.Prediction(
        audit_id=f"x{gold}{final}{lang}{country}{conf}",
        gold_specific=gold,
        initial_specific=final,
        final_specific=final,
        initial_confidence=conf,
        final_confidence=conf,
        route_1="search",
        route_2="direct_verified",
        used_evidence=True,
        cache_hit=False,
        language=lang,
        country=country,
        gold_language=gold_lang,
        gold_country=gold_country,
    )


# --------------------------------------------------------------------------- #
# D3 · 切片
# --------------------------------------------------------------------------- #
def test_small_slices_are_flagged_not_dropped():
    """5 条样本上的 0.80 是 4/5，不是一个能写进表格的数。

    标 `small_sample` 而不是把这层丢掉：丢掉会让读表的人以为该语言没有数据，
    实际上是"有数据但太少"，这两件事对下一步该做什么的指向完全不同。
    """
    preds = [_p(2, 2, lang="bn") for _ in range(5)] + [
        _p(2, 2 if i % 4 else 12, lang="en") for i in range(40)
    ]
    by_lang = metrics.by_language(preds)
    assert by_lang["bn"]["n"] == 5 and by_lang["bn"]["small_sample"] is True
    assert by_lang["en"]["n"] == 40 and by_lang["en"]["small_sample"] is False


def test_summary_never_emits_a_cross_slice_comparison():
    """裁决②：切片只描述，不对比。

    即使造一个"en 100% / bn 50%"的极端场景，summary 里也不许出现
    best / worst / gap 这类字段 —— 它们会被直接读成"模型在孟加拉语上更差"，
    而在 n≈25 的层上那个差值几乎全是抽样噪声。
    """
    preds = (
        [_p(2, 2, lang="en") for _ in range(30)]
        + [_p(2, 2 if i % 2 else 12, lang="bn") for i in range(30)]
    )
    s = metrics.summarize(preds)
    assert s["slice_interpretation"] == "descriptive_only"
    assert "未来工作" in s["slice_limitation"]
    banned = {"gap", "best", "worst", "language_gap", "country_gap"}
    assert not (banned & set(s)), f"summary 里出现了跨组对比字段: {banned & set(s)}"
    assert not hasattr(metrics, "slice_gap"), "slice_gap 应已删除（裁决②）"


def test_confusing_pairs_are_reported_per_tier():
    """裁决①：Tier 1 与 Tier 2 分开报，不混成一张表。"""
    preds = [_p(2, 2), _p(12, 2), _p(1, 13)]
    s = metrics.summarize(preds)
    by_tier = s["confusing_pairs_by_tier"]
    assert "definitional" in by_tier and "definitional_compositional" in by_tier
    assert {r["pair"] for r in by_tier["definitional"]} >= {"2/12", "5/19", "8/23"}
    assert "1/13" in {r["pair"] for r in by_tier["definitional_compositional"]}
    assert "confusing_pairs" not in s


def test_slices_prefer_gold_language_over_model_predicted():
    """用模型自己判的语言分层，等于让模型给自己划考区。

    下面这条样本模型把孟加拉语广告判成了英语。按模型判读切片，它会被算进 en 层，
    en 的准确率被这条错误拉低、bn 层则凭空少了一条 —— 两个层同时失真。
    """
    preds = [_p(2, 12, lang="en", gold_lang="bn")]
    assert "bn" in metrics.by_language(preds)
    assert "en" not in metrics.by_language(preds)
    assert metrics.summarize(preds)["slice_key"] == "gold"

    # 金标没有语言列时退回模型判读，并在 summary 里如实标出来
    preds2 = [_p(2, 12, lang="en")]
    assert "en" in metrics.by_language(preds2)
    assert metrics.summarize(preds2)["slice_key"] == "model_predicted"


def test_summary_carries_split_and_arm():
    """任何一份指标都必须答得出"这是在哪份切分、哪个 prompt 条件下跑的"。"""
    preds = [_p(2, 2)]
    preds[0].split, preds[0].pairs_arm = "eval", "B"
    s = metrics.summarize(preds)
    assert s["split"] == "eval" and s["pairs_arm"] == "B"


# --------------------------------------------------------------------------- #
# A3 · 消融汇总
# --------------------------------------------------------------------------- #
def _arm(acc_conf, acc_ctrl, low_share=0.3):
    """造一臂：混淆组准确率 acc_conf、对照组 acc_ctrl。"""
    def group(acc, n=40):
        hit = int(acc * n)
        return [_p(2, 2, conf=0.5 if i < low_share * n else 0.95) for i in range(hit)] + [
            _p(2, 12, conf=0.5 if i < low_share * n else 0.95) for i in range(n - hit)
        ]
    return {"confusing": group(acc_conf), "control": group(acc_ctrl)}


def test_difference_in_differences_strips_out_a_global_shift():
    """核心方法学点：混淆组涨了不代表提示有效。

    这里造的是一个"两组同涨 10pp"的场景 —— 提示只是让模型整体更保守，
    在非混淆样本上也照样改了答案。delta_confusing 会显示 +0.10 看着很像有效，
    但差分是 0：提示没有带来任何**判别力**。
    """
    results = {"A": _arm(0.60, 0.70), "B": _arm(0.70, 0.80)}
    out = ablation.summarize(results)
    c = next(x for x in out["contrasts"] if x["contrast"] == "B − A")
    assert c["delta_confusing"]["exact_match"] == 0.10
    assert c["difference_in_differences"] == 0.0


def test_difference_in_differences_detects_real_discrimination():
    results = {"A": _arm(0.60, 0.70), "B": _arm(0.75, 0.70)}
    out = ablation.summarize(results)
    c = next(x for x in out["contrasts"] if x["contrast"] == "B − A")
    assert c["difference_in_differences"] == 0.15


def test_summary_always_carries_the_sample_size_caveat():
    """90 条上的 3pp 不是发现。这句话必须跟着结果走，而不是靠人记得。"""
    out = ablation.summarize({"A": _arm(0.6, 0.7), "B": _arm(0.7, 0.7)})
    assert any("不可解读" in c for c in out["caveats"])
    assert any("held-out" in c for c in out["caveats"])


def test_arm_stats_expose_the_confidence_signal_itself():
    """消融要回答的是置信度信号，不只是准确率 —— 低置信占比和均值都要出。"""
    stats = ablation._arm_stats(_arm(0.6, 0.6)["confusing"])
    assert "low_confidence_share" in stats and "mean_initial_confidence" in stats
    assert 0 < stats["low_confidence_share"] < 1
