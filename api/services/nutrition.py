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
from services.search import GENERIC_CATEGORY_WORDS, Query, SearchHit, split_script

logger = logging.getLogger(__name__)

# Annex 4 的判据用到 saturated fat 与 energy(kJ)（8/23 按每份判饱和脂肪+钠，9 还要能量），
# Day6 一并纳入抽取范围 —— 不抽就等于把 8/23/9 永久锁死在"证据不足 → 转人工"。
NUTRIENTS: tuple[Nutrient, ...] = (
    "sugar", "fat", "saturated_fat", "fiber", "sodium", "protein", "energy_kj",
)


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


def _discriminative_tokens(brand: str | None, product_name: str | None) -> set[str]:
    """能证明"是同一个产品"的词 —— **类目词不算**。

    `GENERIC_CATEGORY_WORDS` 里的 yoghurt / drink / milk 这类词，
    全网每一个同类页面上都有。拿它们当"同一个产品"的判据，
    竞品页（`Anchor Drinking Yoghurt 180ml`）和科普文
    （`Greek Yoghurt Nutrition Facts`）会一起进候选池 ——
    它们的营养面板是**真的**，只是**不是这个产品的**。
    那比没有证据更糟：无证据会老实转人工，错证据会伪装成有据可依。

    与 `search.is_generic` 用同一份词表、同一条理由（Day5 §3 规则 3）：
    那边管"不许发泛查询"，这边管"不许收泛结果"。之前只做了前一半 ——
    实测 `Kotmale Drinking Yoghurt` 的搜索结果里，
    `Anchor Drinking Yoghurt` 只靠 "drinking"+"yoghurt" 就能过关。
    """
    brand_latin, _ = split_script(brand)
    toks = _token_set(f"{brand_latin or ''} {product_name or ''}")
    return {t for t in toks if t not in GENERIC_CATEGORY_WORDS}


def _has_overlap(
    title: str, brand: str | None, product_name: str | None, url: str = ""
) -> bool:
    """标题（或域名）与目标产品有没有**区分性**重叠。

    本土文字标题与英文查询词天然无重叠，这时改用拉丁部分再判一次，
    防止误杀 Daraz 这类本土电商页面（Day5 §5 阶段一）。
    """
    brand_latin, brand_native = split_script(brand)
    brand_toks = _token_set(brand_latin or "")

    # **品牌已知 → 必须命中品牌。** 品名不参与判定。
    #
    # 为什么不靠"非类目词"凑：`GENERIC_CATEGORY_WORDS` 里有 "drink" 却没有
    # "drinking"，于是 `Anchor Drinking Yoghurt` 靠一个 "drinking" 就过了关。
    # 补词表是补不完的 —— 单复数、-ing、拼写变体（yogurt/yoghurt）没有尽头，
    # 而漏一个的代价是竞品的营养面板被当成本产品的证据。
    # 与 HFSS 那次同类：**别用一份必须穷举才成立的表当判据**。
    # 品牌才是识别产品的那个词，就认它。
    #
    # 代价是品牌 OCR 认错时会全部落空 → `no_result` → 转人工。
    # 那是老实的失败：宁可交给人，也不拿错产品的数字去裁定（纪律 #6）。
    want = brand_toks or _discriminative_tokens(brand, product_name)
    if not want:
        # 品牌未识别且品名全是类目词 —— 我们这边没有任何区分性信息，
        # 判不了就别在这里拦，交给阶段二的 LLM（它至少能读懂页面内容）。
        return True

    # 本土文字品牌直接出现在标题里也算命中（拉丁转写可能对不上）
    if brand_native and brand_native in (title or ""):
        return True

    # 品牌自己的域名：标题可能只写 "Drinking Yoghurt"，不该因此误杀
    host = re.sub(r"[^a-z0-9]", "", (url or "").lower().split("/")[2] if "//" in (url or "") else "")
    brand_key = re.sub(r"[^a-z0-9]", "", (brand_latin or "").lower())
    if brand_key and len(brand_key) > 2 and brand_key in host:
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
        if not _has_overlap(h.title, brand, product_name, h.url):
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
# 每份口径 —— Annex 4 的 8/23（餐食）与 9（健康零食）按 per serve 判
# --------------------------------------------------------------------------- #
# 这一段是 Day6 新增。此前整条链路只有 per-100g 一个口径，
# 于是 8/23/9 的官方判据在系统里**无法执行**：拿 per-100g 去比 "900mg /serve"
# 是把两个不同分母的数字放在一起比，比出来的对错都是假的。
# `nutrient_rules` 因此规定：份量不明就转人工，不许用 per-100g 顶替。
KCAL_TO_KJ = 4.184

_SERVING_PATTERNS = (
    r"serving\s*size\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(g|ml)\b",
    r"per\s*serve\s*\(?\s*(\d+(?:\.\d+)?)\s*(g|ml)\s*\)?",
    r"per\s*serving\s*\(?\s*(\d+(?:\.\d+)?)\s*(g|ml)\s*\)?",
    r"\(\s*(\d+(?:\.\d+)?)\s*(g|ml)\s*per\s*serv",
    r"每份\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(克|g|毫升|ml)",
)


def parse_serving_size(text: str) -> float | None:
    """从页面文本里读份量（g 或 ml，两者按 1:1 处理）。读不出返回 None —— 不猜。"""
    t = (text or "").lower()
    for pat in _SERVING_PATTERNS:
        m = re.search(pat, t)
        if m:
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if 0 < v < 5000:            # 明显不合理的份量当没读到
                return v
    return None


def basis_of(unit: str) -> str:
    """把原始单位串归成 Basis 字面量：per_100g / per_100ml / per_serve / unknown。"""
    u = (unit or "").strip().lower().replace(" ", "")
    if "/100ml" in u or "per100ml" in u:
        return "per_100ml"
    if "/100g" in u or "per100g" in u:
        return "per_100g"
    if "serving" in u or "serve" in u or "portion" in u:
        return "per_serve"
    if re.search(r"/(\d+(?:\.\d+)?)(g|ml)\b|per(\d+(?:\.\d+)?)(g|ml)", u):
        return "per_serve"          # 显式写了非 100 的分母，本质就是"每 N 克"
    if not u or u in MASS_FACTOR or u.startswith(("kj", "kcal", "cal")):
        return "unknown"
    return "unknown"


def to_kj(value: float, unit: str) -> float | None:
    """能量统一到 kJ（Annex 4 的 9 用 kJ）。kcal 按 ×4.184 换算。"""
    if value is None:
        return None
    u = (unit or "").strip().lower().replace(" ", "")
    if u.startswith("kj"):
        return round(value, 2)
    if u.startswith(("kcal", "cal")):
        return round(value * KCAL_TO_KJ, 2)
    return None


def per_serve_value(
    value: float, unit: str, serving_size_g: float | None, normalized: float | None
) -> float | None:
    """算每份绝对量。两条路：单位本来就是每份 → 直接用；否则由 per-100g × 份量/100 推。

    份量未知且单位不是每份 → None。**这是有意的**：宁可让 8/23/9 转人工，
    也不拿一个分母不对的数字去卡 Annex 4 的线。
    """
    if value is None:
        return None
    u = (unit or "").strip().lower().replace(" ", "")

    numerator = 1.0
    for token, factor in MASS_FACTOR.items():
        if u.startswith(token):
            numerator = factor
            break

    if basis_of(unit) == "per_serve":
        m = re.search(r"/(\d+(?:\.\d+)?)(g|ml)\b|per(\d+(?:\.\d+)?)(g|ml)", u)
        if m and normalized is not None and serving_size_g:
            # 单位写的是"每 N 克"而份量是另一个数：以份量为准，从 per-100g 推回去
            return round(normalized * serving_size_g / 100.0, 4)
        return round(value * numerator, 4)

    if normalized is not None and serving_size_g and serving_size_g > 0:
        return round(normalized * serving_size_g / 100.0, 4)
    return None


def _nutrient_value(
    nutrient: Nutrient,
    value: float,
    unit: str,
    *,
    serving_size_g: float | None = None,
    normalized: float | None = None,
    confidence: float = 0.8,
) -> NutrientValue:
    """构造 NutrientValue，统一补齐 basis / normalized / per_serve / serving_size_g。

    所有抽取路径（LLM / 规则 / 缓存回填）都必须走这里，
    否则又会出现"某条路径忘了填 per_serve，8/23 静默失效"这种问题。
    """
    if nutrient == "energy_kj":
        kj = to_kj(value, unit)
        per_100 = kj if basis_of(unit) in ("per_100g", "per_100ml", "unknown") else None
        if basis_of(unit) == "per_serve":
            ps = kj
        elif per_100 is not None and serving_size_g:
            ps = round(per_100 * serving_size_g / 100.0, 2)
        else:
            ps = None
        return NutrientValue(
            nutrient=nutrient, value=value, unit=unit, basis=basis_of(unit),
            normalized=per_100, per_serve=ps, serving_size_g=serving_size_g,
            confidence=confidence,
        )

    if normalized is None:
        normalized = normalize(value, unit, serving_size_g)
    return NutrientValue(
        nutrient=nutrient,
        value=value,
        unit=unit,
        basis=basis_of(unit),
        normalized=normalized,
        per_serve=per_serve_value(value, unit, serving_size_g, normalized),
        serving_size_g=serving_size_g,
        confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# 阶段二 · 抽取
# --------------------------------------------------------------------------- #
EXTRACT_SYSTEM_PROMPT = """You are a nutrition fact extractor. Extract nutrition data for the
specified product from the given web snippets.
Rules:
1. Extract ONLY values explicitly present in the snippet. Never guess or fill in missing nutrients.
2. Keep the original unit as-is; also provide `normalized` in g/100g (solids) or g/100ml (liquids).
   If serving-size info is missing, set normalized to null.
3. Report `serving_size_g` per item whenever the page states a serving/portion size
   (e.g. "Serving size: 30g", "per serve (250ml)"). Set it to null if the page does not say.
   This matters: some official cut-offs are defined PER SERVE, and without the serving size
   the item cannot be judged at all.
4. Extract `saturated_fat` and `energy_kj` when present — they are separate from total `fat`.
   For energy, keep the original unit ("kJ" or "kcal"); do not convert.
5. If a snippet is about a different product (same name, different variant or pack size with
   materially different values), return match=false for it.
6. Snippets may mix English with Hindi/Bengali/Urdu/Sinhala/Tamil — extract from whichever
   language carries the nutrition panel; do not translate values.
7. Output strict JSON only.

JSON schema:
{"items":[{"index":int,"match":bool,"serving_size_g":number|null,
"nutrients":[{"nutrient":"sugar|fat|saturated_fat|fiber|sodium|protein|energy_kj",
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
_ENERGY_UNIT = r"\s*(kj|kcal|cal)\b"
RULE_PATTERNS: dict[Nutrient, list[str]] = {
    "sugar": [rf"sugars?\D{{0,20}}{_NUM}{_UNIT}"],
    # `fat` 必须排掉 saturated/trans，否则"saturated fat 6g"会被当成总脂肪读走
    "fat": [rf"(?<!saturated\s)(?<!trans\s)fat\D{{0,20}}{_NUM}{_UNIT}"],
    "saturated_fat": [
        rf"saturated\s*(?:fatty\s*acids?|fat)\D{{0,20}}{_NUM}{_UNIT}",
        rf"\bsat\.?\s*fat\D{{0,20}}{_NUM}{_UNIT}",
    ],
    "fiber": [rf"fib(?:re|er)\D{{0,20}}{_NUM}{_UNIT}"],
    "sodium": [rf"sodium\D{{0,20}}{_NUM}{_UNIT}", rf"salt\D{{0,20}}{_NUM}{_UNIT}"],
    "protein": [rf"protein\D{{0,20}}{_NUM}{_UNIT}"],
    "energy_kj": [rf"energy\D{{0,20}}{_NUM}{_ENERGY_UNIT}"],
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
    serving = parse_serving_size(text)
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
                _nutrient_value(
                    nutrient, value, unit,
                    serving_size_g=serving, confidence=0.75,
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
        # 份量优先信模型抽的，抽不到再从原文正则兜一次 —— 缺它 8/23/9 就判不了
        serving = item.get("serving_size_g")
        try:
            serving = float(serving) if serving is not None else None
        except (TypeError, ValueError):
            serving = None
        if not serving or serving <= 0:
            serving = parse_serving_size(f"{hit.title}\n{hit.snippet}")

        nutrients: list[NutrientValue] = []
        for nv in item.get("nutrients", []):
            if nv.get("nutrient") not in NUTRIENTS:
                continue
            value, unit = float(nv["value"]), str(nv.get("unit", ""))
            nutrients.append(
                _nutrient_value(
                    nv["nutrient"], value, unit,
                    serving_size_g=serving,
                    normalized=nv.get("normalized"),
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
    labels = {
        "sugar": "糖", "fat": "脂肪", "saturated_fat": "饱和脂肪", "fiber": "纤维",
        "sodium": "钠", "protein": "蛋白", "energy_kj": "能量",
    }
    parts: list[str] = []
    for n in NUTRIENTS:
        v = next((e.get(n) for e in evidence if e.get(n) is not None), None)
        if v is not None:
            unit = "kJ/100" if n == "energy_kj" else "g/100"
            parts.append(f"{labels[n]} {v}{unit}")
    # per-serve 是 Annex 4 判 8/23/9 的口径，摘要里必须能看到有没有份量
    serve = next(
        (nv.serving_size_g for e in evidence for nv in e.nutrients if nv.serving_size_g),
        None,
    )
    parts.append(f"份量 {serve}g" if serve else "份量未知（8/23/9 判不了）")
    return "、".join(parts) or "未抽到可用 normalized 值"
