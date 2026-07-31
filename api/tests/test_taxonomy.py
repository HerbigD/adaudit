"""taxonomy.json 加载、prompt 块 token 预算、级联选择器数据。"""

from __future__ import annotations

from config import settings
from services import taxonomy


def test_loads_33_specifics_12_generals():
    tx = taxonomy.load()
    assert len(tx.specifics) == 33
    assert len(tx.generals) == 12


def test_every_specific_has_valid_parent():
    tx = taxonomy.load()
    for code, s in tx.specifics.items():
        assert s.parent_id in tx.generals, code


# 基线随 taxonomy 版本与 prompt 语言策略漂移：
#   v0.9-draft（中英双写）          746
#   v1.0-codebook（中英双写）      1038  (+39%)
#   v1.0-codebook（Day5 改英文为主） 637  (-39%)
#   v1.1-annex4（Day6）             630  (-1%)   ← 当前
# 记下基线是为了让下一次改动"涨/跌了多少"一眼可见，而不是把它钉死。
#
# Day6：Annex 4 的数值判据（`thresholds_block()`）进了两份 system prompt，
# classify 1286→1648、adjudicate 871→1225。涨的这 ~360/~350 token 全是官方阈值原文，
# 是判 2/12、5/19、7/24、8/23 的必要输入。taxonomy_block 反而略降 ——
# 混淆对不再人工列，note 由阈值推导，比原来的中文注释短。
#
# 裁决①后又加了 Tier 2 的 8 对（线上默认 arm=B2）：classify 1648→1727、
# adjudicate 1225→1304，每对约 10 token。两者仍远在 2000 预算内。
TOKEN_BASELINE = {"taxonomy_block": 630, "classify_prompt": 1727, "adjudicate_prompt": 1304}
DRIFT_TOLERANCE = 0.25


def test_prompt_block_within_token_budget():
    """验收项：taxonomy prompt 块 token 数 ≤ 2000。"""
    report = taxonomy.token_report()
    budget = settings.taxonomy_prompt_token_budget
    assert report["taxonomy_block"] <= budget, report
    # 整段 system prompt（taxonomy + 规则 + 输出契约）也不该失控
    assert report["classify_prompt"] <= budget, report


def test_token_drift_against_recorded_baseline():
    """替换 taxonomy 数据后 token 会漂移；超过 25% 就该重新看一眼预算。"""
    report = taxonomy.token_report()
    for key, base in TOKEN_BASELINE.items():
        drift = abs(report[key] - base) / base
        assert drift <= DRIFT_TOLERANCE, (
            f"{key} 从基线 {base} 漂到 {report[key]}（{drift:.0%}）——"
            f"确认预算仍够用后更新 TOKEN_BASELINE"
        )


def test_prompt_block_contains_all_33_codes():
    block = taxonomy.taxonomy_block()
    for code in taxonomy.load().specifics:
        assert f"[{code}]" in block, code


def test_prompt_marks_evidence_dependent_categories():
    """`*` 标记 = 最终判定依赖营养数据，模型据此该给低置信而不是硬猜。"""
    block = taxonomy.taxonomy_block()
    for line in block.splitlines():
        line = line.rstrip()
        if not line.startswith((" *", "  [")):
            continue
        code = int(line[line.index("[") + 1 : line.index("]")])
        s = taxonomy.get(code)
        assert line.lstrip().startswith("*") == s.needs_evidence, code


def test_confusing_pairs_are_derived_not_hand_written():
    """A3 决议：混淆对由 Annex 4 阈值自动推导，taxonomy.json 里不再人工声明。"""
    import json
    from pathlib import Path

    raw = json.loads(Path(settings.taxonomy_path).read_text(encoding="utf-8"))
    assert "confusing_pairs" not in raw, "人工混淆对清单必须从数据源里删掉（A3 决议）"

    pairs = taxonomy.confusing_pairs()
    assert (2, 12) in pairs and (5, 19) in pairs and (8, 23) in pairs
    assert taxonomy.is_confusing_pair(12, 2)
    assert not taxonomy.is_confusing_pair(2, 5)
    # 每一对都必须带 source（裁决①：三档制，source 必填）
    assert all(
        taxonomy.pair_source(a, b)
        in ("definitional", "definitional_compositional", "dev_error_analysis")
        for a, b in pairs
    )


def test_pair_tiers_are_kept_separate():
    """裁决①：Tier 1 与 Tier 2 报指标时必须分开声明，所以先得分得开。

    Tier 1 = 共享数值切分线（2/12 的糖 20g 那类）
    Tier 2 = Annex 4 定义决定但判据是组成/形态（1/13 面条炸不炸那类）
    两者混谈会让"混淆性是定义的推论"这句话失去分辨力。
    """
    tiers = taxonomy.pairs_by_tier()
    assert set(tiers["definitional"]) == {(2, 12), (3, 18), (5, 19), (7, 24), (8, 23)}
    assert (1, 13) in tiers["definitional_compositional"]
    assert (11, 25) in tiers["definitional_compositional"]
    # Tier 2 不许混进 Tier 1
    assert not set(tiers["definitional"]) & set(tiers["definitional_compositional"])


def test_compositional_pair_pointing_at_a_missing_code_is_skipped_not_silent():
    """数据侧原清单含 (35,36)，但本 taxonomy 只到 34 —— 必须跳过并在数据源里留痕。"""
    import json
    from pathlib import Path

    raw = json.loads(Path(settings.taxonomy_path).read_text(encoding="utf-8"))
    assert raw["compositional_pairs"]["_dropped"]["pair"] == [35, 36]
    assert (35, 36) not in taxonomy.confusing_pairs()


def test_derivation_requires_a_shared_complementary_cut_off():
    """判据是"同一营养素、同一 basis、方向相反、切分点相同"，不是"同父类"。

    两个具体的回归点：
    - (7,24) 必须在：低脂咸味酱 <10g fat/100g vs 高脂咸味酱 >10g fat/100g，
      共用同一条线。它们分属父类 8 与 6，旧的"同父类"规则会漏掉。
    - (23,24) 必须不在：23 按 per serve 的饱和脂肪+钠判，24 按 per 100g 的总脂肪判，
      没有共享判定线。旧规则因为二者同属父类 6 且 key_dimensions 都是['脂肪','盐']
      而把它当成混淆对 —— 那是假对。
    """
    pairs = taxonomy.confusing_pairs()
    assert (7, 24) in pairs
    assert (3, 18) in pairs          # 果汁含量 98% 两侧
    assert (23, 24) not in pairs
    assert (8, 24) not in pairs


def test_pairs_arm_controls_what_reaches_the_prompt():
    """A3 四臂：A 空 / B 仅 Tier1 / B2 加 Tier2（线上默认）/ C 再加 Tier3。"""
    assert taxonomy.pairs_for_arm("A") == ()
    b, b2, c = (taxonomy.pairs_for_arm(x) for x in ("B", "B2", "C"))
    assert (2, 12) in b
    assert (1, 13) not in b and (1, 13) in b2          # Tier 2 不进 B 臂
    assert set(b) < set(b2) <= set(c)                  # 逐层包含
    assert "[2]vs[12]" not in taxonomy.confusing_pairs_block("A")
    assert "[2]vs[12]" in taxonomy.confusing_pairs_block("B")
    # 未知 arm 按 B2（线上默认）处理：宁可多给定义级先验，也绝不泄漏经验对
    assert taxonomy.pairs_for_arm("X") == b2
    assert all(taxonomy.pair_source(*p) != "dev_error_analysis" for p in b2)


def test_empirical_pair_is_tagged_and_only_visible_in_arm_c():
    tx = taxonomy.load()
    original = tx.confusing_pairs
    try:
        taxonomy.register_empirical_pair(19, 25, "Parle Smooth：牛奶 vs 含糖饮料")
        assert taxonomy.pair_source(19, 25) == "dev_error_analysis"
        assert (19, 25) not in taxonomy.pairs_for_arm("B")
        assert (19, 25) in taxonomy.pairs_for_arm("C")
    finally:
        object.__setattr__(tx, "confusing_pairs", original)
        taxonomy.PAIR_SOURCE.pop((19, 25), None)


def test_prompt_block_is_english_only():
    """Day5 §9 软约定 1：模型侧一律英文，中文名只留在 UI 展示层。"""
    import re

    block = taxonomy.taxonomy_block()
    cjk = re.findall(r"[一-鿿]", block)
    assert not cjk, f"prompt 文本块混入中文字符: {set(cjk)}"
    # 但级联选择器（UI 层）必须还有中文
    assert any(s["name_zh"] for s in taxonomy.cascade()["specifics"])


def test_every_confusing_pair_has_decision_dimensions():
    """每对都要有判定维度，否则 Day5 §6 的冲突判定对它形同虚设。

    两处豁免，都是"天生没有数值维度"而不是漏写：
    - `PAIRS_WITHOUT_NUTRIENT_DIM`：果汁含量在配料表，不在营养表
    - Tier 2 definitional_compositional：判据本来就是组成/形态，不是数字
    """
    for pair in taxonomy.confusing_pairs():
        if pair in taxonomy.PAIRS_WITHOUT_NUTRIENT_DIM:
            continue
        if taxonomy.pair_source(*pair) == "definitional_compositional":
            continue
        assert taxonomy.pair_dimensions(*pair), pair


def test_pair_nutrients_use_annex4_field_names():
    """回归点：8/23 的判据是 **saturated fat**，不是总脂肪。

    旧的手抄表这里写的是 ("fat","sodium")，于是冲突判定盯着一个 Annex 4
    根本没用到的维度看 —— 表面有覆盖，实际静默失效。现在维度由阈值自动推出。
    """
    assert taxonomy.pair_nutrients([2, 12]) == ("sugar", "fiber")
    assert taxonomy.pair_nutrients([5, 19]) == ("fat",)
    assert taxonomy.pair_nutrients([8, 23]) == ("saturated_fat", "sodium")
    assert taxonomy.pair_nutrients([7, 24]) == ("fat",)
    assert taxonomy.pair_nutrients([2]) == ()          # 不成对 → 不做冲突判定
    assert taxonomy.pair_nutrients(None) == ()


def test_all_categories_confirmed():
    """v1.0-codebook 起 33 条应全部 confirmed=true。"""
    assert taxonomy.cascade()["confirmed_ratio"] == 1.0
    assert taxonomy.load().version == "1.1-annex4"


def test_cascade_shape():
    data = taxonomy.cascade()
    assert len(data["generals"]) == 12
    assert len(data["specifics"]) == 33
    assert all("parent_id" in s and "name_zh" in s for s in data["specifics"])
    # 名称仍为草案时 confirmed_ratio 应能如实反映，供 /api/health 暴露
    assert 0.0 <= data["confirmed_ratio"] <= 1.0


def test_normalize_handles_legacy_and_garbage():
    assert taxonomy.normalize(22) == 32
    assert taxonomy.normalize("12") == 12
    assert taxonomy.normalize("[12]") == 12
    assert taxonomy.normalize(999) is None
    assert taxonomy.normalize(None) is None


def test_hfss_verdicts_cover_every_code():
    """判定表必须覆盖全部 33 类 —— 新增类别时强制做一次政策判断，不许静默漏掉。"""
    tx = taxonomy.load()
    assert set(tx.specifics) <= set(taxonomy.HFSS_VERDICTS)
    assert all(why for _, why in taxonomy.HFSS_VERDICTS.values()), "每条判定都要写依据"


def test_hfss_set_matches_codebook_semantics():
    codes = taxonomy.hfss_codes()
    # 应计入
    for c in (12, 13, 14, 15, 16, 17, 19, 20, 21, 23, 24, 25, 32):
        assert c in codes, c
    # 不应计入（曾被名称正则误命中的三条重点盯住）
    assert 7 not in codes         # 高不饱和脂肪油脂，"咸味"曾误命中
    assert 29 not in codes        # 茶咖，名称里的"甜味"是否定语义，曾误命中
    assert 11 not in codes        # 瓶装水
    assert 9 not in codes         # 核心食物健康零食
    assert 31 not in codes        # 快餐健康选项
    assert 1 not in codes


def test_alcohol_tracked_separately_from_hfss():
    """酒精受广告监管但不属于高糖/高脂/高盐口径，分开统计。"""
    assert 26 in taxonomy.alcohol_codes()
    assert 26 not in taxonomy.hfss_codes()


def test_hfss_table_is_reviewable():
    table = taxonomy.hfss_table()
    assert len(table) >= 33
    row = next(r for r in table if r["code"] == 13)
    assert row["hfss"] is True and "油炸" in row["rationale"]
