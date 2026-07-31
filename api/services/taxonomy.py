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
    thresholds: dict | None = None      # Annex 4 逐字判据 + 机器可判规则

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
            thresholds=s.get("thresholds"),
        )

    # 混淆对分三档（人类 07-31 裁决①），`source` 字段必填：
    #   Tier 1 definitional               —— 从 thresholds 自动推导，共享数值切分线。见 `_derive_pairs`
    #   Tier 2 definitional_compositional —— Annex 4 定义决定但判据是组成/形态，在 json 里显式登记
    #   Tier 3 dev_error_analysis         —— 只能经 register_empirical_pair() 从 dev split 注入
    pairs, notes = _derive_pairs(specifics)
    comp_pairs, comp_notes = _load_compositional(raw, specifics)
    pairs = tuple(pairs) + comp_pairs
    notes.update(comp_notes)

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


PAIR_SOURCE: dict[tuple[int, int], str] = {}
# 推导副产物：每对的判定营养维度（已映射成 Evidence 的 Nutrient 名）
PAIR_DIMS: dict[tuple[int, int], tuple[str, ...]] = {}

# thresholds 里的字段名 → Evidence 的 Nutrient 名。
# 值为 None 表示"不是营养表里的读数"（如果汁百分比要读配料表），
# 不能用于 Day5 的跨源冲突判定。
_TH_TO_NUTRIENT: dict[str, str | None] = {
    "sugar": "sugar",
    "fiber": "fiber",
    "fat": "fat",
    "saturated_fat": "saturated_fat",
    "sodium_mg": "sodium",
    "energy_kj": "energy_kj",
    "protein": "protein",
    "fruit_pct": None,
}

_OPPOSITE = {"<": {">", ">="}, "<=": {">", ">="}, ">": {"<", "<="}, ">=": {"<", "<="}}

# 混淆对的判定字段中文名 —— 只用于给人看的 note，模型侧 prompt 走 thresholds 的英文原文
_TH_LABEL_ZH = {
    "sugar": "糖", "fiber": "膳食纤维", "fat": "脂肪", "saturated_fat": "饱和脂肪",
    "sodium_mg": "钠", "energy_kj": "能量", "protein": "蛋白质", "fruit_pct": "果汁含量",
}

# 判定字段不在营养表里的混淆对：它们是真对，但 Day5 的跨源冲突判定天然对它们失效
# （果汁百分比要读配料表）。登记在这里，`_validate` 就不必每次启动都报一遍。
PAIRS_WITHOUT_NUTRIENT_DIM: frozenset[tuple[int, int]] = frozenset({(3, 18)})


def _rules_of(th: dict | None) -> list[tuple[str, str, float]]:
    """把 thresholds 块里的 all_of / any_of 摊平成 (nutrient, op, value) 列表。

    只取这两个键 —— `cheese_rule` / `soup_rule` 之类是**同一类内部的形态分支**，
    不是两类之间的切分点，拿它们配对会推出假对。
    """
    out: list[tuple[str, str, float]] = []
    for key in ("all_of", "any_of"):
        for item in (th or {}).get(key, []) or []:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                out.append((str(item[0]), str(item[1]), float(item[2])))
    return out


def _derive_pairs(specifics: dict[int, SpecificCategory]):
    """从 taxonomy 结构推导 definitional 混淆对 —— 人工零介入，来源可审计。

    **判据：两类在同一营养素、同一 basis 上给出方向相反、切分点相同的阈值。**
    例：2 是 `sugar < 20`、12 是 `sugar > 20` —— 它们共用一条线，
    线两侧长得一样、只有营养表能分开，这就是"视觉不可区分"的定义级证据。

    这样推出来的对，其混淆性是**Annex 4 定义的推论**，
    不是从标注数据的 confusion matrix 里挖出来的经验，进 prompt 不构成信息泄漏
    （Day6 A3 决议）。经验对只能来自 dev split，走 `register_empirical_pair()`。

    ## 为什么换掉了上一版规则

    上一版判据是「同父类 + `key_dimensions` 相同 + 双方都有 thresholds」。它有两个毛病：

    - **推出假对**：8/23/24 同属父类 6、`key_dimensions` 都是 ['脂肪','盐']，于是
      (8,24) 和 (23,24) 被当成混淆对。但 8/23 按 **per serve** 的饱和脂肪+钠切分，
      24 按 **per 100g** 的总脂肪切分 —— 两者没有共享的判定线，24 与 8/23 之分
      是产品形态（餐食 vs 酱料/汤）而非阈值，模型拿营养表分不开也不该去分。
    - **漏掉真对**：7（低脂咸味酱 <10g fat/100g）与 24（高脂咸味酱 >10g fat/100g）
      共用同一条 10g 线，是最干净的定义级混淆对，却因为分属父类 8 与 6 被挡在门外。
      Annex 4 的父类分组是按"健康/不健康"编排的，不是按视觉相似度，拿它当判据是错的。

    新规则同时修好这两处，且推导结果与 `nutrient_rules._PAIR_RULES` 天然对齐。
    """
    pairs, notes = [], {}
    codes = sorted(specifics)
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            sa, sb = specifics[a], specifics[b]
            if not (sa.thresholds and sb.thresholds):
                continue
            if sa.thresholds.get("basis") != sb.thresholds.get("basis"):
                continue

            shared: list[str] = []
            for na, opa, va in _rules_of(sa.thresholds):
                for nb, opb, vb in _rules_of(sb.thresholds):
                    if na == nb and va == vb and opb in _OPPOSITE.get(opa, set()):
                        if na not in shared:
                            shared.append(na)
            if not shared:
                continue

            pairs.append((a, b))
            # note 用**共享的那条切分线**，不用 key_dimensions —— 后者是人工写的
            # 品类描述（如 7 写的是"脂肪酸类型"），和实际把两类分开的那个字段不是一回事
            notes[(a, b)] = "｜".join(_TH_LABEL_ZH.get(s, s) for s in shared)
            PAIR_SOURCE[(a, b)] = "definitional"
            PAIR_DIMS[(a, b)] = tuple(
                n for n in (_TH_TO_NUTRIENT.get(s) for s in shared) if n
            )
    return tuple(pairs), notes


def _load_compositional(raw: dict, specifics: dict[int, SpecificCategory]):
    """读 Tier 2（definitional_compositional）—— 显式登记，不推导。

    为什么不推导：这些对的判据是**组成/形态**（1/13 面条炸不炸、31/32 快餐是否只推健康款），
    不是数值切分线。`_derive_pairs` 只认共享切分线，硬要把它们也推出来就得放宽判据，
    而 A3 的整个论证依赖"混淆性是定义的推论"—— 判据放宽一分，那句话就弱一分。
    所以分档，不混谈。

    指向不存在编号的对**跳过并告警**，不静默丢：数据侧原清单里的 (35,36)
    就属于这种（本 taxonomy 只到 34），见 OPEN-QUESTIONS 的 A4。
    """
    block = raw.get("compositional_pairs") or {}
    pairs: list[tuple[int, int]] = []
    notes: dict[tuple[int, int], str] = {}
    for item in block.get("pairs", []):
        a, b = sorted(item["pair"])
        missing = [c for c in (a, b) if c not in specifics]
        if missing:
            logger.warning(
                "compositional_pairs 里的 (%s,%s) 指向不存在的编号 %s，已跳过", a, b, missing
            )
            continue
        source = item.get("source")
        if not source:
            raise ValueError(f"compositional_pairs 的 ({a},{b}) 缺 source 字段（裁决①要求必填）")
        pairs.append((a, b))
        notes[(a, b)] = item.get("criterion") or item.get("note", "")
        PAIR_SOURCE[(a, b)] = source
    return tuple(pairs), notes


def register_empirical_pair(a: int, b: int, note: str = "") -> None:
    """注入经验混淆对。**只准来自 dev split**，报指标时必须声明（A3 决议）。"""
    tx = load()
    key = tuple(sorted((a, b)))
    if key in tx.confusing_pairs:
        return
    object.__setattr__(tx, "confusing_pairs", tx.confusing_pairs + (key,))
    tx.pair_notes[key] = note or "dev 误差分析"
    PAIR_SOURCE[key] = "dev_error_analysis"


def pair_source(a: int, b: int) -> str:
    return PAIR_SOURCE.get(tuple(sorted((a, b))), "unknown")


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
    # Tier 2（definitional_compositional）天然没有营养维度 —— 它们的判据就是组成/形态，
    # 不是数字。对它们告警等于每次启动都刷 8 行噪音，且会淹没真正该看的那一条。
    uncovered = [
        p
        for p in tx.confusing_pairs
        if PAIR_SOURCE.get(p) != "definitional_compositional"
        and not (PAIR_DIMS.get(p) or PAIR_NUTRIENTS.get(p))
        and p not in PAIRS_WITHOUT_NUTRIENT_DIM
    ]
    if uncovered:
        logger.warning(
            "这些混淆对没有可用的判定维度（冲突判定对它们失效）: %s —— "
            "definitional 对应由 Annex 4 阈值自动推出，经验对需在 PAIR_NUTRIENTS 里补",
            uncovered,
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
# definitional 对的维度**自动来自 Annex 4 阈值**（`PAIR_DIMS`，由 `_derive_pairs` 填），
# 不再手抄 —— 手抄过一次就错过一次：旧表里 (8,23) 写的是 ("fat","sodium")，
# 而 Annex 4 用的是 **saturated fat**，写错的那一维在冲突判定里等于静默失效。
#
# 下表只保留**经验对**（`register_empirical_pair` 注入的，来自 dev split）的维度，
# 因为它们没有 Annex 4 阈值可推。这里仍然是**显式表**而不是从 key_dimensions 的
# 中文串猜（教训见 HFSS_VERDICTS 上面那段）。
#
# 三对当前不在推导结果里、留在这儿备查：Annex 4 对它们**没有给数值切分点**，
#   (16,17) 甜零食 vs 咸零食 —— 靠品类形态，不靠营养表
#   (18,25) 果汁饮料 vs 含糖饮料 —— 靠果汁百分比（配料表，非营养表）
#   (25,29) 含糖饮料 vs 茶咖 —— 靠"是否加糖"，同上
# 它们要进 prompt 必须走 `register_empirical_pair()` 并标 source=dev_error_analysis。
PAIR_NUTRIENTS: dict[tuple[int, int], tuple[str, ...]] = {
    (16, 17): ("sugar", "sodium"),
    (18, 25): ("sugar",),
    (25, 29): ("sugar",),
}


def pair_dimensions(a: int, b: int) -> tuple[str, ...]:
    """单对的判定维度：先查 Annex 4 推导结果，再退回经验对的显式表。"""
    key = (a, b) if a < b else (b, a)
    load()          # 确保 PAIR_DIMS 已填充
    return PAIR_DIMS.get(key) or PAIR_NUTRIENTS.get(key, ())


def pair_nutrients(codes: Any = None) -> tuple[str, ...]:
    """给定一组候选细类编号，返回它们所处混淆对的判定维度（去重）。

    codes 为 None 或没有任何一对完整落在其中时返回空元组 —— 冲突判定随之关闭，
    这是有意的：条件 3 要求"恰好落在目标混淆对的判定维度上"。
    """
    if not codes:
        return ()
    have = {c for c in codes if isinstance(c, int)}
    out: list[str] = []
    for a, b in load().confusing_pairs:
        if a in have and b in have:
            out.extend(d for d in pair_dimensions(a, b) if d not in out)
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


# --------------------------------------------------------------------------- #
# A3 消融：prompt 里放哪些混淆对
# --------------------------------------------------------------------------- #
# 四臂（人类裁决①后从三臂拆开）。**Tier 2 必须单独占一臂**，
# 否则 B→C 的差值把"组成级先验"和"经验先验"搅在一起，谁也说不清是哪个在起作用。
#
#   A  —— 一对都不放。回答"置信度信号是模型内生的，还是我们喂出来的"
#   B  —— 仅 Tier 1（definitional，共享数值切分线）。阈值级先验值多少
#   B2 —— Tier 1 + Tier 2（+ definitional_compositional）。**线上默认**
#   C  —— 全部三档。经验先验再加多少；**只准在 held-out 上报**
#
# 可读的对比：B−A = 阈值先验；B2−B = 组成先验；C−B2 = 经验先验。
ARM_TIERS: dict[str, tuple[str, ...]] = {
    "A": (),
    "B": ("definitional",),
    "B2": ("definitional", "definitional_compositional"),
    "C": ("definitional", "definitional_compositional", "dev_error_analysis"),
}
PairsArm = str


def pairs_for_arm(arm: str = "B2") -> tuple[tuple[int, int], ...]:
    """按消融臂过滤混淆对。未知 arm 一律按 B2 处理（保守：不泄漏经验对）。"""
    tiers = ARM_TIERS.get((arm or "").upper() if arm else "", None)
    if tiers is None:
        tiers = ARM_TIERS["B2"]
    return tuple(p for p in load().confusing_pairs if PAIR_SOURCE.get(p) in tiers)


def pairs_by_tier() -> dict[str, list[tuple[int, int]]]:
    """按 source 分组 —— 报指标时 Tier 1 与 Tier 2 必须分开声明（裁决①）。"""
    out: dict[str, list[tuple[int, int]]] = {}
    for p in load().confusing_pairs:
        out.setdefault(PAIR_SOURCE.get(p, "unknown"), []).append(p)
    return out


def confusing_pairs_block(arm: str = "B") -> str:
    tx = load()
    parts = []
    for a, b in pairs_for_arm(arm):
        dim = tx.pair_notes.get((a, b), "").split("｜")[0]
        parts.append(f"[{a}]vs[{b}]({dim})")
    return "; ".join(parts) or "(none supplied — judge confusability yourself)"


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


def thresholds_block() -> str:
    """Annex 4 的数值判据，英文原样进 prompt（Day6 B1 决议）。"""
    tx = load()
    return "\n".join(
        f"[{c}] {tx.specifics[c].thresholds['verbatim']}"
        for c in sorted(tx.specifics)
        if tx.specifics[c].thresholds and tx.specifics[c].thresholds.get("verbatim")
    )


def build_classify_prompt(
    few_shots: list[str] | None = None, pairs_arm: str | None = None
) -> str:
    from config import settings

    arm = pairs_arm or settings.pairs_arm
    prompt = CLASSIFY_SYSTEM_PROMPT.format(
        taxonomy=taxonomy_block(), pairs=confusing_pairs_block(arm)
    )
    prompt += ("\n\nOFFICIAL NUMERIC CUT-OFFS (protocol Annex 4 — these decide the "
               "confusable pairs; you usually CANNOT read them off a photo, so when a "
               "category below applies, report low specific_confidence and let the "
               "system retrieve the nutrition panel):\n" + thresholds_block())
    if few_shots:
        prompt += (
            "\n\nCORRECTED EXAMPLES (human corrections on similar ads — follow the correction):\n"
            + "\n".join(few_shots)
        )
    return prompt


def build_adjudicate_prompt(pairs_arm: str | None = None) -> str:
    from config import settings

    arm = pairs_arm or settings.pairs_arm
    return ADJUDICATE_SYSTEM_PROMPT.format(
        taxonomy=taxonomy_block(), pairs=confusing_pairs_block(arm)
    ) + ("\n\nOFFICIAL NUMERIC CUT-OFFS (protocol Annex 4 — apply these literally; "
         "cereals/dairy/sauces/soups are judged per 100g, meals and snacks PER SERVE; "
         "if serving size is unknown, say so and keep confidence low):\n"
         + thresholds_block())


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
        # A3 决议：每对必须自带 `source`。definitional = 从 Annex 4 阈值推出、零标注介入；
        # dev_error_analysis = 从 dev split 的误差分析来，报指标时必须声明。
        "confusing_pairs": [
            {
                "pair": [a, b],
                "note": tx.pair_notes.get((a, b), ""),
                "source": PAIR_SOURCE.get((a, b), "unknown"),
                "dimensions": list(pair_dimensions(a, b)),
            }
            for a, b in tx.confusing_pairs
        ],
    }
