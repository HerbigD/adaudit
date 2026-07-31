"""Annex 4 判定引擎（`services/nutrient_rules.py`）与裁决节点的接线。

这些用例逐条对应 Annex 4 原文的数值，改动阈值必然打红 —— 这是有意的：
阈值是权威来源，任何变更都该是一次显式决策，不是顺手改。
"""

from __future__ import annotations

import pytest

from graph.state import Classification, Evidence
from services import nutrient_rules, taxonomy


def _ev(*, serving: float | None = None, source="official", **nutrients) -> Evidence:
    """构造一条证据。传 `serving` 就同时填 per_serve —— 走真实的 nutrition 构造路径。"""
    from services import nutrition

    vals = []
    for name, (value, unit) in nutrients.items():
        vals.append(
            nutrition._nutrient_value(name, value, unit, serving_size_g=serving)
        )
    return Evidence(id="ev_001", source_type=source, nutrients=vals)


# --------------------------------------------------------------------------- #
# 2 / 12 · 早餐谷物 · per 100g · <20g 糖 AND >5g 纤维
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sugar,fibre,expect",
    [
        (8.0, 9.0, 2),      # 双达标
        (25.0, 9.0, 12),    # 糖超
        (8.0, 2.0, 12),     # 纤维不足
        (25.0, 2.0, 12),    # 双不达标
    ],
)
def test_cereals_follow_annex4(sugar, fibre, expect):
    v = nutrient_rules.decide([2, 12], [_ev(sugar=(sugar, "g/100g"), fiber=(fibre, "g/100g"))])
    assert v.code == expect and v.ok, v.reason


@pytest.mark.parametrize("sugar,fibre", [(20.0, 9.0), (8.0, 5.0), (20.0, 5.0)])
def test_cereals_boundary_goes_to_the_unhealthy_class(sugar, fibre):
    """Annex 4 的 2 是 `<20 且 >5`、12 是 `>20 或 <5` —— 恰好等于 20 或 5 时**两边都不成立**。

    这是原文留下的定义缝隙，不是我们读错。本项目补充规则：边界值归非健康类（12），
    对 HFSS 监管口径保守。判定理由里必须把"这是本项目补充规则"说出来，
    否则读结果的人会以为 Annex 4 就是这么写的。
    """
    v = nutrient_rules.decide([2, 12], [_ev(sugar=(sugar, "g/100g"), fiber=(fibre, "g/100g"))])
    assert v.code == 12
    assert "本项目补充规则" in v.reason


def test_cereals_missing_nutrient_is_uncertain_not_a_guess():
    v = nutrient_rules.decide([2, 12], [_ev(sugar=(8.0, "g/100g"))])
    assert v.uncertain and v.code is None
    assert "fiber" in v.missing


# --------------------------------------------------------------------------- #
# 5 / 19 · 乳品 · per 100g · 奶 3g、奶酪 15g 两条不同的线
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fat,cheese,expect", [
    (1.5, False, 5), (3.0, False, 5), (3.1, False, 19),
    (12.0, True, 5), (15.0, True, 5), (20.0, True, 19),
])
def test_dairy_uses_a_different_cut_off_for_cheese(fat, cheese, expect):
    v = nutrient_rules.decide([5, 19], [_ev(fat=(fat, "g/100g"))], is_cheese=cheese)
    assert v.code == expect, v.reason


def test_full_cream_milk_is_19_the_parle_case():
    """用户给的 Parle Smooth 例子：全脂奶 >3g/100g → 19，不是 5。"""
    v = nutrient_rules.decide([5, 19], [_ev(fat=(3.5, "g/100g"))])
    assert v.code == 19


# --------------------------------------------------------------------------- #
# 7 / 24 · 咸味酱 · per 100g · 10g 脂肪 —— **阈值只管酱类**
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fat,expect", [(4.0, 7), (9.9, 7), (10.0, 24), (18.0, 24)])
def test_savoury_sauces(fat, expect):
    """恰好 10g 归 24：7 是 `<10`、24 是 `>10`，等于 10 时两边都不成立。"""
    v = nutrient_rules.decide([7, 24], [_ev(fat=(fat, "g/100g"))], is_sauce=True)
    assert v.code == expect, v.reason


def test_the_10g_line_must_not_be_applied_to_cooking_oils():
    """Annex 4 原文：\"Oils ..., **and** low fat savoury sauces (<10g fat /100g)\"。

    `<10g/100g` 只修饰 savoury sauces 那一支。橄榄油脂肪 ~100g/100g ——
    把这条线通用化会把**所有植物油**判进 24，而 Annex 4 明确把植物油放在 7。
    油脂进 7、黄油/动物脂肪进 24 是**定义之分，不是阈值之分**。

    这是转录核对抓到的一处硬伤：数值抄对了，适用范围抄丢了。
    """
    oil = [_ev(fat=(91.0, "g/100g"))]
    v = nutrient_rules.decide([7, 24], oil, is_sauce=False)
    assert v.uncertain and v.code is None
    assert "定义之分" in v.reason

    # 形态判不出时也不许套阈值 —— 默认成"是酱"就等于没修
    assert nutrient_rules.decide([7, 24], oil, is_sauce=None).uncertain


def test_sauce_form_never_defaults_to_true():
    assert nutrient_rules.sauce_form("Tomato Ketchup 500g") is True
    assert nutrient_rules.sauce_form("Extra Virgin Olive Oil 1L") is False
    assert nutrient_rules.sauce_form("Amul Butter 100g") is False
    assert nutrient_rules.sauce_form("Fortune Sunflower Oil") is False
    assert nutrient_rules.sauce_form("Mystery Product 200g") is None


def test_soup_uses_the_2g_line_and_boundary_goes_to_24():
    """Annex 4：8 含 soups (<2g fat/100g)、24 含 soups (>2g fat/100g)。同样有边界缝隙。"""
    assert nutrient_rules.decide([8, 23], [_ev(fat=(1.2, "g/100g"))], is_soup=True).code == 8
    assert nutrient_rules.decide([8, 23], [_ev(fat=(2.0, "g/100g"))], is_soup=True).code == 24
    assert nutrient_rules.decide([8, 24], [_ev(fat=(5.0, "g/100g"))], is_soup=True).code == 24


def test_soup_is_intercepted_before_the_per_serve_meal_rule():
    """汤按 per-100g 的脂肪判，冷冻餐食按 per-serve 的饱和脂肪+钠判 —— 两条不同的线。

    汤如果落进 8/23 的餐食分支，会因为"份量不明"被判 uncertain，
    而它本来有一条完全可用的 per-100g 判据。
    """
    ev = _ev(fat=(1.2, "g/100g"))          # 没有份量、没有饱和脂肪
    assert nutrient_rules.decide([8, 23], [ev], is_soup=True).code == 8
    assert nutrient_rules.decide([8, 23], [ev], is_soup=False).uncertain


def test_scope_warning_lives_in_the_data_source():
    """适用范围的约束必须留在 taxonomy.json 里可查，不能只活在代码注释。"""
    th7 = taxonomy.load().specifics[7].thresholds
    assert th7["threshold_scope"] == "savoury_sauces_only"
    assert "olive oil" in th7["scope_warning"] or "橄榄油" in th7["scope_warning"]
    assert taxonomy.load().specifics[24].thresholds["threshold_scope"]


# --------------------------------------------------------------------------- #
# 8 / 23 · 餐食 · **per serve** · 饱和脂肪 6g、钠 900mg
# --------------------------------------------------------------------------- #
def test_meals_are_judged_per_serve():
    ev = _ev(serving=350.0, saturated_fat=(1.0, "g/100g"), sodium=(200.0, "mg/100g"))
    # 每份：饱和脂肪 3.5g（≤6）、钠 700mg（≤900）→ 8
    v = nutrient_rules.decide([8, 23], [ev])
    assert v.code == 8, v.reason

    ev2 = _ev(serving=350.0, saturated_fat=(2.5, "g/100g"), sodium=(300.0, "mg/100g"))
    # 每份：饱和脂肪 8.75g（>6）→ 23
    assert nutrient_rules.decide([8, 23], [ev2]).code == 23


def test_meals_refuse_to_substitute_per_100g_when_serving_is_unknown():
    """份量不明就必须转人工 —— 拿 per-100g 去比 "900mg /serve" 是在比两个不同分母的数。

    这是整套规则里最容易被"顺手兜底"破坏的一条：随手用 per-100g 顶替，
    链路会一路绿灯跑完，产出的却是无意义的判定。
    """
    ev = _ev(saturated_fat=(2.5, "g/100g"), sodium=(300.0, "mg/100g"))
    v = nutrient_rules.decide([8, 23], [ev])
    assert v.uncertain and v.code is None
    assert "per-100g" in v.reason


def test_meals_or_reading_is_flagged_as_a_project_supplement():
    """Annex 4 的 23 用逗号并列两个条件、没写 and/or；本项目读作 OR。

    这个读法必须留在数据源里可查，不能只活在代码注释里。
    """
    th = taxonomy.load().specifics[23].thresholds
    assert "project_note" in th and "Annex 4 原文" in th["project_note"]
    # 只有钠超标（脂肪达标）也应判 23 —— 这正是 OR 读法的可观测后果
    ev = _ev(serving=300.0, saturated_fat=(1.0, "g/100g"), sodium=(400.0, "mg/100g"))
    assert nutrient_rules.decide([8, 23], [ev]).code == 23


# --------------------------------------------------------------------------- #
# 9 · 健康零食 · per serve · 三条全过
# --------------------------------------------------------------------------- #
def test_healthy_snack_needs_all_three_thresholds():
    ok = _ev(serving=30.0, energy_kj=(1500.0, "kJ/100g"),
             saturated_fat=(2.0, "g/100g"), sodium=(200.0, "mg/100g"))
    # 每份：450kJ(<600)、0.6g 饱和脂肪(<3)、60mg 钠(<200) → 9
    assert nutrient_rules.decide([9], [ok]).code == 9

    bad = _ev(serving=30.0, energy_kj=(2500.0, "kJ/100g"),
              saturated_fat=(2.0, "g/100g"), sodium=(200.0, "mg/100g"))
    # 每份 750kJ 超线 → 不是健康零食，但落 16/17/21 要看品类形态 → 转人工
    v = nutrient_rules.decide([9], [bad])
    assert v.uncertain and v.code is None


# --------------------------------------------------------------------------- #
# 3 / 18 · 果汁 —— 判据不在营养表里
# --------------------------------------------------------------------------- #
def test_juice_is_always_uncertain_because_fruit_pct_is_not_a_nutrient():
    v = nutrient_rules.decide([3, 18], [_ev(sugar=(9.0, "g/100ml"))])
    assert v.uncertain and "fruit_pct" in v.missing
    assert (3, 18) in taxonomy.PAIRS_WITHOUT_NUTRIENT_DIM


# --------------------------------------------------------------------------- #
# 钠空间（B3 决议）
# --------------------------------------------------------------------------- #
def test_sodium_is_compared_in_sodium_space_not_converted_to_salt():
    """Annex 4 的判据全部用 sodium。旧代码 `sodium × 2.5 → salt` 已删除。

    换算只有一个合法方向：标签只给盐时 `salt_g × 400 = sodium_mg`。
    """
    ev = _ev(serving=100.0, sodium=(95.0, "mg/100g"))
    assert nutrient_rules.sodium_mg_per_100g([ev]) == pytest.approx(95.0)
    assert nutrient_rules.sodium_mg_per_serve([ev]) == pytest.approx(95.0)
    assert nutrient_rules.SALT_G_TO_SODIUM_MG == 400.0

    import inspect

    from graph.nodes import adjudicate_with_evidence as node

    src = inspect.getsource(node)
    assert "2.5" not in src, "钠→盐 ×2.5 换算不该再出现在裁决节点里"


# --------------------------------------------------------------------------- #
# 裁决节点接线
# --------------------------------------------------------------------------- #
def _initial(**kw) -> Classification:
    base = dict(
        product_name="Test Cereal 500g", brand="TestBrand", general_id=1,
        candidate_codes=[2, 12], leaf_vs_parent="parent",
        specific_confidence=0.55, general_confidence=0.88,
    )
    base.update(kw)
    return Classification(**base)


def test_rule_fallback_now_uses_annex4_and_still_tags_itself_as_fallback():
    from graph.nodes.adjudicate_with_evidence import _rule_based

    ev = [_ev(sugar=(25.0, "g/100g"), fiber=(2.0, "g/100g"))]
    out = _rule_based(_initial(), ev, conflict=False)
    assert out.specific_code == 12
    assert "Annex 4" in out.reasoning
    # 换了权威阈值 ≠ 换成了真实模型裁决：eval 的双闸照旧要拦它
    assert out.adapter == "rule-fallback"


def test_rule_fallback_returns_parent_when_annex4_cannot_decide():
    """判不了就交出去，**不许**回落到 pool[0]。

    随手挑一个候选会把"判不了"伪装成"判出来了"，而且伪装得毫无痕迹 ——
    下游看到的是一个正常的叶子编号，没人会再去问它是怎么来的。
    """
    from graph.nodes.adjudicate_with_evidence import _rule_based

    ev = [_ev(sugar=(25.0, "g/100g"))]              # 缺纤维
    out = _rule_based(_initial(), ev, conflict=False)
    assert out.specific_code is None
    assert out.leaf_vs_parent == "parent"
    assert out.specific_confidence < 0.5
    assert "转人工" in out.reasoning


def test_cheese_cut_off_is_picked_up_from_the_product_name():
    from graph.nodes.adjudicate_with_evidence import _rule_based

    ev = [_ev(fat=(12.0, "g/100g"))]
    milk = _rule_based(
        _initial(product_name="Full Cream Milk 1L", general_id=3, candidate_codes=[5, 19]),
        ev, conflict=False,
    )
    cheese = _rule_based(
        _initial(product_name="Mozzarella Cheese 200g", general_id=3, candidate_codes=[5, 19]),
        ev, conflict=False,
    )
    assert milk.specific_code == 19        # 12g > 3g 线
    assert cheese.specific_code == 5       # 12g ≤ 15g 线
