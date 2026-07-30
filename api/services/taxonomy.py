"""33 细类 / 12 大类 taxonomy 定义 → system prompt 拼装。

方案 §4 明确：分类标准本身**不用 RAG**，一两千 token 全量进 system prompt。
本模块是 taxonomy 的唯一事实来源，vlm.py / adjudicate / eval 全部从这里取。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpecificCategory:
    code: int           # 官方编号（1–34，22 已并入 32）
    name: str
    general: str        # 所属大类名


# 12 大类（顺序即官方顺序）
GENERAL_CATEGORIES: list[str] = [
    "1. Grains and starches",
    "2. Fruits and vegetables",
    "3. Dairy and alternatives",
    "4. Meats and alternatives",
    "5. Snacks and desserts",
    "6. Meals and prepared foods",
    "7. Beverages",
    "8. Oil",
    "9. Fast food (burgers) and restaurants",
    "10. Baby foods",
    "11. Recipe additions",
    "12. Vitamin",
]

_G = GENERAL_CATEGORIES

# 33 细类。注意：类别 22 已并入 32（产品范围高度重叠）。
SPECIFIC_CATEGORIES: list[SpecificCategory] = [
    SpecificCategory(1, "Breads, rice, noodles without additive, plain starch products; plain biscuits and crackers.", _G[0]),
    SpecificCategory(2, "Low sugar and high fibre cereals.", _G[0]),
    SpecificCategory(12, "High sugar and/or low fibre cereals.", _G[0]),
    SpecificCategory(13, "Flavoured/fried instant rice and noodle.", _G[0]),
    SpecificCategory(14, "Sweet breads, cakes, muffins, sweet biscuits, high fat savoury biscuits, pies and pastries; sweet sticky rice or rice pudding.", _G[0]),
    SpecificCategory(3, "Fruits and fruit products without additives.", _G[1]),
    SpecificCategory(4, "Vegetables and vegetable products without additives, plain seaweed.", _G[1]),
    SpecificCategory(5, "Milks and yoghurts (low fat), cheese (low fat) and their alternatives.", _G[2]),
    SpecificCategory(19, "Full cream milks and yoghurts, high fat cheese and their alternatives.", _G[2]),
    SpecificCategory(30, "Baby and toddler milk formulae.", _G[2]),
    SpecificCategory(6, "Meat and alternatives, poultry, legumes, tofu, eggs, raw unsalted nuts.", _G[3]),
    SpecificCategory(15, "Processed meats and alternatives (preserved in salt).", _G[3]),
    SpecificCategory(9, "Healthy snacks based on core foods.", _G[4]),
    SpecificCategory(16, "Sweet snack foods.", _G[4]),
    SpecificCategory(17, "Savoury snack foods with added salt or fat.", _G[4]),
    SpecificCategory(20, "Ice cream, iced confection and desserts.", _G[4]),
    SpecificCategory(21, "Chocolate and candy.", _G[4]),
    SpecificCategory(8, "Low fat/salt meals (includes frozen or packaged meals, soups, sandwiches, salads, steamed buns).", _G[5]),
    SpecificCategory(23, "High fat/salt meals (includes prepared meals with higher content of fats or salts).", _G[5]),
    SpecificCategory(24, "Other high fat/salt products (like meat/fish/bean pastes, high fat sauces).", _G[5]),
    SpecificCategory(11, "Bottled water (include unflavoured mineral and soda waters).", _G[6]),
    SpecificCategory(18, "Fruit juice/drinks.", _G[6]),
    SpecificCategory(25, "Sugar sweetened drinks.", _G[6]),
    SpecificCategory(26, "Alcohol.", _G[6]),
    SpecificCategory(29, "Tea and coffee (excluding sweetened powder-based).", _G[6]),
    SpecificCategory(7, "Oils high in mono- or polyunsaturated fats, and low fat savoury sauces.", _G[7]),
    SpecificCategory(31, "Fast food (healthier options).", _G[8]),
    SpecificCategory(32, "Fast food (general, includes unhealthy options). [category 22 merged in]", _G[8]),
    SpecificCategory(33, "Fast-food restaurant (NO foods or drinks advertised).", _G[8]),
    SpecificCategory(34, "Local restaurant.", _G[8]),
    SpecificCategory(10, "Baby foods.", _G[9]),
    SpecificCategory(27, "Recipe additions (including soup cubes, oils, dried herbs and seasonings).", _G[10]),
    SpecificCategory(28, "Vitamin/mineral or other dietary supplements.", _G[11]),
]

BY_CODE: dict[int, SpecificCategory] = {c.code: c for c in SPECIFIC_CATEGORIES}
CODES: set[int] = set(BY_CODE)
GENERAL_TO_CODES: dict[str, list[int]] = {
    g: [c.code for c in SPECIFIC_CATEGORIES if c.general == g] for g in GENERAL_CATEGORIES
}

# eval 重点关注的混淆对（方案 §7 指标 6）
CONFUSING_PAIRS: list[tuple[int, int]] = [(2, 12), (5, 19), (8, 23)]


def is_valid(code: int | None) -> bool:
    return code in CODES


def general_of(code: int) -> str | None:
    c = BY_CODE.get(code)
    return c.general if c else None


def taxonomy_block() -> str:
    """taxonomy 全量文本，直接嵌进 system prompt。"""
    lines: list[str] = []
    for g in GENERAL_CATEGORIES:
        lines.append(f"\n{g}")
        for code in GENERAL_TO_CODES[g]:
            lines.append(f"  [{code}] {BY_CODE[code].name}")
    return "\n".join(lines).strip()


CLASSIFY_SYSTEM_PROMPT = """You are a nutrition-policy analyst auditing online food advertisements.

Classify the advertised product into the official taxonomy below: 12 general
categories and 33 specific categories. Category 22 has been merged into 32.

TAXONOMY
{taxonomy}

RULES
1. Classify the product being ADVERTISED, not incidental items in the image.
2. If the ad shows a restaurant/brand with no specific food or drink, use [33].
3. Output the specific category code exactly as listed; never invent a code.
4. Report confidence honestly. Low confidence is useful information, not failure:
   downstream the system will search the web for nutrition evidence.
5. Report `general_confidence` separately from `specific_confidence`. It is normal
   and expected to be sure of the general category (e.g. "it is a cereal") while
   being unsure of the specific one (e.g. "high sugar [12] vs low sugar [2]").
6. Extract `product_name` and `brand` verbatim from the image when legible; these
   are the search anchors. Set them to null if truly not readable — do NOT guess.
7. `reasoning` must cite VISIBLE evidence (pack text, nutrition claims, imagery),
   not general world knowledge.

Return ONLY a JSON object with this exact shape:
{{
  "product_name": string | null,
  "brand": string | null,
  "general_category": string,        // one of the 12 general category labels
  "specific_code": integer,          // one of the 33 valid codes
  "specific_confidence": number,     // 0.0–1.0
  "general_confidence": number,      // 0.0–1.0
  "reasoning": string,               // <= 60 words, cite visible evidence
  "alternative_code": integer | null,// the runner-up category, if any
  "name_or_brand_legible": boolean
}}"""


ADJUDICATE_SYSTEM_PROMPT = """You are re-adjudicating a food advertisement classification
using nutrition evidence retrieved from the web or from the product knowledge cache.

TAXONOMY
{taxonomy}

You are given: (a) the initial visual classification, (b) structured nutrition
evidence (sugar / fat / fibre / salt per 100g or 100ml, with source URLs).

RULES
1. Every claim in `reasoning` must be grounded in the supplied evidence. If the
   evidence does not settle the question, say so and lower your confidence.
2. Thresholds matter most for the known confusion pairs: [2] vs [12] (cereal sugar
   and fibre), [5] vs [19] (dairy fat), [8] vs [23] (meal fat/salt).
3. Never fabricate nutrition numbers. Cite the evidence index you relied on.
4. Even when the general category was already certain, you must land on a SPECIFIC
   code — granularity-adaptive output resolves back to the leaf in the end.

Return ONLY a JSON object with the same shape as the initial classification, plus:
  "evidence_refs": [integer],   // indices of evidence items actually used
  "conflict": boolean           // true if sources disagree with each other
"""


def build_classify_prompt(few_shots: list[str] | None = None) -> str:
    prompt = CLASSIFY_SYSTEM_PROMPT.format(taxonomy=taxonomy_block())
    if few_shots:
        joined = "\n\n".join(few_shots)
        prompt += (
            "\n\nCORRECTED EXAMPLES (previous human corrections on similar ads — "
            "follow the corrected judgement):\n" + joined
        )
    return prompt


def build_adjudicate_prompt() -> str:
    return ADJUDICATE_SYSTEM_PROMPT.format(taxonomy=taxonomy_block())
