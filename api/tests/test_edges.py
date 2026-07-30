"""边逻辑单测：构造不同置信度，验证两个条件边的三路/两路分流。"""

from __future__ import annotations

import pytest

from graph.edges import decide_route_1, decide_route_2
from graph.state import Classification, Evidence


def clf(**kw) -> Classification:
    base = dict(
        general_id=1,
        specific_code=2,
        specific_confidence=0.9,
        general_confidence=0.95,
        name_brand_identifiable=True,
        brand="X",
        product_name="Y",
    )
    return Classification(**{**base, **kw})


def test_route1_high_confidence_goes_direct():
    assert decide_route_1({"initial": clf(specific_confidence=0.95)}) == "direct"


def test_route1_low_confidence_with_anchor_goes_search():
    assert decide_route_1({"initial": clf(specific_confidence=0.5, general_confidence=0.5)}) == "search"


def test_route1_low_confidence_without_anchor_goes_human():
    c = clf(
        specific_confidence=0.5,
        general_confidence=0.5,
        name_brand_identifiable=False,
        brand=None,
        product_name=None,
    )
    assert decide_route_1({"initial": c}) == "human"


def test_route1_no_initial_goes_human():
    assert decide_route_1({"initial": None}) == "human"


def test_route2_verified():
    st = {"revised": clf(specific_confidence=0.9), "evidence": [Evidence(provenance="web", nutrients=[])]}
    assert decide_route_2(st) == "direct_verified"


def test_route2_low_confidence_goes_human():
    st = {"revised": clf(specific_confidence=0.4), "evidence": [Evidence(provenance="web", nutrients=[])]}
    assert decide_route_2(st) == "human"


def test_route2_conflict_goes_human():
    st = {
        "revised": clf(specific_confidence=0.95, conflict=True),
        "evidence": [Evidence(provenance="web", nutrients=[])],
    }
    assert decide_route_2(st) == "human"


def test_route2_no_evidence_goes_human():
    assert decide_route_2({"revised": clf(), "evidence": []}) == "human"


def test_route2_leaf_unresolved_goes_human():
    """有证据但叶子仍未定 → 人工，不能直出。"""
    st = {
        "revised": clf(
            specific_code=None,
            candidate_codes=[2, 12],
            leaf_vs_parent="parent",
            specific_confidence=0.9,
        ),
        "evidence": [Evidence(provenance="web", nutrients=[])],
    }
    assert decide_route_2(st) == "human"


@pytest.mark.parametrize("code", [2, 12, 5, 19, 8, 23])
def test_valid_codes_accepted(code):
    assert clf(specific_code=code).specific_code == code


def test_invalid_code_rejected():
    with pytest.raises(Exception):
        clf(specific_code=99)
