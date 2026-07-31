"""Annex 4 判定规则引擎 —— 取代此前 `_rule_based` 里的占位阈值。

数值全部逐字来自 **Outdoor advertising protocol, Annex 4: Suggested food categorisation**，
`taxonomy.json` 的 `thresholds` 字段是唯一来源，本模块只负责执行。

## 三条执行约定（人类 Day6 决议）

1. **钠空间，不换算成盐。** Annex 4 判据全部用 sodium。营养标签只给 salt 时
   `sodium_mg = salt_g × 400` 换过去再比 —— 换算方向是"盐→钠"，不是反过来。
2. **单位分家。** cereals / dairy / sauces / soups 用 per 100g；
   meals(8/23) / snacks(9) 用 **per serve**。份量不明就把该维度标 `uncertain`
   并转人工，**不许拿 per-100g 顶替**。
3. **边界规则（本项目补充，非 Annex 4 原文）。** 2 要求 `<20 AND >5`，
   12 要求 `>20 OR <5` —— 恰好等于 20 或 5 时两边都不成立，存在定义缝隙。
   统一规则：**边界值归非健康类**（sugar ≥20 或 fibre ≤5 → 12），对 HFSS 监管口径保守。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from graph.state import Evidence, Nutrient
from services import taxonomy

SALT_G_TO_SODIUM_MG = 400.0        # 1g 盐 ≈ 400mg 钠


@dataclass
class Verdict:
    """判定结果。`uncertain=True` 时 code 必为 None —— 调用方应转人工。"""

    code: int | None = None
    uncertain: bool = False
    reason: str = ""
    used: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    rule: str = ""

    @property
    def ok(self) -> bool:
        return self.code is not None and not self.uncertain


# --------------------------------------------------------------------------- #
# 读数提取
# --------------------------------------------------------------------------- #
def _rank(evidence: Iterable[Evidence]) -> list[Evidence]:
    """按 source_type 优先级取高可信来源（official > nutrition_db > ecommerce > …）。"""
    from services.nutrition import SOURCE_RANK

    return sorted(evidence, key=lambda e: SOURCE_RANK.get(e.source_type, 9))


def per_100g(evidence: Iterable[Evidence], nutrient: Nutrient) -> float | None:
    for e in _rank(evidence):
        v = e.get(nutrient)
        if v is not None:
            return v
    return None


def per_serve(evidence: Iterable[Evidence], nutrient: Nutrient) -> float | None:
    for e in _rank(evidence):
        v = e.per_serve(nutrient)
        if v is not None:
            return v
    return None


def sodium_mg_per_100g(evidence: Iterable[Evidence]) -> float | None:
    """钠（mg/100g）。标签只给盐时按 ×400 换算过去。"""
    v = per_100g(evidence, "sodium")
    if v is not None:
        return v * 1000.0          # Evidence 的 normalized 是 g，钠判据用 mg
    for e in _rank(evidence):
        nv = e.raw("sodium")
        if nv and nv.unit and "salt" in nv.unit.lower() and nv.normalized is not None:
            return nv.normalized * SALT_G_TO_SODIUM_MG
    return None


def sodium_mg_per_serve(evidence: Iterable[Evidence]) -> float | None:
    v = per_serve(evidence, "sodium")
    return v * 1000.0 if v is not None else None


# --------------------------------------------------------------------------- #
# 各混淆对的判定
# --------------------------------------------------------------------------- #
def _cereals(evidence) -> Verdict:
    """2 vs 12 · per 100g · sugar + fibre。含边界规则。"""
    sugar, fibre = per_100g(evidence, "sugar"), per_100g(evidence, "fiber")
    missing = [n for n, v in (("sugar", sugar), ("fiber", fibre)) if v is None]
    if missing:
        return Verdict(uncertain=True, missing=missing, rule="2/12",
                       reason=f"缺 {'、'.join(missing)}（per 100g），无法按 Annex 4 判定")
    used = {"sugar_g_100g": sugar, "fibre_g_100g": fibre}
    # Annex 4: 2 = <20 AND >5；12 = >20 OR <5。边界归 12（本项目补充规则）
    if sugar < 20 and fibre > 5:
        return Verdict(code=2, used=used, rule="2/12",
                       reason=f"糖 {sugar}<20 且 纤维 {fibre}>5 → [2]")
    boundary = (sugar == 20) or (fibre == 5)
    return Verdict(
        code=12, used=used, rule="2/12",
        reason=f"糖 {sugar} / 纤维 {fibre} → [12]"
               + ("（恰在边界，按本项目补充规则归非健康类）" if boundary else ""))


def _dairy(evidence, is_cheese: bool = False) -> Verdict:
    """5 vs 19 · per 100g · fat。奶酪切分点是 15g，其余 3g。"""
    fat = per_100g(evidence, "fat")
    if fat is None:
        return Verdict(uncertain=True, missing=["fat"], rule="5/19",
                       reason="缺脂肪（per 100g），无法按 Annex 4 判定")
    cut = 15.0 if is_cheese else 3.0
    code = 19 if fat > cut else 5
    return Verdict(code=code, used={"fat_g_100g": fat}, rule="5/19",
                   reason=f"脂肪 {fat}g/100g {'>' if fat > cut else '≤'} {cut} → [{code}]")


# 7/24 的 10g 线**只管咸味酱**，不管食用油（见下）。判定前先确认品类形态。
_SAUCE_HINT = ("sauce", "ketchup", "mayonnaise", "mayo", "dressing", "chutney",
               "paste", "gravy", "dip", "酱", "沙拉酱", "番茄酱")
_OIL_HINT = ("oil", "olive", "sunflower", "canola", "ghee", "butter", "vanaspati",
             "mustard oil", "食用油", "橄榄油", "黄油")
_SOUP_HINT = ("soup", "broth", "汤")


def _sauces(evidence, *, is_sauce: bool | None = None, is_soup: bool = False) -> Verdict:
    """7 vs 24 · per 100g · fat。

    ## 阈值适用范围（Annex 4 转录核对发现的一处硬伤）

    原文是 "Oils high in mono- or polyunsaturated fats, (olive oil, ...),
    **and** low fat savoury sauces (<10g fat /100g)" ——
    `<10g/100g` **只修饰 savoury sauces 那一支**。

    橄榄油的脂肪是 ~100g/100g。把这条线通用化，等于把所有植物油判进 24
    （other high fat/salt products）—— 而 Annex 4 明确把植物油放在 7。
    油脂进 7、butter/animal fats 进 24 是**定义之分，不是阈值之分**。

    所以品类形态不明时返回 `uncertain` 转人工，**不套用阈值**。

    ## 边界

    7 是 `<10`、24 是 `>10`，恰好 10 时两边都不成立。
    按本项目统一规则归非健康类 → 24。汤的 2g 线同理（8 是 `<2`、24 是 `>2`）。
    """
    if is_soup:
        fat = per_100g(evidence, "fat")
        if fat is None:
            return Verdict(uncertain=True, missing=["fat"], rule="8/24(soup)",
                           reason="缺脂肪（per 100g），汤类无法按 Annex 4 判定")
        # Annex 4：8 含 soups (<2g fat /100g, exclude dehydrated)；24 含 soups (>2g fat /100g and all dehydrated)
        code = 24 if fat >= 2 else 8
        return Verdict(
            code=code, used={"fat_g_100g": fat}, rule="8/24(soup)",
            reason=f"汤类脂肪 {fat}g/100g {'≥' if fat >= 2 else '<'} 2 → [{code}]"
                   + ("（恰在边界，按本项目补充规则归非健康类）" if fat == 2 else "")
                   + "；脱水汤一律归 24，需由感知层判断")

    if is_sauce is False:
        return Verdict(
            uncertain=True, rule="7/24",
            reason="Annex 4 的 10g 脂肪线只适用 savoury sauces。本品不是酱类 —— "
                   "植物油归 7、黄油/动物脂肪归 24 是定义之分不是阈值之分，转人工")
    if is_sauce is None:
        return Verdict(
            uncertain=True, rule="7/24",
            reason="无法确认是否为 savoury sauce。Annex 4 的 10g 线只管酱类，"
                   "对食用油套用会把橄榄油（~100g 脂肪/100g）误判进 24，转人工")

    fat = per_100g(evidence, "fat")
    if fat is None:
        return Verdict(uncertain=True, missing=["fat"], rule="7/24",
                       reason="缺脂肪（per 100g），无法按 Annex 4 判定")
    code = 24 if fat >= 10 else 7
    return Verdict(
        code=code, used={"fat_g_100g": fat}, rule="7/24",
        reason=f"咸味酱脂肪 {fat}g/100g {'≥' if fat >= 10 else '<'} 10 → [{code}]"
               + ("（恰在边界，按本项目补充规则归非健康类）" if fat == 10 else ""))


def sauce_form(text: str) -> bool | None:
    """从品名判断是不是 savoury sauce。判不出返回 None —— **不默认是**。

    默认成"是"会让 7/24 的阈值悄悄套到食用油上，正是上面要防的那件事。
    """
    t = (text or "").lower()
    if any(k in t for k in _OIL_HINT) and not any(k in t for k in _SAUCE_HINT):
        return False
    if any(k in t for k in _SAUCE_HINT):
        return True
    return None


def soup_form(text: str) -> bool:
    return any(k in (text or "").lower() for k in _SOUP_HINT)


def _meals(evidence) -> Verdict:
    """8 vs 23 · **per serve** · saturated fat + sodium。份量不明 → uncertain。"""
    sat = per_serve(evidence, "saturated_fat")
    na = sodium_mg_per_serve(evidence)
    missing = [n for n, v in (("saturated_fat/serve", sat), ("sodium/serve", na)) if v is None]
    if missing:
        return Verdict(uncertain=True, missing=missing, rule="8/23",
                       reason=f"缺 {'、'.join(missing)} —— Annex 4 的 8/23 按每份判，"
                              f"份量不明不许拿 per-100g 顶替，转人工")
    used = {"sat_fat_g_serve": sat, "sodium_mg_serve": na}
    # Annex 4 原文用逗号并列，本项目读作 OR（见 taxonomy.json 的 project_note）
    if sat > 6 or na > 900:
        return Verdict(code=23, used=used, rule="8/23",
                       reason=f"饱和脂肪 {sat}g/份 或 钠 {na}mg/份 超标 → [23]")
    return Verdict(code=8, used=used, rule="8/23",
                   reason=f"饱和脂肪 {sat}≤6 且 钠 {na}≤900（每份）→ [8]")


def _healthy_snack(evidence) -> Verdict:
    """9 · per serve · energy + saturated fat + sodium，三条全过才算。"""
    kj = per_serve(evidence, "energy_kj")
    sat = per_serve(evidence, "saturated_fat")
    na = sodium_mg_per_serve(evidence)
    missing = [n for n, v in (("energy/serve", kj), ("saturated_fat/serve", sat),
                              ("sodium/serve", na)) if v is None]
    if missing:
        return Verdict(uncertain=True, missing=missing, rule="9",
                       reason=f"缺 {'、'.join(missing)}（每份），无法判定健康零食")
    used = {"energy_kj_serve": kj, "sat_fat_g_serve": sat, "sodium_mg_serve": na}
    if kj < 600 and sat < 3 and na < 200:
        return Verdict(code=9, used=used, rule="9",
                       reason=f"能量 {kj}<600kJ 且 饱和脂肪 {sat}<3g 且 钠 {na}<200mg（每份）→ [9]"
                              "（另需 based on core foods，由感知层判断）")
    return Verdict(uncertain=True, used=used, rule="9",
                   reason="未同时满足健康零食三条阈值，具体落 16/17/21 需看品类形态，转人工")


def _juice(evidence) -> Verdict:
    """3 vs 18 · 果汁含量 98%。果汁百分比不在营养表里，通常要读配料表。"""
    return Verdict(uncertain=True, missing=["fruit_pct"], rule="3/18",
                   reason="Annex 4 按果汁含量 ≥98% 切分 3/18，该字段需从配料表读取，"
                          "当前 Evidence 不含，转人工")


# 混淆对 → 判定函数
_PAIR_RULES = {
    (2, 12): _cereals,
    (5, 19): _dairy,
    (7, 24): _sauces,
    (8, 23): _meals,
    (3, 18): _juice,
}


def decide(
    candidates: Iterable[int],
    evidence: list[Evidence],
    *,
    is_cheese: bool = False,
    is_sauce: bool | None = None,
    is_soup: bool = False,
) -> Verdict:
    """按候选叶子挑规则。没有对应规则或证据不足 → uncertain（调用方转人工）。

    `is_cheese` / `is_sauce` / `is_soup` 是**品类形态**信号，来自品名或感知层。
    Annex 4 有三处阈值的适用范围取决于形态而不是数值：
    奶酪 15g vs 奶 3g、savoury sauce 的 10g、汤的 2g。判不出形态就不套阈值。
    """
    have = {c for c in candidates if isinstance(c, int)}
    if not have or not evidence:
        return Verdict(uncertain=True, reason="无候选或无证据")

    # 汤跨 8/24 两类，且它和 8/23 的餐食判据（per serve）用的不是同一条线，
    # 所以必须在 (8,23) 之前拦下来，否则汤会被按"冷冻餐食"判。
    if is_soup and ({8, 24} & have or {8, 23} & have):
        return _sauces(evidence, is_soup=True)

    for pair, fn in _PAIR_RULES.items():
        if set(pair) <= have:
            if fn is _dairy:
                return fn(evidence, is_cheese)
            if fn is _sauces:
                return fn(evidence, is_sauce=is_sauce)
            return fn(evidence)

    if 9 in have:
        return _healthy_snack(evidence)

    return Verdict(
        uncertain=True, rule=f"{sorted(have)}",
        reason=f"候选 {sorted(have)} 在 Annex 4 里没有数值判据（如 16/17 甜咸之分靠品类形态），转人工",
    )


def thresholds_block() -> str:
    """把 taxonomy.json 里的 Annex 4 原文拼成 prompt 片段（英文原样）。"""
    tx = taxonomy.load()
    lines = []
    for code in sorted(tx.specifics):
        th = getattr(tx.specifics[code], "thresholds", None)
        if th and th.get("verbatim"):
            lines.append(f"[{code}] {th['verbatim']}")
    return "\n".join(lines)
