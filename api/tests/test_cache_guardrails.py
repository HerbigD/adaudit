"""Day 3 加固 1 & 2：缓存写入护栏 + 人工裁定 supersede。"""

from __future__ import annotations

import pytest

import db
from config import settings
from graph.state import Classification, Evidence, NutrientValue
from services import cache_store


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    """SQLite 行与向量库**都要**隔离 —— 只清前者会让命中得分依赖测试执行顺序。"""
    from services import vectorstore

    monkeypatch.setattr(settings, "chroma_path", str(tmp_path / "chroma"))
    vectorstore.reset()
    db.init_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM product_cache")
    yield
    vectorstore.reset()


def clf(conf: float = 0.95, code: int | None = 12, **kw) -> Classification:
    from services import taxonomy

    return Classification(
        brand=kw.pop("brand", "AcmeFoods"),
        product_name=kw.pop("product_name", "Crunchy Cereal 500g"),
        general_id=taxonomy.general_id_of(code) if code else 1,
        specific_code=code,
        leaf_vs_parent="leaf" if code else "parent",
        specific_confidence=conf,
        general_confidence=max(conf, 0.9),
        **kw,
    )


def web_ev(**kw) -> Evidence:
    return Evidence(
        id="ev_001",
        source_url="https://x.test/p",
        source_title="X product",
        source_type="official",
        provenance="web",
        nutrients=[
            NutrientValue(nutrient="sugar", value=24.6, unit="g/100g", normalized=24.6),
            NutrientValue(nutrient="fiber", value=3.1, unit="g/100g", normalized=3.1),
        ],
        extracted_by="mock-extract",
        **kw,
    )


# --------------------------------------------------------------------------- #
# 护栏
# --------------------------------------------------------------------------- #
def test_guard_allows_high_confidence_web_backed_leaf():
    ok, why = cache_store.should_cache(clf(), [web_ev()], "ok")
    assert ok, why


def test_guard_blocks_low_confidence():
    ok, why = cache_store.should_cache(
        clf(conf=settings.direct_threshold - 0.01), [web_ev()], "ok"
    )
    assert not ok and "DIRECT_THRESHOLD" in why


def test_guard_blocks_when_search_not_ok():
    for status in ("no_result", "timeout", "budget_exceeded", "cache", None):
        ok, why = cache_store.should_cache(clf(), [web_ev()], status)
        assert not ok, status


def test_guard_blocks_empty_or_cache_only_evidence():
    assert not cache_store.should_cache(clf(), [], "ok")[0]
    cache_only = [Evidence(provenance="cache", source_type="cache",
                       nutrients=[NutrientValue(nutrient="sugar", value=24.6,
                                                unit="g/100g", normalized=24.6)])]
    ok, why = cache_store.should_cache(clf(), cache_only, "ok")
    assert not ok and "联网证据" in why


def test_guard_blocks_conflict_and_parent_level():
    assert not cache_store.should_cache(clf(conflict=True), [web_ev()], "ok")[0]
    parent = clf(code=None, candidate_codes=[2, 12])
    assert not cache_store.should_cache(parent, [web_ev()], "ok")[0]


# --------------------------------------------------------------------------- #
# provenance 与 supersede
# --------------------------------------------------------------------------- #
def test_auto_write_then_human_supersede():
    auto = cache_store.upsert("AcmeFoods", "Crunchy Cereal 500g", [web_ev()], clf(), provenance="auto")
    assert auto["action"] == "created"

    rec, score = cache_store.lookup("AcmeFoods", "Crunchy Cereal 500g")
    assert rec["provenance"] == "auto" and score >= settings.cache_hit_threshold

    human = cache_store.supersede_with_human_verdict(
        "acmefoods", "CRUNCHY CEREAL 500G", [web_ev()], clf(code=2), audit_id="aud-1"
    )
    assert human["action"] == "superseded"
    assert human["id"] == auto["id"]           # 唯一键 upsert，不分叉

    rec, _ = cache_store.lookup("AcmeFoods", "Crunchy Cereal 500g")
    assert rec["provenance"] == "human_verified"
    assert rec["revision"] == 2
    assert rec["superseded_by"] == "aud-1"
    assert rec["verdict"]["specific_code"] == 2


def test_auto_cannot_overwrite_human_verified():
    """单向棘轮：auto 不许覆盖人工核过的档案。"""
    cache_store.supersede_with_human_verdict(
        "AcmeFoods", "Crunchy Cereal 500g", [web_ev()], clf(code=2)
    )
    res = cache_store.upsert(
        "AcmeFoods", "Crunchy Cereal 500g", [web_ev()], clf(code=12), provenance="auto"
    )
    assert res["action"] == "refused"
    rec, _ = cache_store.lookup("AcmeFoods", "Crunchy Cereal 500g")
    assert rec["provenance"] == "human_verified"
    assert rec["verdict"]["specific_code"] == 2


def test_evidence_carries_provenance():
    cache_store.upsert("AcmeFoods", "Crunchy Cereal 500g", [web_ev()], clf(), provenance="auto")
    rec, _ = cache_store.lookup("AcmeFoods", "Crunchy Cereal 500g")
    ev = cache_store.to_evidence(rec)[0]
    assert ev.cache_provenance == "auto"
    assert "自动沉淀" in ev.source_title
    assert ev.get("sugar") == 24.6


def test_stats_counts_verified_and_superseded():
    cache_store.upsert("A", "P1", [web_ev()], clf(), provenance="auto")
    cache_store.supersede_with_human_verdict("A", "P1", [web_ev()], clf(code=2))
    s = cache_store.stats()
    assert s["products"] == 1 and s["human_verified"] == 1 and s["superseded"] == 1
