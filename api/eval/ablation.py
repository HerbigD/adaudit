"""A3 · 四臂消融：prompt 里的混淆对，到底给模型带来了多少东西。

| arm | prompt 里的混淆对 | 回答什么 |
|---|---|---|
| A  | 无 | 置信度信号是不是**内生**的 |
| B  | Tier 1 definitional（共享数值切分线） | 阈值级先验值多少 |
| B2 | + Tier 2 definitional_compositional | 组成级先验再加多少（**线上默认**） |
| C  | + Tier 3 dev 经验对 | 经验先验再加多少（**只准在 held-out 报**） |

**A 与 B 的差值是这段实验里最值钱的数**：它正面回答"你说的置信度感知，
是模型自己的，还是你在 prompt 里喂出来的"。审稿人一定会问这一句。

Tier 2 单独占一臂而不是并进 B（人类裁决①）：并进去的话 B→C 的差值会把
"组成级先验"和"经验先验"搅在一起，谁也说不清是哪个在起作用。

## 为什么要配对照组

只在混淆样本上比三臂会得出一个假结论：加混淆对提示 → 混淆样本准确率上升 → 有效。
但提示也可能只是把模型整体推向"多报低置信"，于是它在**非混淆**样本上
也开始犹豫、也开始转人工 —— 那不是判别力变强，是阈值整体漂移。

所以对照组按同样的国家×语言分布抽等量的**非混淆**样本。
真正该看的是差分：`Δ混淆 − Δ对照`。只有它显著为正，才说明提示带来的是判别力。

## 样本量

混淆样本 90 条（人类决议）+ 等量对照 = 180 条 × 4 臂 = 720 次跑批。
90 条上 5 个百分点的差异对应 ±10pp 左右的置信区间 —— 这个规模能看出大效应，
看不出小效应。报结论时必须带这句，不许拿 90 条上的 3pp 说事。

## 消融集从哪来（A9 · 人类 07-31 批准）

**来源：`池 − eval − smoke`（4,615 条），不是 dev 200。**

原因是 dev 里凑不出有用的样本：Tier 1 只有 22 条、非混淆样本只剩 59 条
（配对后对照组只能凑 48）。而 B−A 这个对比**只由 Tier 1 驱动**
（B 臂只放 Tier 1 的 5 对，Tier 2 样本在 A 与 B 之间 prompt 完全相同，纯噪声），
22 条的支撑等于这一问答不上。全池有 523 条落在 Tier 1 对上。

批准附带三个条件，都落在代码里：

1. **与 eval / smoke 严格互斥** —— `_assert_disjoint()` 在返回前断言，
   不靠自觉。违反直接抛异常，不是打日志
2. **Methods 单列 "ablation set"** —— `manifest()` 产出与 dev/eval 并列的口径说明
3. **Tier 3 经验对仍只准来自 dev** —— 消融集扩大**不**放宽这条。
   `register_empirical_pair()` 的来源约束与消融集来源是两件事，
   混起来会让 arm C 的先验悄悄扩容到 held-out 之外

按档配额抽取：先取满 Tier 1，再用 Tier 2 补足。
`subset_composition` 随结果报，每个对比只在**它的自变量真正变化的那一档**上解读，
n 按那一档算 —— 不然会拿 90 的分母去解释一个几十条支撑的差值。

**跑批前先出抽样清单给人看**（`--dry-run` / `manifest()`）。
真实调用要花钱，样本抽错了跑完只能重跑。
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from config import settings
from eval import dataset, metrics
from eval import split as split_mod

ARMS = ("A", "B", "B2", "C")
CONFUSION_N = 90            # 人类决议：混淆样本 90 条
MIN_DETECTABLE_PP = 10      # 这个样本量下大致能看出的最小效应（百分点）
TIER_ORDER = ("definitional", "definitional_compositional", "dev_error_analysis", "unknown")

# A10 追加约束：单一混淆对在本档配额里的占比上限。
#
# 不设帽会怎样：Tier 1 候选池 489 条里 5/19（奶与酸奶）占 **72%**（350 条），
# 抽 60 条会得到 ~43 条奶 —— B−A 就退化成"模型在奶类上的表现"，
# 而结论会被写成"阈值型混淆对上置信度先验有效"。那是两回事。
#
# 池子的形状不是抽样能修的（真实广告里奶就是多），但**结论的覆盖面**是抽样决定的。
PAIR_CAP_RATIO = 0.5


def _key(s: dataset.Sample) -> str:
    return f"{s.country or 'UNK'}|{s.language or 'unk'}"


def _stable_order(samples: list[dataset.Sample]) -> list[dataset.Sample]:
    seed = settings.split_seed
    return sorted(
        samples,
        key=lambda s: hashlib.sha256(f"{seed}:abl:{s.id}".encode()).hexdigest(),
    )


def pair_of(sample: dataset.Sample, tier: str) -> str | None:
    """样本在指定档里落在哪一对上。

    同一档内每个 stable_code 只属于一对（Tier 1 的 5 对与 Tier 2 的 8 对都互不重码），
    所以这里返回单值而不是列表。跨档重码（如 7 同属 Tier1 的 (7,24) 与 Tier2 的 (7,27)）
    由 `tier_of` 先定档来消歧。
    """
    from services import taxonomy

    for a, b in taxonomy.confusing_pairs():
        if taxonomy.pair_source(a, b) == tier and sample.gold_specific in (a, b):
            return f"{a}/{b}"
    return None


def _draw_with_pair_cap(
    rows: list[dataset.Sample], n: int, tier: str, cap: int | None = None
) -> list[dataset.Sample]:
    """在"单一对 ≤ cap"的约束下取 n 条，按对轮流取（round-robin）。

    ## 为什么是轮流取而不是"贪心取满再补"

    裁决原文是"单一对不超过 30 条，不足的从其他对补"。
    按字面贪心执行：5/19 取满 30，再从 3/18 取 30 —— 上限满足了，
    但 60 条只覆盖 **2 对**，B−A 仍然是个窄结论。

    轮流取同样满足上限（每对 12 条 < 30），却覆盖全部 5 对 ——
    它服务的正是设这条上限的**理由**："不设帽 B−A 就退化成纯奶类结论"。
    所以这里按理由执行，不按字面执行，并把 per-pair 分布如实报出来供复核。

    ## 要说清楚的一点：均衡来自轮流取，不来自上限

    当前池形状下 5 对各取 12 条，12 远低于上限 30 —— **上限一次都没触发**。
    它是兜底，只在"某些对提前取空、剩下的对被迫多担"时才起作用。
    别把它当调分布的旋钮：改 `PAIR_CAP_RATIO` 现在不会改变任何结果。

    取不满就返回实际条数，不跨约束硬凑（`build_subset` 会照实报 composition）。
    """
    cap = cap if cap is not None else max(1, round(n * PAIR_CAP_RATIO))
    by_pair: dict[str, list[dataset.Sample]] = defaultdict(list)
    for s in rows:
        by_pair[pair_of(s, tier) or "unknown"].append(s)

    # 顺序固定（按可用量降序、同量按对名），保证可复现
    order = sorted(by_pair, key=lambda k: (-len(by_pair[k]), k))
    cursor = {k: 0 for k in order}
    picked: list[dataset.Sample] = []

    while len(picked) < n:
        progressed = False
        for k in order:
            if len(picked) >= n:
                break
            if cursor[k] >= min(len(by_pair[k]), cap):
                continue
            picked.append(by_pair[k][cursor[k]])
            cursor[k] += 1
            progressed = True
        if not progressed:          # 所有对要么取空、要么触帽
            break
    return picked


def tier_of(sample: dataset.Sample) -> str | None:
    """样本落在哪一档的混淆对上。跨档时取更强的那档（definitional 优先）。"""
    from services import taxonomy

    tiers = {
        taxonomy.pair_source(a, b)
        for a, b in taxonomy.confusing_pairs()
        if sample.gold_specific in (a, b)
    }
    for t in ("definitional", "definitional_compositional", "dev_error_analysis"):
        if t in tiers:
            return t
    return None


class HeldOutContamination(RuntimeError):
    """消融集碰到了 eval 或 smoke。"""


HELD_OUT_SPLITS = ("eval", "smoke")


def _held_out_ids() -> set[str]:
    ids: set[str] = set()
    for name in HELD_OUT_SPLITS:
        ids |= {s.id for s in dataset.from_split(name)}
    return ids


def _assert_disjoint(samples: list[dataset.Sample], held_out: set[str]) -> None:
    """A9 条件 1：与 eval / smoke 严格互斥，**代码断言，不靠自觉**。

    这里抛异常而不是打日志：一条 eval 样本混进消融集，产出的数字看起来完全正常，
    没有任何迹象提示它被污染过。日志会被滚掉，异常不会。
    """
    bad = sorted({s.id for s in samples} & held_out)
    if bad:
        raise HeldOutContamination(
            f"消融集里混进了 {len(bad)} 条 eval/smoke 样本：{bad[:10]}"
            f"{' …' if len(bad) > 10 else ''}\n"
            f"这会让 held-out 失效。检查 build_subset 的候选池构造。"
        )


def _candidate_pool(source: str) -> list[dataset.Sample]:
    """消融集候选池。`pool_minus_heldout` 是 A9 批准的默认来源。"""
    if source != "pool_minus_heldout":
        return dataset.from_split(source)

    held_out = _held_out_ids()
    rows = split_mod.load_split("pool_4942")
    out = [
        dataset.Sample(
            id=r.id, image_path=r.image_path, gold_specific=r.gold_specific,
            gold_general=_general_of(r.gold_specific),
            source="ablation_pool",
            is_confusing_pair=any(r.gold_specific in p for p in _pairs()),
            country=r.country, language=r.language, split="ablation",
        )
        for r in rows
        if r.id not in held_out and _representable(r.gold_specific)
    ]
    return out


def _representable(code: int) -> bool:
    """A4 维持 33 类：gold ∈ {35,36,38} 的 27 张 parked，不进任何指标。"""
    from services import taxonomy

    return code in taxonomy.load().specifics


def _general_of(code: int) -> str:
    from services import taxonomy

    return taxonomy.general_of(code) if _representable(code) else ""


def parse_tier_quota(spec: str | None) -> dict[str, int] | None:
    """`"definitional=60,definitional_compositional=30"` → dict。空则返回 None。"""
    if not spec:
        return None
    out: dict[str, int] = {}
    for part in spec.split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        if k not in TIER_ORDER:
            raise SystemExit(f"[ablation] 未知档名 {k!r}，可选：{TIER_ORDER}")
        out[k] = int(v)
    return out


def build_subset(
    split: str = "pool_minus_heldout",
    n_confusing: int = CONFUSION_N,
    tier_quota: dict[str, int] | None = None,
    pair_cap: int | None = None,
) -> tuple[list[dataset.Sample], list[dataset.Sample]]:
    """返回 (混淆子集, 匹配对照组)。

    默认从 `池 − eval − smoke` 抽（A9 批准）。传具体 split 名则退回从那一份抽，
    用于对照实验或复现旧结果。

    混淆子集**按档配额**：`tier_quota` 不给时按裁决原文"先取满 Tier 1，再用 Tier 2 补到 n"。
    池里 Tier 1 有 523 条，所以默认下 90 条会**全部**来自 Tier 1 ——
    这让 B−A 拿到满额支撑，但 **B2−B 的支撑降为 0**（没有 Tier 2 样本，
    B 与 B2 两臂在这批样本上 prompt 唯一的差别不作用于任何一条）。

    要同时看两个对比就显式给配额，例如
    `tier_quota={"definitional": 60, "definitional_compositional": 30}`。
    权衡在此说明，代码不替你选。

    **每档内再按混淆对设上限**（A10 追加约束）：单一对不超过本档配额的
    `PAIR_CAP_RATIO`（默认 50%）。Tier 1 池里 5/19 占 72%，不设帽 B−A 会退化成
    纯奶类结论。`pair_cap` 可显式覆盖。

    对照组抽不满时**不补**其他层 —— 补了就破坏了配对，
    差分 `Δ混淆 − Δ对照` 会掺进分布差异。宁可对照组少几条，也要保持可比。
    """
    all_samples = _candidate_pool(split)
    held_out = _held_out_ids()

    by_tier: dict[str, list[dataset.Sample]] = defaultdict(list)
    for s in _stable_order([s for s in all_samples if s.is_confusing_pair]):
        by_tier[tier_of(s) or "unknown"].append(s)

    confusing: list[dataset.Sample] = []
    for tier in TIER_ORDER:
        room = n_confusing - len(confusing)
        if room <= 0:
            break
        want_n = room if tier_quota is None else min(room, tier_quota.get(tier, 0))
        if want_n <= 0:
            continue
        rows = by_tier.get(tier, [])
        # A10：`unknown` 档没有"对"的概念，不设帽
        take = (
            rows[:want_n] if tier == "unknown"
            else _draw_with_pair_cap(rows, want_n, tier, pair_cap)
        )
        if len(take) < want_n:
            print(f"[ablation] {tier} 在单对上限内只凑到 {len(take)}/{want_n} 条 —— "
                  f"如实报，不跨约束硬凑")
        confusing.extend(take)
    confusing = _stable_order(confusing)

    if not confusing:
        raise SystemExit(
            f"[ablation] {split} 里没有落在混淆对上的样本 —— "
            f"先确认切分 csv 的 gold_specific 覆盖了 {list(_pairs())} 这些编号"
        )

    comp = Counter(tier_of(s) or "unknown" for s in confusing)
    print(f"[ablation] 来源={split}｜混淆子集 {len(confusing)} 条，按档: {dict(comp)}")
    # 每个对比只由"自变量真正变化的那一档"驱动，逐条报支撑量
    for tier, contrast in (("definitional", "B−A"),
                           ("definitional_compositional", "B2−B"),
                           ("dev_error_analysis", "C−B2")):
        n = comp.get(tier, 0)
        if n == 0:
            print(f"[ablation] ⚠ {tier} 0 条 → **{contrast} 这个对比做不出来**"
                  f"（两臂在这批样本上 prompt 没有实际差别）")
        elif n < 30:
            print(f"[ablation] ⚠ {tier} 仅 {n} 条 → {contrast} 只有很大的效应才看得出来")

    want = Counter(_key(s) for s in confusing)
    pool: dict[str, list[dataset.Sample]] = defaultdict(list)
    for s in _stable_order([s for s in all_samples if not s.is_confusing_pair]):
        pool[_key(s)].append(s)

    control: list[dataset.Sample] = []
    shortfall: dict[str, int] = {}
    for k, need in sorted(want.items()):
        take = pool[k][:need]
        control.extend(take)
        if len(take) < need:
            shortfall[k] = need - len(take)
    if shortfall:
        print(f"[ablation] 对照组这些层抽不满（保持配对，不跨层补）: {shortfall}")

    # A9 条件 1：断言在返回前，不在调用方 —— 调用方可能忘了调
    _assert_disjoint(confusing, held_out)
    _assert_disjoint(control, held_out)
    return confusing, control


def contrast_support(confusing: list[dataset.Sample]) -> dict[str, Any]:
    """每个对比的实际支撑量 —— 读 DiD 时的正确分母。"""
    comp = Counter(tier_of(s) or "unknown" for s in confusing)
    return {
        contrast: {
            "driven_by_tier": tier,
            "n": comp.get(tier, 0),
            "interpretable": comp.get(tier, 0) >= 30,
        }
        for tier, contrast in (("definitional", "B−A"),
                               ("definitional_compositional", "B2−B"),
                               ("dev_error_analysis", "C−B2"))
    }


def manifest(
    confusing: list[dataset.Sample],
    control: list[dataset.Sample],
    source: str = "pool_minus_heldout",
) -> dict[str, Any]:
    """抽样清单 —— A9 条件 2、3 的书面凭据，**跑批前交人过目**。

    真实调用要花钱，样本抽错了跑完只能重跑；而且"消融集碰没碰 held-out"
    这件事跑完就查不了了，必须在跑之前落成文件。
    """
    def _by_pair(rows):
        """A10：per-pair 分布必须进 manifest 与日报 —— 它决定 B−A 的结论能覆盖多宽。"""
        out: dict[str, dict[str, int]] = {}
        for s in rows:
            tier = tier_of(s)
            if not tier:
                continue
            out.setdefault(tier, {})
            key = pair_of(s, tier) or "unknown"
            out[tier][key] = out[tier].get(key, 0) + 1
        return {t: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for t, v in out.items()}

    def _dist(rows):
        return {
            "n": len(rows),
            "by_tier": dict(Counter(tier_of(s) or "non_confusing" for s in rows)),
            "by_pair": _by_pair(rows),
            "by_country": dict(sorted(Counter(s.country or "UNK" for s in rows).items())),
            "by_language": dict(sorted(Counter(s.language or "unk" for s in rows).items())),
            # 键统一成字符串：JSON 的对象键只能是字符串，用 int 会让
            # 落盘再读回来的 manifest 与内存里的不相等 —— 一致性校验就形同虚设
            "by_gold": {str(k): v for k, v in sorted(Counter(s.gold_specific for s in rows).items())},
        }

    return {
        "name": "ablation set",
        "source": source,
        "seed": settings.split_seed,
        "provenance": (
            "从「单标签可表达池 − eval − smoke」按档配额抽取（A9 批准）。"
            "与 dev / eval 并列，是第三份独立子集，不是 dev 的子集。"
        ),
        "disjoint_from": list(HELD_OUT_SPLITS),
        "disjoint_verified": True,          # `build_subset` 返回前已断言，不通过就抛异常
        "tier3_constraint": (
            "Tier 3（dev_error_analysis）经验对仍只准来自 dev split —— "
            "消融集扩大**不**放宽这一条（A9 条件 3）。"
        ),
        "confusing": _dist(confusing),
        "control": _dist(control),
        "contrast_support": contrast_support(confusing),
        "pair_cap_ratio": PAIR_CAP_RATIO,
        "methods_wording": (
            "B−A 的结论仅覆盖本次实际抽到的混淆对组合（见 confusing.by_pair），"
            "不得表述为「在所有阈值型混淆对上成立」。"
            "Tier 3 经验对尚未积累，C−B2 该对比留待后续。"
        ),
        "overlap_with_held_out": 0,
    }


def write_manifest(path, confusing, control, source="pool_minus_heldout") -> dict[str, Any]:
    m = manifest(confusing, control, source)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    ids = Path(str(p).replace(".json", "_ids.csv"))
    with ids.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "tier", "pair", "id", "image_path",
                    "gold_specific", "country", "language"])
        for group, rows in (("confusing", confusing), ("control", control)):
            for s in rows:
                tier = tier_of(s)
                w.writerow([group, tier or "non_confusing",
                            (pair_of(s, tier) if tier else "") or "", s.id, s.image_path,
                            s.gold_specific, s.country, s.language])
    print(f"[ablation] 抽样清单已写入 {p} 与 {ids}")
    return m


def _pairs():
    from services import taxonomy

    return taxonomy.confusing_pairs()


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def _arm_stats(preds: list[metrics.Prediction]) -> dict[str, Any]:
    if not preds:
        return {"n": 0}
    low = [p for p in preds if p.initial_confidence < settings.direct_threshold]
    return {
        "n": len(preds),
        "exact_match": metrics.exact_match(preds),
        "general_match": metrics.general_match(preds),
        # 这才是"置信度感知"的直接观测量：模型主动认怂的比例，以及认怂时的平均置信
        "low_confidence_share": len(low) / len(preds),
        "mean_initial_confidence": sum(p.initial_confidence for p in preds) / len(preds),
        "parent_level_share": metrics.parent_level_share(preds),
        "human_review_rate": metrics.human_review_rate(preds),
    }


def summarize(
    results: dict[str, dict[str, list[metrics.Prediction]]],
    split: str = "dev",
    composition: dict[str, int] | None = None,
) -> dict[str, Any]:
    """results[arm]["confusing" | "control"] -> 预测列表。

    `composition` 是混淆子集的按档条数（`build_subset` 打印的那个），
    必须跟着结果一起报 —— 见 caveats 第二条。
    """
    arms = {
        arm: {group: _arm_stats(preds) for group, preds in groups.items()}
        for arm, groups in results.items()
    }

    contrasts = []
    for hi, lo, question in (
        ("B", "A", "Tier 1 阈值先验（共享数值切分线）带来多少"),
        ("B2", "B", "Tier 2 组成先验在阈值先验之上再加多少"),
        ("C", "B2", "Tier 3 dev 经验先验再加多少（只准在 held-out 上报）"),
        ("C", "A", "三档先验合计带来多少"),
    ):
        if hi not in arms or lo not in arms:
            continue
        d_conf = _delta(arms[hi], arms[lo], "confusing")
        d_ctrl = _delta(arms[hi], arms[lo], "control")
        contrasts.append(
            {
                "contrast": f"{hi} − {lo}",
                "question": question,
                "delta_confusing": d_conf,
                "delta_control": d_ctrl,
                # 差分：扣掉"整体变犹豫"这个混杂后，还剩多少判别力
                "difference_in_differences": (
                    None
                    if d_conf.get("exact_match") is None or d_ctrl.get("exact_match") is None
                    else round(d_conf["exact_match"] - d_ctrl["exact_match"], 4)
                ),
            }
        )

    n = arms.get("A", {}).get("confusing", {}).get("n", 0)
    return {
        "split": split,
        "arms": arms,
        "contrasts": contrasts,
        "subset_composition": dict(composition or {}),
        "caveats": [
            f"混淆子集 n={n}；该样本量下小于约 {MIN_DETECTABLE_PP} 个百分点的差异不可解读。",
            "每个对比只由**它的自变量真正变化的那一档**样本驱动："
            "B−A 靠 Tier 1、B2−B 靠 Tier 2、C−B2 靠 Tier 3。"
            "读 difference_in_differences 时要用 subset_composition 里对应档的 n，"
            "不是子集总数 —— 否则会拿 90 的分母去解释一个几十条支撑的差值。",
            "arm C 含 dev 经验对，其结果**只有在 held-out 上跑才可报**，"
            "且报告中必须声明该先验来自标注数据的误差分析。",
            "对照组按混淆子集的国家×语言分布配对抽取；"
            "真正的结论看 difference_in_differences，不看 delta_confusing 本身。",
        ],
    }


def _delta(hi: dict, lo: dict, group: str) -> dict[str, Any]:
    a, b = hi.get(group, {}), lo.get(group, {})
    keys = ("exact_match", "low_confidence_share", "mean_initial_confidence",
            "human_review_rate", "parent_level_share")
    return {
        k: (None if a.get(k) is None or b.get(k) is None else round(a[k] - b[k], 4))
        for k in keys
    }
