"""结果筛选 → 营养抽取 → Evidence 产出 → 冲突判定。

两阶段（Day5 §5）：
  阶段一 · 候选筛选：纯 Python，零成本 —— 黑名单、标题重叠、source_type、排序取前 3
  阶段二 · LLM 抽取：一次调用批量处理 3 个候选，全程英文 prompt
抽取失败（JSON 非法 / 全部 match=false）→ **降级模式**：不调第二次，
把 snippet 原文 + 一次类别倾向存成 Evidence（nutrients 为空）。

单位换算集中在本文件的 `normalize()` 一个函数里 —— **裁决节点只看 normalized，不做换算**。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from config import settings
from graph.state import Evidence, Nutrient, NutrientValue, SourceType
from services import taxonomy, vlm
from services.search import Query, SearchHit, split_script

logger = logging.getLogger(__name__)

NUTRIENTS: tuple[Nutrient, ...] = ("sugar", "fat", "fiber", "sodium", "protein")


# --------------------------------------------------------------------------- #
# 域名表（数据驱动）
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _sources() -> dict[str, Any]:
    return json.loads(Path(settings.sources_by_country_path).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def blacklist() -> frozenset[str]:
    groups = _sources()["blacklist"]
    return frozenset(
        d.lower() for k, v in groups.items() if not k.startswith("_") for d in v
    )


def _country_block(country: str | None) -> dict[str, Any]:
    countries = _sources()["countries"]
    block = dict(countries.get("_global", {}))
    if country and country.upper() in countries:
        local = countries[country.upper()]
        for key in ("ecommerce", "nutrition_db"):
            block[key] = list(local.get(key, [])) + list(block.get(key, []))
    else:
        # 推不出国家：合并全部国家的表，宁可判宽也不误判成 other
        for code, local in countries.items():
            if code.startswith("_"):
                continue
            for key in ("ecommerce", "nutrition_db"):
                block[key] = list(block.get(key, [])) + list(local.get(key, []))
    return block


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) if m else "").lower().removeprefix("www.")


def is_blacklisted(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in blacklist())


def classify_source(url: str, brand: str | None, country: str | None = None) -> SourceType:
    """official 判定与国家/语言无关，永远生效；其余查国家域名表。"""
    host = _host(url)
    if not host:
        return "other"

    brand_latin, _ = split_script(brand)
    brand_key = re.sub(r"[^a-z0-9]", "", (brand_latin or "").lower())
    if brand_key and brand_key in re.sub(r"[^a-z0-9]", "", host):
        return "official"

    block = _country_block(country)
    for domain in block.get("nutrition_db", []):
        if host == domain or host.endswith("." + domain):
            return "nutrition_db"
    for domain in block.get("ecommerce", []):
        if host == domain or host.endswith("." + domain):
            return "ecommerce"
    return "other"


SOURCE_RANK = {"official": 0, "nutrition_db": 1, "ecommerce": 2, "cache": 3, "other": 4}


# --------------------------------------------------------------------------- #
# 阶段一 · 候选筛选
# --------------------------------------------------------------------------- #
def _token_set(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1}


def _has_overlap(title: str, brand: str | None, product_name: str | None) -> bool:
    """标题与 brand+product_name 有无重叠。

    本土文字标题与英文查询词天然无重叠，这时改用拉丁部分再判一次，
    防止误杀 Daraz 这类本土电商页面（Day5 §5 阶段一）。
    """
    want = _token_set(f"{brand or ''} {product_name or ''}")
    if not want:
        return True
    if _token_set(title) & want:
        return True
    latin_title, native_title = split_script(title)
    if native_title and _token_set(latin_title) & want:
        return True
    # 标题完全是本土文字：无法判重叠，放行交给阶段二（LLM 能读多语言）
    return bool(native_title) and not _token_set(latin_title)


def screen_candidates(
    hits: Iterable[SearchHit],
    *,
    brand: str | None,
    product_name: str | None,
    country: str | None = None,
    topk: int | None = None,
) -> tuple[list[tuple[SearchHit, SourceType]], dict[str, int]]:
    """返回 (候选列表, 筛选统计)。排序：official > nutrition_db > ecommerce > other。"""
    topk = topk or settings.search_candidates_topk
    hits = list(hits)
    stats = {"in": len(hits), "blacklisted": 0, "no_overlap": 0, "out": 0}

    scored: list[tuple[int, SearchHit, SourceType]] = []
    for h in hits:
        if is_blacklisted(h.url):
            stats["blacklisted"] += 1
            continue
        if not _has_overlap(h.title, brand, product_name):
            stats["no_overlap"] += 1
            continue
        st = classify_source(h.url, brand, country)
        scored.append((SOURCE_RANK[st], h, st))

    scored.sort(key=lambda x: x[0])
    out = [(h, st) for _, h, st in scored[:topk]]
    stats["out"] = len(out)
    return out, stats


# --------------------------------------------------------------------------- #
# 单位换算 —— 全项目唯一一处
# --------------------------------------------------------------------------- #
MASS_FACTOR = {"g": 1.0, "gram": 1.0, "grams": 1.0, "mg": 1e-3, "mcg": 1e-6, "µg": 1e-6, "ug": 1e-6}
FL_OZ_ML = 29.5735
OZ_G = 28.3495


def normalize(value: float, unit: str, serving_size_g: float | None = None) -> float | None:
    """把 `value unit` 换算到 g/100g（固体）或 g/100ml（液体）。

    换算不出（如 per serving 但缺份量）返回 None —— 此时 snippet 里保留原值，
    裁决节点看到 normalized=None 就知道这条读数不能用来卡阈值。
    """
    if value is None:
        return None
    u = (unit or "").strip().lower().replace(" ", "")

    # 分子质量单位
    numerator = 1.0
    for token, factor in MASS_FACTOR.items():
        if u.startswith(token):
            numerator = factor
            break
    else:
        if u.startswith("%"):
            return None                      # 百分比（如 %RDA）不是绝对量

    grams = value * numerator

    # 分母基准
    if "/100g" in u or "/100ml" in u or "per100g" in u or "per100ml" in u:
        return round(grams, 4)

    m = re.search(r"/(\d+(?:\.\d+)?)(g|ml)\b", u) or re.search(r"per(\d+(?:\.\d+)?)(g|ml)", u)
    if m:
        basis = float(m.group(1))
        return round(grams * 100.0 / basis, 4) if basis else None

    if "floz" in u:
        return round(grams * 100.0 / FL_OZ_ML, 4)
    if re.search(r"/oz\b|peroz", u):
        return round(grams * 100.0 / OZ_G, 4)

    if "serving" in u or "portion" in u or "serve" in u:
        if serving_size_g and serving_size_g > 0:
            return round(grams * 100.0 / serving_size_g, 4)
        return None                          # 缺份量 → 明确置空，不猜

    # 没写分母：按 per 100g 处理（营养面板的通用默认），但降低置信由调用方处理
    if u in MASS_FACTOR or not u:
        return round(grams, 4)
    return None


# --------------------------------------------------------------------------- #
# 阶段二 · 抽取
# --------------------------------------------------------------------------- #
EXTRACT_SYSTEM_PROMPT = """You are a nutrition fact extractor. Extract nutrition data for the
specified product from the given web snippets.
Rules:
1. Extract ONLY values explicitly present in the snippet. Never guess or fill in missing nutrients.
2. Keep the original unit as-is; also provide `normalized` in g/100g (solids) or g/100ml (liquids).
   If serving-size info is missing, set normalized to null.
3. If a snippet is about a different product (same name, different variant or pack size with
   materially different values), return match=false for it.
4. Snippets may mix English with Hindi/Bengali/Urdu/Sinhala/Tamil — extract from whichever
   language carries the nutrition panel; do not translate values.
5. Output strict JSON only.

JSON schema:
{"items":[{"index":int,"match":bool,"nutrients":[{"nutrient":"sugar|fat|fiber|sodium|protein",
"value":number,"unit":string,"normalized":number|null,"confidence":number}]}]}"""


def _user_prompt(
    brand: str | None,
    product_name: str | None,
    ad_language: str,
    country: str | None,
    candidates: list[tuple[SearchHit, SourceType]],
) -> str:
    lines = [
        f"Target product: {brand or '(unknown brand)'} {product_name or ''}".strip(),
        f"Ad language: {ad_language} / Country: {country or 'unknown'}",
        "Candidates:",
    ]
    for i, (h, st) in enumerate(candidates, start=1):
        lines.append(f"[{i}] {h.title} {h.url} (source_type={st})")
        lines.append(h.snippet[:600])
    return "\n".join(lines)


# 规则抽取用的正则（mock/离线兜底，adapter 打 mock-extract 会被 eval 双闸拦下）
_NUM = r"([\d]+(?:[.,]\d+)?)"
_UNIT = r"\s*(mg|g|µg|mcg)\b"
RULE_PATTERNS: dict[Nutrient, list[str]] = {
    "sugar": [rf"sugars?\D{{0,20}}{_NUM}{_UNIT}"],
    "fat": [rf"(?<!saturated\s)(?<!trans\s)fat\D{{0,20}}{_NUM}{_UNIT}"],
    "fiber": [rf"fib(?:re|er)\D{{0,20}}{_NUM}{_UNIT}"],
    "sodium": [rf"sodium\D{{0,20}}{_NUM}{_UNIT}", rf"salt\D{{0,20}}{_NUM}{_UNIT}"],
    "protein": [rf"protein\D{{0,20}}{_NUM}{_UNIT}"],
}


def _basis_from_text(text: str) -> str:
    t = text.lower()
    if re.search(r"per\s*100\s*ml", t):
        return "/100ml"
    if re.search(r"per\s*100\s*g", t):
        return "/100g"
    m = re.search(r"per\s*(\d+(?:\.\d+)?)\s*(g|ml)\b", t)
    if m:
        return f"/{m.group(1)}{m.group(2)}"
    if re.search(r"per\s*(serving|serve|portion)", t):
        return "/serving"
    if re.search(r"fl\.?\s*oz", t):
        return "/floz"
    return "/100g"


def rule_extract(hit: SearchHit) -> list[NutrientValue]:
    text = f"{hit.title}\n{hit.snippet}"
    basis = _basis_from_text(text)
    out: list[NutrientValue] = []
    for nutrient, pats in RULE_PATTERNS.items():
        for pat in pats:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            try:
                value = float(m.group(1).replace(",", "."))
            except ValueError:
                continue
            unit = f"{m.group(2).lower()}{basis}"
            out.append(
                NutrientValue(
                    nutrient=nutrient,
                    value=value,
                    unit=unit,
                    normalized=normalize(value, unit),
                    confidence=0.75,
                )
            )
            break
    return out


async def extract_evidence(
    candidates: list[tuple[SearchHit, SourceType]],
    *,
    brand: str | None,
    product_name: str | None,
    ad_language: str = "en",
    country: str | None = None,
    query: Query | None = None,
) -> tuple[list[Evidence], str]:
    """返回 (Evidence 列表, 抽取模式)。模式 ∈ {llm, rule, degraded}。"""
    if not candidates:
        return [], "degraded"

    tier = query.tier if query else 1
    qtext = query.text if query else ""

    # --- 优先真实 LLM ---
    try:
        text = await vlm.complete(
            EXTRACT_SYSTEM_PROMPT,
            _user_prompt(brand, product_name, ad_language, country, candidates),
        )
        items = _parse_extraction(text)
        evidence = _build_from_items(items, candidates, qtext, tier, vlm.settings_llm_name())
        if evidence:
            return evidence[: settings.max_evidence], "llm"
        logger.info("抽取返回空/全部 match=false，转降级模式")
    except vlm.VLMError:
        # mock / 未配置 provider：走规则抽取，adapter 打 mock-extract
        evidence = _build_by_rule(candidates, qtext, tier)
        if evidence:
            return evidence[: settings.max_evidence], "rule"
    except Exception as exc:  # noqa: BLE001 — JSON 非法等，一律降级，不调第二次
        logger.warning("抽取失败，转降级模式: %s", exc)

    return _build_degraded(candidates, qtext, tier), "degraded"


def _parse_extraction(text: str) -> list[dict[str, Any]]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("抽取输出中没有 JSON")
    data = json.loads(raw[start : end + 1])
    return [i for i in data.get("items", []) if i.get("match", True)]


def _build_from_items(
    items: list[dict[str, Any]],
    candidates: list[tuple[SearchHit, SourceType]],
    qtext: str,
    tier: int,
    adapter: str,
) -> list[Evidence]:
    out: list[Evidence] = []
    for item in items:
        idx = int(item.get("index", 0)) - 1
        if not (0 <= idx < len(candidates)):
            continue
        hit, st = candidates[idx]
        nutrients: list[NutrientValue] = []
        for nv in item.get("nutrients", []):
            if nv.get("nutrient") not in NUTRIENTS:
                continue
            value, unit = float(nv["value"]), str(nv.get("unit", ""))
            normalized = nv.get("normalized")
            if normalized is None:
                normalized = normalize(value, unit)
            nutrients.append(
                NutrientValue(
                    nutrient=nv["nutrient"],
                    value=value,
                    unit=unit,
                    normalized=normalized,
                    confidence=float(nv.get("confidence", 0.8)),
                )
            )
        if nutrients:
            out.append(
                _evidence(hit, st, qtext, tier, adapter, nutrients=nutrients)
            )
    return out


def _build_by_rule(
    candidates: list[tuple[SearchHit, SourceType]], qtext: str, tier: int
) -> list[Evidence]:
    out: list[Evidence] = []
    for hit, st in candidates:
        nutrients = rule_extract(hit)
        if nutrients:
            out.append(_evidence(hit, st, qtext, tier, "mock-extract", nutrients=nutrients))
    return out


def _build_degraded(
    candidates: list[tuple[SearchHit, SourceType]], qtext: str, tier: int
) -> list[Evidence]:
    """降级 Evidence：nutrients 为空，保留原文供人工/裁决参考。

    照样可用，但裁决节点必须显式声明"基于非结构化证据"。
    """
    return [
        _evidence(
            hit,
            st,
            qtext,
            tier,
            "degraded",
            nutrients=[],
            conclusion_hint="页面未提供可解析的营养成分表，仅能依据文案与品类线索判断",
        )
        for hit, st in candidates
    ]


def _evidence(
    hit: SearchHit,
    st: SourceType,
    qtext: str,
    tier: int,
    adapter: str,
    *,
    nutrients: list[NutrientValue],
    conclusion_hint: str | None = None,
) -> Evidence:
    return Evidence(
        product_query=qtext,
        source_url=hit.url,
        source_title=hit.title,
        source_type=st,
        snippet=(hit.snippet or "")[:300],
        nutrients=nutrients,
        conclusion_hint=conclusion_hint,
        provenance="web",
        query_tier=tier,
        extracted_by=adapter,
    )


def assign_ids(evidence: list[Evidence], start: int = 1) -> list[Evidence]:
    """ev_001 递增 —— 裁决节点引用的就是这个 id。"""
    return [
        e.model_copy(update={"id": f"ev_{i:03d}"})
        for i, e in enumerate(evidence, start=start)
    ]


# --------------------------------------------------------------------------- #
# 冲突判定（Day5 §6）
# --------------------------------------------------------------------------- #
def detect_conflict(
    evidence: list[Evidence], target_codes: Iterable[int] | None = None
) -> tuple[bool, str]:
    """三条**全部满足**才判冲突，返回 (是否冲突, 说明)。

    1. ≥2 条 Evidence 含同一 nutrient 的 normalized 值
    2. 最高值与最低值相对偏差 > 50%
    3. 该 nutrient 恰好落在目标混淆对的判定维度上

    设计理由：新旧配方差异和小数点错位是常态，只有落在判定维度上的大分歧才值得转人工；
    否则裁决节点按 source_type 优先级取高可信来源即可，并在 reasoning 里声明取舍。
    """
    dims = taxonomy.pair_nutrients(target_codes)
    if not dims:
        return False, "目标混淆对未知，不做冲突判定"

    for nutrient in dims:
        values = [e.get(nutrient) for e in evidence]
        values = [v for v in values if v is not None]
        if len(values) < 2:
            continue
        lo, hi = min(values), max(values)
        if hi <= 0:
            continue
        gap = (hi - lo) / hi
        if gap > settings.conflict_relative_gap:
            return True, (
                f"{nutrient} 跨来源相对偏差 {gap:.0%}（{lo}–{hi}），"
                f"且该维度正是判定依据"
            )
    return False, "无落在判定维度上的大分歧"


def summarize(evidence: list[Evidence]) -> str:
    """给 SSE / trace 用的一行人话摘要。"""
    if not evidence:
        return "无证据"
    if all(e.is_degraded for e in evidence):
        return f"{len(evidence)} 条降级证据（无营养面板）"
    labels = {"sugar": "糖", "fat": "脂肪", "fiber": "纤维", "sodium": "钠", "protein": "蛋白"}
    parts: list[str] = []
    for n in NUTRIENTS:
        v = next((e.get(n) for e in evidence if e.get(n) is not None), None)
        if v is not None:
            parts.append(f"{labels[n]} {v}g/100")
    return "、".join(parts) or "未抽到可用 normalized 值"
