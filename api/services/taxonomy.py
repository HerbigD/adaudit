"""Taxonomy 单一事实来源 —— 从 `api/data/taxonomy.json` 加载，不在代码里另造数据。

产出两份东西（对应 taxonomy.json 的 meta.usage）：
① `classify_initial` 的 system prompt 文本块（全量进 prompt，不做 RAG，≤2000 token）
② 前端两级级联选择器数据（`GET /api/taxonomy`）

当前数据源为 `v1.0-codebook`（逐条对照原始 codebook 重写，33 条全部 `confirmed=true`）。
加载时仍会对任何 `confirmed=false` 的条目发 warning，并在 `/api/health` 暴露 `confirmed_ratio`。
v0.9-draft 时期的差异清单已归档在 `docs/archive/taxonomy_conflicts_resolved.md`。

HFSS 归属**不从名称推**，见本文件下半部分的 `HFSS_VERDICTS` 判定表与其上的说明。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 历史数据里的 22 号按 32 处理（taxonomy.json 中 stable_code 32 的 merge_note）
LEGACY_CODE_MAP: dict[int, int] = {22: 32}


@dataclass(frozen=True)
class GeneralCategory:
    id: int
    name_en: str
    name_zh: str

    @property
    def label(self) -> str:
        return f"{self.id}. {self.name_en}"


@dataclass(frozen=True)
class SpecificCategory:
    stable_code: int
    parent_id: int
    name_zh: str
    name_en: str
    description_zh: str = ""
    key_dimensions: tuple[str, ...] = ()
    evidence_needed: tuple[str, ...] = ()
    confusable_with: tuple[int, ...] = ()
    confirmed: bool = False
    merge_note: str | None = None

    @property
    def needs_evidence(self) -> bool:
        """最终判定依赖营养数据 —— 视觉不确定时应给低置信并保留名称/品牌提取。"""
        return bool(self.evidence_needed)


@dataclass
class Taxonomy:
    version: str
    updated: str
    generals: dict[int, GeneralCategory]
    specifics: dict[int, SpecificCategory]
    confusing_pairs: tuple[tuple[int, int], ...]
    pair_notes: dict[tuple[int, int], str] = field(default_factory=dict)

    # ---------- 查询 ----------
    @property
    def codes(self) -> set[int]:
        return set(self.specifics)

    def children_of(self, general_id: int) -> list[int]:
        return [c for c, s in self.specifics.items() if s.parent_id == general_id]

    def is_valid(self, code: Any) -> bool:
        return isinstance(code, int) and code in self.specifics

    def normalize(self, code: Any) -> int | None:
        """把历史编号（22）映射到现行编号，非法返回 None。"""
        if isinstance(code, str) and code.strip().lstrip("[").rstrip("]").isdigit():
            code = int(code.strip().lstrip("[").rstrip("]"))
        if not isinstance(code, int):
            return None
        code = LEGACY_CODE_MAP.get(code, code)
        return code if code in self.specifics else None


def _tuple(x: Any) -> tuple:
    return tuple(x or ())


@lru_cache(maxsize=1)
def load() -> Taxonomy:
    from config import settings

    path = Path(settings.taxonomy_path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    generals = {
        g["id"]: GeneralCategory(id=g["id"], name_en=g["name_en"], name_zh=g["name_zh"])
        for g in raw["general_categories"]
    }
    specifics: dict[int, SpecificCategory] = {}
    for s in raw["specific_categories"]:
        specifics[s["stable_code"]] = SpecificCategory(
            stable_code=s["stable_code"],
            parent_id=s["parent_id"],
            name_zh=s["name_zh"],
            name_en=s["name_en"],
            description_zh=s.get("description_zh", ""),
            key_dimensions=_tuple(s.get("key_dimensions")),
            evidence_needed=_tuple(s.get("evidence_needed")),
            confusable_with=_tuple(s.get("confusable_with")),
            confirmed=bool(s.get("confirmed", False)),
            merge_note=s.get("merge_note"),
        )

    pairs: list[tuple[int, int]] = []
    notes: dict[tuple[int, int], str] = {}
    for p in raw.get("confusing_pairs", []):
        a, b = sorted(p["pair"])
        pairs.append((a, b))
        notes[(a, b)] = f"{p.get('dimension', '')}｜{p.get('note', '')}".strip("｜")

    tx = Taxonomy(
        version=raw["meta"].get("version", "unknown"),
        updated=raw["meta"].get("updated", ""),
        generals=generals,
        specifics=specifics,
        confusing_pairs=tuple(pairs),
        pair_notes=notes,
    )
    _validate(tx)
    return tx


def _validate(tx: Taxonomy) -> None:
    """加载期硬校验 —— 数据错了要在启动时炸，而不是在跑批第 300 张时才发现。"""
    if len(tx.specifics) != 33:
        raise ValueError(f"taxonomy.json 细类数应为 33，实际 {len(tx.specifics)}")
    if len(tx.generals) != 12:
        raise ValueError(f"taxonomy.json 大类数应为 12，实际 {len(tx.generals)}")
    orphans = [c for c, s in tx.specifics.items() if s.parent_id not in tx.generals]
    if orphans:
        raise ValueError(f"细类父类 id 不存在: {orphans}")
    dangling = {
        c: [x for x in s.confusable_with if x not in tx.specifics]
        for c, s in tx.specifics.items()
        if any(x not in tx.specifics for x in s.confusable_with)
    }
    if dangling:
        raise ValueError(f"confusable_with 指向不存在的编号: {dangling}")

    # HFSS 判定表必须覆盖全部编号：新增类别时强制做一次政策判断，不许静默漏掉
    missing = sorted(set(tx.specifics) - set(HFSS_VERDICTS))
    if missing:
        raise ValueError(
            f"HFSS_VERDICTS 缺少这些 stable_code 的判定: {missing}（见 services/taxonomy.py）"
        )
    stale = sorted(set(HFSS_VERDICTS) - set(tx.specifics))
    if stale:
        logger.warning("HFSS_VERDICTS 里有 taxonomy 已不存在的编号: %s", stale)

    # key_dimensions 必须能映射成英文：漏一个就意味着中文会漏进 model 侧 prompt
    unmapped = sorted(
        {d for s in tx.specifics.values() for d in s.key_dimensions if d not in _DIM_ABBR}
    )
    if unmapped:
        raise ValueError(
            f"_DIM_ABBR 缺少这些 key_dimensions 的英文映射: {unmapped}（见 services/taxonomy.py）"
        )

    # 每个混淆对都要有判定维度，否则 Day5 的冲突判定对它形同虚设
    uncovered = [p for p in tx.confusing_pairs if p not in PAIR_NUTRIENTS]
    if uncovered:
        raise ValueError(
            f"PAIR_NUTRIENTS 缺少这些混淆对的判定维度: {uncovered}（见 services/taxonomy.py）"
        )

    unconfirmed = [c for c, s in tx.specifics.items() if not s.confirmed]
    if unconfirmed:
        logger.warning(
            "taxonomy.json v%s：%d/%d 个细类名称仍为草案（confirmed=false），"
            "接真实 VLM 前需用原项目 codebook 核对，"
            "历史差异清单见 docs/archive/taxonomy_conflicts_resolved.md",
            tx.version,
            len(unconfirmed),
            len(tx.specifics),
        )


# --------------------------------------------------------------------------- #
# 模块级便捷访问（旧调用方保持可用）
# --------------------------------------------------------------------------- #
def is_valid(code: Any) -> bool:
    return load().is_valid(code)


def normalize(code: Any) -> int | None:
    return load().normalize(code)


def get(code: int) -> SpecificCategory | None:
    return load().specifics.get(code)


def general_id_of(code: int) -> int | None:
    s = load().specifics.get(code)
    return s.parent_id if s else None


def general_label(general_id: int) -> str:
    g = load().generals.get(general_id)
    return g.label if g else f"{general_id}. (unknown)"


def general_of(code: int) -> str | None:
    """细类编号 → 大类展示名（`3. Dairy and alternatives` 形式）。"""
    gid = general_id_of(code)
    return general_label(gid) if gid else None


def confusing_pairs() -> tuple[tuple[int, int], ...]:
    return load().confusing_pairs


def is_confusing_pair(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return False
    return tuple(sorted((a, b))) in set(load().confusing_pairs)


# --------------------------------------------------------------------------- #
# HFSS 判定表
# --------------------------------------------------------------------------- #
# 曾经用名称正则从 taxonomy.json 推导，v1.0-codebook 替换后人工核对发现三类硬伤：
#   · [7] 高不饱和脂肪油脂与低脂咸味酱 —— 被"咸味"误命中，实际是健康油脂类
#   · [29] 茶与咖啡（不含甜味粉剂冲调）—— 被"甜味"误命中，名称里那是**否定**语义
#   · [13] 调味/油炸即食米饭面条 —— 名称不含风险词，实际是高脂高盐，被漏掉
# 正则读不懂否定和语义，所以这里改成**显式判定表**：每个 stable_code 一行结论 + 一句依据，
# 可审、可 diff、可在答辩时逐条解释。taxonomy.json 仍是分类数据的唯一来源，
# HFSS 归属是叠加在它之上的一层**政策判断**，本就不该从名称字符串里猜。
#
# 覆盖性由 `_validate` 强制：taxonomy 里出现新编号而这张表没写，启动即报错。
HFSS_VERDICTS: dict[int, tuple[bool, str]] = {
    1:  (False, "无添加谷物主食与原味饼干，核心食物"),
    2:  (False, "低糖高纤维谷物"),
    3:  (False, "无添加水果"),
    4:  (False, "无添加蔬菜、原味海苔"),
    5:  (False, "低脂奶制品"),
    6:  (False, "未加工肉禽豆蛋、原味无盐坚果"),
    7:  (False, "高不饱和脂肪油脂与低脂咸味酱 —— codebook 里这是健康向油脂类"),
    8:  (False, "低脂/低盐餐食"),
    9:  (False, "基于核心食物的健康零食"),
    10: (False, "婴幼儿食品，单独监管口径"),
    11: (False, "瓶装水"),
    12: (True,  "高糖和/或低纤维谷物"),
    13: (True,  "调味/油炸即食米饭面条 —— 油炸+调味，证据维度即钠与脂肪"),
    14: (True,  "甜味烘焙与高脂咸味烘焙"),
    15: (True,  "盐腌加工肉制品，高钠"),
    16: (True,  "甜味零食"),
    17: (True,  "加盐/加脂咸味零食"),
    18: (False, "果汁/果汁饮料 —— codebook 与含糖饮料(25)分列，此处按非 HFSS 计；"
                "若课题采用的营养分级模型把果汁计入游离糖，改这一行即可"),
    19: (True,  "全脂奶/酸奶、高脂奶酪"),
    20: (True,  "冰淇淋、冰品与甜点"),
    21: (True,  "巧克力与糖果"),
    23: (True,  "高脂/高盐餐食"),
    24: (True,  "其他高脂/高盐制品（酱料类）"),
    25: (True,  "含糖饮料"),
    26: (False, "酒精 —— 受广告监管但不属于高糖/高脂/高盐口径，单独统计（见 alcohol_codes）"),
    27: (False, "烹饪添加物，用量口径不同"),
    28: (False, "维生素/膳食补充剂"),
    29: (False, "茶与咖啡，名称已明确排除甜味粉剂冲调"),
    30: (False, "婴幼儿配方奶粉，单独监管口径"),
    31: (False, "快餐健康选项"),
    32: (True,  "快餐常规，含不健康选项"),
    33: (False, "仅餐厅品牌，无食品可判"),
    34: (False, "本地餐厅，无具体食品"),
}

# 受广告监管但不计入 HFSS 的类别，报告里分开列
ALCOHOL_CODES: frozenset[int] = frozenset({26})


# --------------------------------------------------------------------------- #
# 混淆对 → 判定维度（Day5 §6 冲突判定条件 3）
# --------------------------------------------------------------------------- #
# 同样是**显式表**而不是从 key_dimensions 的中文串猜（教训见 HFSS_VERDICTS 上面那段）。
# 覆盖性由 `_validate` 强制：taxonomy.json 新增混淆对而这里没写，启动即报错。
PAIR_NUTRIENTS: dict[tuple[int, int], tuple[str, ...]] = {
    (2, 12): ("sugar", "fiber"),
    (5, 19): ("fat",),
    (8, 23): ("fat", "sodium"),
    (16, 17): ("sugar", "sodium"),
    (18, 25): ("sugar",),
    (25, 29): ("sugar",),
}


def pair_nutrients(codes: Any = None) -> tuple[str, ...]:
    """给定一组候选细类编号，返回它们所处混淆对的判定维度（去重）。

    codes 为 None 或没有任何一对完整落在其中时返回空元组 —— 冲突判定随之关闭，
    这是有意的：条件 3 要求"恰好落在目标混淆对的判定维度上"。
    """
    if not codes:
        return ()
    have = {c for c in codes if isinstance(c, int)}
    out: list[str] = []
    for (a, b), dims in PAIR_NUTRIENTS.items():
        if a in have and b in have:
            out.extend(d for d in dims if d not in out)
    return tuple(out)


@lru_cache(maxsize=1)
def hfss_codes() -> frozenset[int]:
    """高糖/高脂/高盐相关细类。判定依据见 HFSS_VERDICTS 每行的注释。"""
    return frozenset(c for c, (is_hfss, _) in HFSS_VERDICTS.items() if is_hfss)


def alcohol_codes() -> frozenset[int]:
    return ALCOHOL_CODES


def hfss_table() -> list[dict[str, Any]]:
    """给报告/复核页用的可读判定表。"""
    tx = load()
    return [
        {
            "code": c,
            "name_zh": tx.specifics[c].name_zh if c in tx.specifics else "(已移除)",
            "hfss": v,
            "rationale": why,
        }
        for c, (v, why) in sorted(HFSS_VERDICTS.items())
    ]


# --------------------------------------------------------------------------- #
# ① system prompt 文本块
# --------------------------------------------------------------------------- #
# taxonomy.json 的 key_dimensions 是中文（给人看的）；模型侧一律英文，所以这里映射。
# **不做兜底透传**：映射缺失会在 `_validate` 里报错，否则中文会悄悄漏进 prompt。
_DIM_ABBR = {
    "糖": "sugar",
    "含糖量": "sugar",
    "纤维": "fibre",
    "脂": "fat",
    "脂肪": "fat",
    "脂肪含量": "fat",
    "脂肪酸类型": "fat-type",
    "盐": "salt",
    "钠": "sodium",
    "加工方式": "processing",
    "调味": "flavoured",
    "油炸": "fried",
    "添加剂": "additives",
    "是否有添加剂": "additives",
    "是否腌制加工": "cured",
    "原料基底": "base",
    "婴配": "infant-formula",
    "水果 vs 蔬菜": "fruit-vs-veg",
    "果汁含量": "juice%",
    "果汁浓度": "juice%",
    "乳含量": "milk%",
    "酒精": "alcohol",
    "健康选项": "healthier?",
    "仅餐厅无食品": "venue-only",
}


def _dims(s: SpecificCategory) -> str:
    if not s.key_dimensions:
        return ""
    return ",".join(_DIM_ABBR[d] for d in s.key_dimensions if d in _DIM_ABBR)


def taxonomy_block() -> str:
    """33 类全量清单。行格式刻意压缩到一行一类，控制 token。

    **模型侧一律英文**（Day5 §9 软约定 1）：codebook 本来就是英文标准，
    英文 prompt 还省一道翻译失真。中文名只留在 UI 展示层（`cascade()`）。

    `*` 标记 evidence_needed 非空 —— 提示模型这些类别的最终判定依赖营养数据，
    视觉不确定时应给低置信度并保留名称/品牌提取。
    """
    tx = load()
    lines: list[str] = []
    for gid in sorted(tx.generals):
        g = tx.generals[gid]
        lines.append(f"\n{g.id}. {g.name_en}")
        for code in sorted(tx.children_of(gid)):
            s = tx.specifics[code]
            star = "*" if s.needs_evidence else " "
            dims = _dims(s)
            tail = f" [{dims}]" if dims else ""
            lines.append(f" {star}[{code}] {s.name_en}{tail}")
    return "\n".join(lines).strip()


def confusing_pairs_block() -> str:
    tx = load()
    parts = []
    for a, b in tx.confusing_pairs:
        dim = tx.pair_notes.get((a, b), "").split("｜")[0]
        parts.append(f"[{a}]vs[{b}]({dim})")
    return "; ".join(parts)


CLASSIFY_SYSTEM_PROMPT = """You audit online food advertisements for nutrition policy compliance.
Classify the ADVERTISED product into the taxonomy below: 12 general categories, 33 specific
codes (stable_code; historical code 22 is merged into 32).

TAXONOMY  (`*` = final call depends on nutrition data, not looks; [..] = key dimensions)
{taxonomy}

VISUALLY CONFUSABLE PAIRS — never force a pick, report honest confidence:
{pairs}

RULES
1. Classify what is being advertised, not incidental items.
2. Use only codes listed above. Never invent one.
3. Report `general_confidence` and `specific_confidence` SEPARATELY. Being sure of the parent
   ("it is a cereal") while unsure of the leaf ("high-sugar [12] vs low-sugar [2]") is the
   expected outcome for `*` categories, not a failure.
4. GRANULARITY-ADAPTIVE OUTPUT: if you cannot settle the leaf but are confident of the parent,
   set "leaf_vs_parent":"parent", set "specific_code":null, and list the leaf candidates you
   are torn between in "candidate_codes". State in `reasoning` which nutrition figure would
   settle it (e.g. "sugar per 100g decides [2] vs [12]").
5. Extract `product_name` and `brand` verbatim when legible — they are the search anchors that
   let the system retrieve nutrition data later. Null them out if truly unreadable; do NOT guess.
   If the brand appears in BOTH a Latin transliteration and a local script, keep both in `brand`.
6. `reasoning` (<= 50 words) must cite VISIBLE evidence: pack text, claims, imagery.
7. Report `ad_language` (ISO 639-1) — the dominant language of the on-image text; when mixed,
   pick the one carrying the most information. Report `country` (ISO 3166-1 alpha-2) inferred
   from brand, script, currency symbols or spokesperson; use null when you cannot tell.
   These two drive how the system searches for nutrition data later — guessing hurts.

Return ONLY this JSON object:
{{
  "product_name": string|null,
  "brand": string|null,
  "name_brand_identifiable": boolean,
  "ad_language": string,            // ISO 639-1, e.g. en/hi/bn/ur/si/ta
  "country": string|null,           // ISO 3166-1 alpha-2, e.g. IN/BD/PK/LK
  "general_id": integer,            // 1-12
  "specific_code": integer|null,    // null when leaf_vs_parent=="parent"
  "candidate_codes": [integer],     // leaf candidates when unsure; [] otherwise
  "leaf_vs_parent": "leaf"|"parent",
  "specific_confidence": number,    // 0.0-1.0
  "general_confidence": number,     // 0.0-1.0
  "reasoning": string
}}"""

ADJUDICATE_SYSTEM_PROMPT = """You re-adjudicate a food advertisement classification using
nutrition evidence retrieved from the web or from the product knowledge cache.

TAXONOMY
{taxonomy}

CONFUSABLE PAIRS (this is where evidence matters most):
{pairs}

RULES
1. Every claim in `reasoning` must be grounded in the supplied evidence; cite the evidence index.
2. Never fabricate nutrition numbers. If evidence does not settle it, lower your confidence.
3. You MUST land on a specific leaf code — granularity-adaptive output resolves back to the leaf
   at this stage. Only if evidence is absent or contradictory may you keep "leaf_vs_parent":"parent".
4. Set "conflict": true when sources disagree with each other.

Return ONLY the same JSON object as the initial classification, plus:
  "evidence_refs": [integer],
  "conflict": boolean
"""


def build_classify_prompt(few_shots: list[str] | None = None) -> str:
    prompt = CLASSIFY_SYSTEM_PROMPT.format(
        taxonomy=taxonomy_block(), pairs=confusing_pairs_block()
    )
    if few_shots:
        prompt += (
            "\n\nCORRECTED EXAMPLES (human corrections on similar ads — follow the correction):\n"
            + "\n".join(few_shots)
        )
    return prompt


def build_adjudicate_prompt() -> str:
    return ADJUDICATE_SYSTEM_PROMPT.format(
        taxonomy=taxonomy_block(), pairs=confusing_pairs_block()
    )


# --------------------------------------------------------------------------- #
# token 计量（验收项：taxonomy prompt 块 ≤ 2000 token）
# --------------------------------------------------------------------------- #
_CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")


def count_tokens(text: str) -> int:
    """有 tiktoken 且能加载编码表就用它；否则用保守启发式（CJK 1 字 1 token）。"""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:  # noqa: BLE001 — 离线环境下载不到 BPE 表，降级
        cjk = len(_CJK.findall(text))
        rest = len(text) - cjk
        return cjk + (rest + 3) // 4


def token_report() -> dict[str, int]:
    block = taxonomy_block()
    return {
        "taxonomy_block": count_tokens(block),
        "classify_prompt": count_tokens(build_classify_prompt()),
        "adjudicate_prompt": count_tokens(build_adjudicate_prompt()),
    }


# --------------------------------------------------------------------------- #
# ② 级联选择器数据
# --------------------------------------------------------------------------- #
def cascade() -> dict[str, Any]:
    tx = load()
    return {
        "version": tx.version,
        "updated": tx.updated,
        "confirmed_ratio": round(
            sum(1 for s in tx.specifics.values() if s.confirmed) / len(tx.specifics), 3
        ),
        "generals": [
            {"id": g.id, "name_en": g.name_en, "name_zh": g.name_zh, "label": g.label}
            for g in sorted(tx.generals.values(), key=lambda x: x.id)
        ],
        "specifics": [
            {
                "code": s.stable_code,
                "parent_id": s.parent_id,
                "name_zh": s.name_zh,
                "name_en": s.name_en,
                "description_zh": s.description_zh,
                "key_dimensions": list(s.key_dimensions),
                "evidence_needed": list(s.evidence_needed),
                "confusable_with": list(s.confusable_with),
                "confirmed": s.confirmed,
            }
            for s in sorted(tx.specifics.values(), key=lambda x: x.stable_code)
        ],
        "confusing_pairs": [
            {"pair": [a, b], "note": tx.pair_notes.get((a, b), "")}
            for a, b in tx.confusing_pairs
        ],
    }
