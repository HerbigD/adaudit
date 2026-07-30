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
#   v1.0-codebook（Day5 改英文为主） 637  (-39%)  ← 当前
# 记下基线是为了让下一次改动"涨/跌了多少"一眼可见，而不是把它钉死。
TOKEN_BASELINE = {"taxonomy_block": 637, "classify_prompt": 1286, "adjudicate_prompt": 871}
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


def test_confusing_pairs_loaded_from_json():
    pairs = taxonomy.confusing_pairs()
    assert (2, 12) in pairs and (5, 19) in pairs and (8, 23) in pairs
    assert taxonomy.is_confusing_pair(12, 2)
    assert not taxonomy.is_confusing_pair(2, 5)


def test_prompt_block_is_english_only():
    """Day5 §9 软约定 1：模型侧一律英文，中文名只留在 UI 展示层。"""
    import re

    block = taxonomy.taxonomy_block()
    cjk = re.findall(r"[一-鿿]", block)
    assert not cjk, f"prompt 文本块混入中文字符: {set(cjk)}"
    # 但级联选择器（UI 层）必须还有中文
    assert any(s["name_zh"] for s in taxonomy.cascade()["specifics"])


def test_pair_nutrients_cover_every_confusing_pair():
    for pair in taxonomy.confusing_pairs():
        assert pair in taxonomy.PAIR_NUTRIENTS, pair
    assert taxonomy.pair_nutrients([2, 12]) == ("sugar", "fiber")
    assert taxonomy.pair_nutrients([5, 19]) == ("fat",)
    assert taxonomy.pair_nutrients([8, 23]) == ("fat", "sodium")
    assert taxonomy.pair_nutrients([16, 17]) == ("sugar", "sodium")
    assert taxonomy.pair_nutrients([2]) == ()          # 不成对 → 不做冲突判定
    assert taxonomy.pair_nutrients(None) == ()


def test_all_categories_confirmed():
    """v1.0-codebook 起 33 条应全部 confirmed=true。"""
    assert taxonomy.cascade()["confirmed_ratio"] == 1.0
    assert taxonomy.load().version == "1.0-codebook"


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
