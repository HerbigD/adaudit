"""Day 5 验收测试（设计文档 §8 的 7 组）。

1 三种失败态 → 状态与路由    2 冲突判定 60% vs 20%    3 单位换算三例
4 降级链路（坏 JSON 不抛异常） 5 预算断言              6 多语言查询
7 域名表
"""

from __future__ import annotations

import asyncio

import pytest

from config import settings
from graph.edges import decide_route_2, verified_threshold_for
from graph.state import Classification, Evidence, NutrientValue
from services import nutrition, search, taxonomy, vlm
from services.search import Query, SearchBudget, SearchHit


def ev(nutrient_values: dict[str, float | None], *, source_type="official", tier=1, **kw) -> Evidence:
    return Evidence(
        id=kw.pop("id", "ev_001"),
        source_url=kw.pop("source_url", "https://x.test/p"),
        source_title=kw.pop("source_title", "X"),
        source_type=source_type,
        query_tier=tier,
        provenance="web",
        extracted_by="mock-extract",
        nutrients=[
            NutrientValue(nutrient=n, value=v or 0.0, unit="g/100g", normalized=v)
            for n, v in nutrient_values.items()
        ],
        **kw,
    )


def clf(**kw) -> Classification:
    base = dict(
        brand="BrandX",
        product_name="Product Y",
        general_id=1,
        specific_code=2,
        specific_confidence=0.9,
        general_confidence=0.95,
    )
    return Classification(**{**base, **kw})


# --------------------------------------------------------------------------- #
# 1. 三种失败态 → 状态与路由
# --------------------------------------------------------------------------- #
async def test_no_result_status_and_route():
    # 品牌与品名都带 nobrand 标记 → Q1/Q2/Q3 全部空手而归
    out = await search.search_product("NoBrandCo", "NoBrand Mystery Pack", country="IN")
    assert out.status == "no_result"
    assert len(out.records) >= 1
    assert decide_route_2({"search_status": "no_result", "revised": None}) == "human"


async def test_tier3_can_rescue_when_brand_query_fails():
    """Q1/Q2 带错品牌搜不到时，Q3 去品牌仍可能救回来 —— 但要打 tier=3 降权。"""
    out = await search.search_product("NoBrandCo", "Crunchy Cereal", country="IN")
    assert out.status == "ok" and out.tier == 3


async def test_timeout_status_and_route(monkeypatch):
    async def _hang(_query):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(search, "_search_once", _hang)
    out = await search.search_product(
        "SlowBrand", "Slow Product", budget=SearchBudget(per_query_timeout=0.05, max_retries=1)
    )
    assert out.status == "timeout"
    assert decide_route_2({"search_status": "timeout", "revised": None}) == "human"


async def test_conflict_status_forces_human_regardless_of_confidence():
    state = {
        "search_status": "conflict",
        "revised": clf(specific_confidence=0.99),
        "evidence": [ev({"fat": 1.2})],
    }
    assert decide_route_2(state) == "human"


async def test_degraded_raises_the_bar_but_still_machine_judgeable():
    """degraded 不直接扔人工，而是给一次加严的机审机会。"""
    base = settings.verified_threshold
    bumped = verified_threshold_for("degraded")
    assert bumped == pytest.approx(base + settings.degraded_threshold_bump)

    just_above_base = clf(specific_confidence=base + 0.01)
    assert decide_route_2(
        {"search_status": "ok", "revised": just_above_base, "evidence": [ev({"sugar": 5})]}
    ) == "direct_verified"
    assert decide_route_2(
        {"search_status": "degraded", "revised": just_above_base, "evidence": [ev({"sugar": 5})]}
    ) == "human"

    well_above = clf(specific_confidence=bumped + 0.01)
    assert decide_route_2(
        {"search_status": "degraded", "revised": well_above, "evidence": [ev({"sugar": 5})]}
    ) == "direct_verified"


# --------------------------------------------------------------------------- #
# 2. 冲突判定：同 nutrient 偏差 60% vs 20%
# --------------------------------------------------------------------------- #
def test_conflict_detected_at_60_percent_gap():
    e = [ev({"fat": 1.2}, id="ev_001"), ev({"fat": 9.8}, id="ev_002")]
    hit, why = nutrition.detect_conflict(e, [5, 19])       # 5/19 判定维度 = fat
    assert hit and "fat" in why


def test_no_conflict_at_20_percent_gap():
    e = [ev({"fat": 8.0}, id="ev_001"), ev({"fat": 9.6}, id="ev_002")]
    hit, _ = nutrition.detect_conflict(e, [5, 19])
    assert not hit


def test_big_gap_off_the_decision_dimension_is_not_conflict():
    """条件 3：分歧必须落在目标混淆对的判定维度上，否则不算冲突。"""
    e = [ev({"protein": 1.0}, id="ev_001"), ev({"protein": 9.0}, id="ev_002")]
    hit, _ = nutrition.detect_conflict(e, [5, 19])         # fat 才是 5/19 的维度
    assert not hit


def test_single_value_cannot_conflict():
    assert not nutrition.detect_conflict([ev({"fat": 1.2})], [5, 19])[0]


# --------------------------------------------------------------------------- #
# 3. 单位换算
# --------------------------------------------------------------------------- #
def test_normalize_mg_per_100g():
    assert nutrition.normalize(168, "mg/100g") == pytest.approx(0.168)


def test_normalize_per_serving_without_size_is_none():
    assert nutrition.normalize(9, "g/serving") is None
    assert nutrition.normalize(9, "g per serving") is None


def test_normalize_per_serving_with_size():
    assert nutrition.normalize(9, "g/serving", serving_size_g=30) == pytest.approx(30.0)


def test_normalize_fl_oz():
    assert nutrition.normalize(11, "g/floz") == pytest.approx(11 * 100 / 29.5735, rel=1e-4)


def test_normalize_explicit_basis_and_percent():
    assert nutrition.normalize(9, "g/30g") == pytest.approx(30.0)
    assert nutrition.normalize(20, "%RDA") is None


# --------------------------------------------------------------------------- #
# 4. 降级链路：抽取返回坏 JSON → 不抛异常，走降级
# --------------------------------------------------------------------------- #
async def test_bad_json_falls_back_to_degraded(monkeypatch):
    async def _bad(_s, _u):
        return "sorry, I cannot comply — here is some prose instead"

    monkeypatch.setattr(vlm, "complete", _bad)
    cands = [(SearchHit("https://x.test/p", "X product", "some text"), "official")]
    evidence, mode = await nutrition.extract_evidence(
        cands, brand="BrandX", product_name="Product Y", query=Query("q", 1)
    )
    assert mode == "degraded"
    assert evidence and all(e.is_degraded for e in evidence)
    assert evidence[0].conclusion_hint            # 降级仍给类别倾向
    assert evidence[0].snippet                    # 原文保留，供人工核


async def test_all_match_false_falls_back_to_degraded(monkeypatch):
    async def _no_match(_s, _u):
        return '{"items":[{"index":1,"match":false,"nutrients":[]}]}'

    monkeypatch.setattr(vlm, "complete", _no_match)
    cands = [(SearchHit("https://x.test/p", "X product", "text"), "official")]
    evidence, mode = await nutrition.extract_evidence(
        cands, brand="BrandX", product_name="Product Y", query=Query("q", 1)
    )
    assert mode == "degraded" and evidence


async def test_mock_provider_uses_rule_extract():
    """mock 下没有 LLM：走规则抽取，adapter 以 mock 开头 → 被 eval 双闸拦截。"""
    hit = SearchHit(
        "https://brandsite.example/p",
        "MockBrand — Nutrition",
        "Nutrition per 100 g: Fat 4.2 g, Sugars 24.6 g, Fibre 3.1 g, Sodium 168 mg.",
    )
    evidence, mode = await nutrition.extract_evidence(
        [(hit, "official")], brand="MockBrand", product_name="Mock Product",
        query=Query("q", 1)
    )
    assert mode == "rule"
    assert evidence[0].extracted_by.startswith("mock")
    assert evidence[0].get("sugar") == pytest.approx(24.6)
    assert evidence[0].get("sodium") == pytest.approx(0.168)   # mg → g


# --------------------------------------------------------------------------- #
# 5. 预算断言
# --------------------------------------------------------------------------- #
async def test_query_budget_capped(monkeypatch):
    calls: list[str] = []

    async def _empty(query):
        calls.append(query)
        return []

    monkeypatch.setattr(search, "_search_once", _empty)
    out = await search.search_product(
        "BudgetBrand", "Some Product", budget=SearchBudget(max_retries=0)
    )
    assert len(calls) <= settings.search_max_queries
    assert out.queries_used <= settings.search_max_queries


async def test_retry_only_once_and_only_on_timeout(monkeypatch):
    calls: list[str] = []

    async def _slow(query):
        calls.append(query)
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(search, "_search_once", _slow)
    out = await search.search_product(
        "SlowBrand", "Some Product",
        budget=SearchBudget(max_queries=1, per_query_timeout=0.05, max_retries=1),
    )
    assert len(calls) == 2                      # 首次 + 重试一次，不再多
    assert out.records[0].attempts == 2


async def test_no_result_is_not_retried(monkeypatch):
    calls: list[str] = []

    async def _empty(query):
        calls.append(query)
        return []

    monkeypatch.setattr(search, "_search_once", _empty)
    await search.search_product(
        "EmptyBrand", "Some Product", budget=SearchBudget(max_queries=1, max_retries=1)
    )
    assert len(calls) == 1                      # 无结果不重试


async def test_hit_stops_remaining_queries(monkeypatch):
    calls: list[str] = []

    async def _hit(query):
        calls.append(query)
        return [SearchHit("https://x.test/p", "X", "Sugars 5 g per 100 g")]

    monkeypatch.setattr(search, "_search_once", _hit)
    out = await search.search_product("HitBrand", "Some Product")
    assert len(calls) == 1 and out.status == "ok" and out.tier == 1


# --------------------------------------------------------------------------- #
# 6. 多语言查询
# --------------------------------------------------------------------------- #
LANG_CASES = [
    ("hi", "Maggi मैगी", "New Limited Edition Masala Instant Noodles 70g", "IN"),
    ("bn", "Pran প্রাণ", "Special Offer Chanachur 150g", "BD"),
    ("ur", "Olpers اولپرز", "Full Cream Milk 1L", "PK"),
    ("si", "Anchor ඇන්කර්", "Toned Milk Powder 400g", "LK"),
    ("en", "Amul", "Double Toned Milk 500ml", "IN"),
]


@pytest.mark.parametrize("lang,brand,name,country", LANG_CASES)
def test_queries_are_english_and_never_chinese(lang, brand, name, country):
    qs = search.build_queries(brand, name, ad_language=lang, country=country)
    assert qs, lang
    for q in qs:
        assert not search.CJK.search(q.text), q.text
    # Q1/Q2 必须是英文构造：不含本土文字
    for q in qs:
        if q.tier < 3:
            assert not search.NATIVE_SCRIPTS.search(q.text), q.text


@pytest.mark.parametrize("lang,brand,name,country", LANG_CASES)
def test_native_script_only_appears_in_tier3(lang, brand, name, country):
    qs = search.build_queries(brand, name, ad_language=lang, country=country)
    native_qs = [q for q in qs if search.NATIVE_SCRIPTS.search(q.text)]
    assert all(q.tier == 3 for q in native_qs)
    _, brand_native = search.split_script(brand)
    if brand_native:
        assert native_qs, f"{lang}: 本土文字品牌应在 Q3 双写"


def test_marketing_words_stripped_but_category_terms_kept():
    qs = search.build_queries(
        "Maggi", "New Limited Edition Masala Instant Noodles 70g",
        ad_language="hi", country="IN",
    )
    q1 = qs[0].text.lower()
    assert "limited" not in q1 and "edition" not in q1
    assert "instant noodles" in q1          # 品类词携带判定信息，不许被截掉


def test_toned_milk_survives_shortening():
    """toned / double toned 直接决定 5/19，截断时必须保住。"""
    q = search.build_queries("Amul", "Special Offer Double Toned Milk 500ml", country="IN")[0]
    assert "double toned milk" in q.text.lower()


def test_generic_category_query_is_refused():
    assert search.build_queries(None, "yoghurt", country="IN") == []
    assert search.build_queries(None, "milk", country="BD") == []


def test_cjk_query_is_rejected_outright():
    with pytest.raises(ValueError):
        search.assert_no_cjk([Query("蒙牛 酸奶 营养成分", 1)])


# --------------------------------------------------------------------------- #
# 7. 域名表
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,country,expected",
    [
        ("https://www.amazon.in/dp/B0X", "IN", "ecommerce"),
        ("https://daraz.com.bd/products/x", "BD", "ecommerce"),
        ("https://daraz.pk/products/x", "PK", "ecommerce"),
        ("https://keells.com/p/x", "LK", "ecommerce"),
        ("https://fdc.nal.usda.gov/food/x", "IN", "nutrition_db"),
        ("https://www.ndtv.com/food/x", "IN", "other"),
    ],
)
def test_source_type_from_domain_table(url, country, expected):
    assert nutrition.classify_source(url, "SomeBrand", country) == expected


def test_official_beats_domain_table_and_is_country_agnostic():
    assert nutrition.classify_source("https://amul.com/products/x", "Amul", None) == "official"
    assert nutrition.classify_source("https://www.maggi.in/p", "Maggi", "LK") == "official"


def test_news_site_is_blacklisted():
    assert nutrition.is_blacklisted("https://www.ndtv.com/food/x")
    assert nutrition.is_blacklisted("https://youtube.com/watch?v=1")
    assert not nutrition.is_blacklisted("https://amazon.in/dp/x")


def test_screening_drops_blacklist_and_ranks_official_first():
    hits = [
        SearchHit("https://www.ndtv.com/food/amul", "Amul Milk news", "..."),
        SearchHit("https://amazon.in/dp/x", "Amul Toned Milk 500ml", "..."),
        SearchHit("https://amul.com/p", "Amul Toned Milk", "..."),
    ]
    cands, stats = nutrition.screen_candidates(
        hits, brand="Amul", product_name="Toned Milk", country="IN"
    )
    assert stats["blacklisted"] == 1
    assert [st for _, st in cands][0] == "official"


def test_native_script_title_is_not_dropped_by_overlap_check():
    """本土文字标题与英文查询词无重叠，不该被误杀（Daraz 类页面）。"""
    hits = [SearchHit("https://daraz.com.bd/p", "প্রাণ চানাচুর ১৫০ গ্রাম", "...")]
    cands, stats = nutrition.screen_candidates(
        hits, brand="Pran", product_name="Chanachur", country="BD"
    )
    assert stats["no_overlap"] == 0 and len(cands) == 1


def test_unknown_country_merges_all_tables():
    """国家推不出时宁可判宽 —— daraz 仍应判成 ecommerce 而不是 other。"""
    assert nutrition.classify_source("https://daraz.lk/p", "SomeBrand", None) == "ecommerce"


# --------------------------------------------------------------------------- #
# search_status 状态机（Day5 §7）
# --------------------------------------------------------------------------- #
def test_status_ok_requires_normalized_on_target_dimension():
    from graph.nodes.web_search import decide_status

    usable = [ev({"fat": 1.2})]
    assert decide_status(usable, False, 1, [5, 19]) == "ok"


def test_status_degraded_when_values_cannot_be_normalized():
    """抽到读数但换算不出（per serving 缺份量）→ degraded，不给 ok 的直出门槛。"""
    from graph.nodes.web_search import decide_status

    unnormalizable = [ev({"fat": None})]
    assert decide_status(unnormalizable, False, 1, [5, 19]) == "degraded"


def test_status_degraded_when_all_tier3():
    from graph.nodes.web_search import decide_status

    assert decide_status([ev({"fat": 1.2}, tier=3)], False, 3, [5, 19]) == "degraded"


def test_status_conflict_wins_over_everything():
    from graph.nodes.web_search import decide_status

    assert decide_status([ev({"fat": 1.2})], True, 1, [5, 19]) == "conflict"
