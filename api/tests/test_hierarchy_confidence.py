"""层级置信度（叶子 vs 父类）单测 —— 粒度自适应的核心判定口径。

覆盖 9 组置信度组合 × 三条断言：
  ① apply_granularity_policy 的降级结果（leaf / parent）
  ② 降级后 candidate_codes 是否带上了混淆兄弟（下游搜索的锚点）
  ③ 该组合最终走哪条路由（decide_route_1）
"""

from __future__ import annotations

import pytest

from config import settings
from graph.edges import decide_route_1
from graph.state import Classification
from services import taxonomy
from services.vlm import apply_granularity_policy

D = settings.direct_threshold            # 0.85
G = settings.general_fallback_threshold  # 0.80


def clf(spec: float, gen: float, *, code: int | None = 2, ident: bool = True, **kw):
    return Classification(
        product_name=kw.pop("product_name", "Cereal 500g"),
        brand=kw.pop("brand", "BrandX"),
        name_brand_identifiable=ident,
        general_id=taxonomy.general_id_of(code) if code else 1,
        specific_code=code,
        leaf_vs_parent="leaf" if code is not None else "parent",
        specific_confidence=spec,
        general_confidence=gen,
        **kw,
    )


# (叶子置信, 父类置信, 期望层级, 期望路由, 说明)
CASES = [
    (0.95, 0.98, "leaf",   "direct", "双高 → 叶子直出，快路径"),
    (0.90, 0.60, "leaf",   "direct", "叶子高父类低 → 仍按叶子（叶子已定则父类由映射回填）"),
    (0.50, 0.95, "parent", "search", "叶子低父类高 → 粒度自适应降级到父类"),
    (0.50, 0.60, "leaf",   "search", "双低但有锚点 → 保持叶子，走取证"),
    (0.84, 0.99, "parent", "search", "叶子卡在阈值下沿 → 降级（边界）"),
    (0.85, 0.99, "leaf",   "direct", "叶子正好等于阈值 → 不降级（边界，闭区间）"),
    (0.40, 0.80, "parent", "search", "父类正好等于回落阈值 → 降级（边界，闭区间）"),
    (0.40, 0.79, "leaf",   "search", "父类差 0.01 → 不降级（边界）"),
    (0.30, 0.35, "leaf",   "human",  "双低且无锚点 → 直接人工"),
]


@pytest.mark.parametrize("spec,gen,level,route,desc", CASES)
def test_hierarchy_combinations(spec, gen, level, route, desc):
    ident = "无锚点" not in desc
    c = clf(spec, gen, ident=ident, brand=None if not ident else "BrandX",
            product_name=None if not ident else "Cereal 500g")
    out = apply_granularity_policy(c)
    assert out.leaf_vs_parent == level, desc
    assert decide_route_1({"initial": out}) == route, desc


def test_parent_level_clears_specific_code_and_keeps_candidates():
    """降级后：specific 置空、候选保留、reasoning 说明为什么待定。"""
    c = clf(0.5, 0.95, code=2)          # [2] 低糖谷物，与 [12] 构成混淆对
    out = apply_granularity_policy(c)
    assert out.specific_code is None
    assert out.leaf_vs_parent == "parent"
    assert 2 in out.candidate_codes and 12 in out.candidate_codes
    assert "粒度自适应" in out.reasoning
    assert out.general_category.startswith("1.")     # 父类仍然确定


def test_parent_level_never_goes_direct():
    """叶子未定就不能直出 —— 报告必须落到细类。"""
    parent = apply_granularity_policy(clf(0.5, 0.99, code=2))
    assert decide_route_1({"initial": parent}) != "direct"


def test_leaf_already_parent_is_not_re_downgraded():
    c = Classification(
        general_id=1, specific_code=None, candidate_codes=[2, 12],
        leaf_vs_parent="parent", specific_confidence=0.4, general_confidence=0.9,
        brand="B", product_name="P",
    )
    out = apply_granularity_policy(c)
    assert out.candidate_codes == [2, 12]     # 不重复追加
    assert out.reasoning == ""                # 已是 parent，不再追加说明


def test_invalid_specific_code_rejected():
    with pytest.raises(Exception):
        clf(0.9, 0.9, code=99)


def test_legacy_code_22_maps_to_32():
    c = Classification(
        general_id=9, specific_code=22, specific_confidence=0.9, general_confidence=0.9
    )
    assert c.specific_code == 32
