"""边逻辑单测：构造不同置信度，验证两个条件边的三路/两路分流。"""

from __future__ import annotations

import pytest

from config import settings
from graph.edges import decide_route_1, decide_route_2
from graph.state import Classification, Evidence


def clf(**kw) -> Classification:
    base = dict(
        general_category="1. Grains and starches",
        specific_code=2,
        specific_confidence=0.9,
        general_confidence=0.95,
        name_or_brand_legible=True,
        brand="X",
        product_name="Y",
    )
    return Classification(**{**base, **kw})


def test_route1_high_confidence_goes_direct():
    assert decide_route_1({"initial": clf(specific_confidence=0.95)}) == "direct"


def test_route1_low_confidence_with_anchor_goes_search():
    assert decide_route_1({"initial": clf(specific_confidence=0.5)}) == "search"


def test_route1_low_confidence_without_anchor_goes_human():
    c = clf(specific_confidence=0.5, name_or_brand_legible=False, brand=None, product_name=None)
    assert decide_route_1({"initial": c}) == "human"


def test_route1_no_initial_goes_human():
    assert decide_route_1({"initial": None}) == "human"


def test_route2_verified():
    st = {"revised": clf(specific_confidence=0.9), "evidence": [Evidence(source="web")]}
    assert decide_route_2(st) == "direct_verified"


def test_route2_low_confidence_goes_human():
    st = {"revised": clf(specific_confidence=0.4), "evidence": [Evidence(source="web")]}
    assert decide_route_2(st) == "human"


def test_route2_conflict_goes_human():
    st = {"revised": clf(specific_confidence=0.95, conflict=True), "evidence": [Evidence(source="web")]}
    assert decide_route_2(st) == "human"


def test_route2_no_evidence_goes_human():
    assert decide_route_2({"revised": clf(), "evidence": []}) == "human"


@pytest.mark.parametrize("code", [2, 12, 5, 19, 8, 23])
def test_invalid_code_rejected(code):
    clf(specific_code=code)  # 合法码不抛
    with pytest.raises(Exception):
        clf(specific_code=99)


def test_granularity_adaptive_display():
    """子类低置信 + 父类高置信 → 按父类粒度展示。"""
    c = clf(specific_confidence=settings.direct_threshold - 0.2, general_confidence=0.95)
    assert c.display_level == "general"
    assert clf(specific_confidence=0.95).display_level == "specific"
