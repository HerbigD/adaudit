"""A9 · 消融集从「池 − eval − smoke」抽取 —— 三个批准条件的回归。

条件（人类 07-31）：
1. 与 eval、smoke **严格互斥（代码断言，不靠自觉）**
2. Methods 单列 "ablation set"，与 dev/eval 并列说明来源与规模
3. Tier 3 经验对仍只准来自 dev split，不随消融集扩大

另外覆盖 A4（27 张 parked 金标不进任何抽样）与"跑批前先出清单"。
"""

from __future__ import annotations

import json

import pytest

from eval import ablation, dataset, split as split_mod


@pytest.fixture(scope="module")
def subset():
    return ablation.build_subset(
        tier_quota={"definitional": 60, "definitional_compositional": 30}
    )


# --------------------------------------------------------------------------- #
# 条件 1 · 互斥
# --------------------------------------------------------------------------- #
def test_ablation_set_never_touches_eval_or_smoke(subset):
    confusing, control = subset
    ids = {s.id for s in confusing} | {s.id for s in control}
    for name in ablation.HELD_OUT_SPLITS:
        held = {s.id for s in dataset.from_split(name)}
        assert not (ids & held), f"消融集与 {name} 有 {len(ids & held)} 条重叠"


def test_contamination_raises_instead_of_logging():
    """混进一条 eval 样本必须**抛异常**。

    打日志不行：一条 eval 样本混进来，产出的数字看起来完全正常，
    没有任何迹象提示它被污染过，而日志会被滚掉。
    """
    ev = dataset.from_split("eval")[:1]
    with pytest.raises(ablation.HeldOutContamination) as exc:
        ablation._assert_disjoint(ev, {ev[0].id})
    assert ev[0].id in str(exc.value)


def test_confusing_and_control_do_not_overlap_each_other(subset):
    confusing, control = subset
    assert not ({s.id for s in confusing} & {s.id for s in control})


# --------------------------------------------------------------------------- #
# 条件 2 · Methods 口径凭据
# --------------------------------------------------------------------------- #
def test_manifest_states_provenance_and_is_writable(subset, tmp_path):
    confusing, control = subset
    m = ablation.write_manifest(tmp_path / "abl.json", confusing, control)

    assert m["name"] == "ablation set"
    assert m["source"] == "pool_minus_heldout"
    assert "与 dev / eval 并列" in m["provenance"]
    assert m["disjoint_from"] == list(ablation.HELD_OUT_SPLITS)
    assert m["overlap_with_held_out"] == 0
    assert m["seed"]                                  # 种子必须落纸，否则抽样不可复现

    on_disk = json.loads((tmp_path / "abl.json").read_text(encoding="utf-8"))
    assert on_disk == m
    # id 清单也要落盘：人过目的是具体样本，不是分布表
    ids_csv = tmp_path / "abl_ids.csv"
    assert ids_csv.exists()
    assert len(ids_csv.read_text(encoding="utf-8").splitlines()) == len(confusing) + len(control) + 1


def test_manifest_reports_per_contrast_support(subset):
    """读 DiD 要用**对应档**的 n，不是子集总数 —— 这个分母必须写在纸上。"""
    confusing, control = subset
    sup = ablation.manifest(confusing, control)["contrast_support"]
    assert sup["B−A"]["driven_by_tier"] == "definitional"
    assert sup["B−A"]["n"] == 60 and sup["B−A"]["interpretable"] is True
    assert sup["B2−B"]["n"] == 30
    # Tier 3 还没有任何经验对 → C−B2 目前做不出来，必须显式说出来而不是留空
    assert sup["C−B2"]["n"] == 0 and sup["C−B2"]["interpretable"] is False


# --------------------------------------------------------------------------- #
# 条件 3 · Tier 3 不随消融集扩大
# --------------------------------------------------------------------------- #
def test_tier3_source_constraint_is_stated_and_unchanged(subset):
    confusing, control = subset
    m = ablation.manifest(confusing, control)
    assert "只准来自 dev split" in m["tier3_constraint"]
    # 消融集扩大不产生任何 Tier 3 对 —— 它只能由 register_empirical_pair 注入
    from services import taxonomy

    assert not taxonomy.pairs_by_tier().get("dev_error_analysis")


# --------------------------------------------------------------------------- #
# 配额与支撑量
# --------------------------------------------------------------------------- #
def test_default_quota_fills_tier1_and_leaves_b2_unsupported():
    """裁决原文是"先取满 Tier 1 再用 Tier 2 补"。

    池里 Tier 1 有 500+ 条，所以默认下 90 条**全部**来自 Tier 1 ——
    B−A 拿到满额支撑，但 B2−B 的支撑降为 0。这是原文的直接后果，
    不是 bug；代码把它显式报出来，不替人做取舍。
    """
    confusing, _ = ablation.build_subset()
    comp = ablation.contrast_support(confusing)
    assert comp["B−A"]["n"] == ablation.CONFUSION_N
    assert comp["B2−B"]["n"] == 0 and comp["B2−B"]["interpretable"] is False


def test_explicit_quota_powers_both_contrasts(subset):
    confusing, _ = subset
    comp = ablation.contrast_support(confusing)
    assert comp["B−A"]["n"] == 60 and comp["B2−B"]["n"] == 30


# --------------------------------------------------------------------------- #
# A10 · 单一混淆对的占比上限
# --------------------------------------------------------------------------- #
def test_pair_cap_keeps_one_pair_from_dominating(subset):
    """Tier 1 候选池里 5/19 占 **72%**（350/489）。不设帽抽 60 条会得到 ~43 条奶，
    B−A 就退化成"模型在奶类上的表现"—— 而结论会被写成"阈值型混淆对上先验有效"。

    上限是 50%（60 条里单对 ≤30）。轮流取的实际结果比上限更均衡，覆盖全部 5 对。
    """
    confusing, _ = subset
    by_pair = ablation.manifest(confusing, []).get("confusing", {})["by_pair"]
    tier1 = by_pair["definitional"]

    assert len(tier1) == 5, f"Tier 1 应覆盖全部 5 对，实际 {sorted(tier1)}"
    n = sum(tier1.values())
    cap = round(n * ablation.PAIR_CAP_RATIO)
    for pair, count in tier1.items():
        assert count <= cap, f"{pair} 占了 {count} 条，超过上限 {cap}"
    # 5/19 在池里占 72%，抽完后不该还占大头
    assert tier1["5/19"] / n <= ablation.PAIR_CAP_RATIO


def test_pair_cap_applies_to_tier2_as_well(subset):
    confusing, _ = subset
    tier2 = ablation.manifest(confusing, [])["confusing"]["by_pair"][
        "definitional_compositional"
    ]
    n = sum(tier2.values())
    assert all(v <= round(n * ablation.PAIR_CAP_RATIO) for v in tier2.values())
    assert len(tier2) >= 6, "Tier 2 有 8 对，30 条应铺开而不是堆在两三对上"


def test_the_old_top_n_draw_would_have_been_milk_heavy():
    """反证：A10 之前的"按稳定序直接取前 N"会复现纯奶类那个失败模式。

    没有这条，上面的用例只能证明"当前结果是均衡的"，
    证明不了"是新抽法让它均衡的"。
    """
    from collections import Counter

    pool = ablation._candidate_pool("pool_minus_heldout")
    tier1 = ablation._stable_order([s for s in pool if ablation.tier_of(s) == "definitional"])
    old = tier1[:60]                       # A10 之前就是这么取的
    share = Counter(ablation.pair_of(s, "definitional") for s in old)
    assert share["5/19"] / 60 > 0.5, f"旧抽法下 5/19 应占大头，实际 {dict(share)}"


def test_round_robin_does_the_balancing_and_the_cap_is_a_backstop():
    """**均衡来自轮流取，不来自上限** —— 这一点值得钉死，免得日后误以为改帽就能调分布。

    当前池形状下，5 对轮流各取 12 条，12 远低于上限 30，所以上限**一次都没触发**。
    上限只在"某些对提前取空、剩下的对被迫多担"时才起作用。
    下面用 cap=5 造出那种局面：5 对 × 5 条 = 25 < 60，凑不齐，说明帽确实在约束。
    """
    pool = ablation._candidate_pool("pool_minus_heldout")
    tier1 = [s for s in pool if ablation.tier_of(s) == "definitional"]

    loose = ablation._draw_with_pair_cap(tier1, 60, "definitional", cap=60)
    tight = ablation._draw_with_pair_cap(tier1, 60, "definitional", cap=5)
    assert len(loose) == 60 and len(tight) == 25
    # cap 放到 60（等于不设帽）时，轮流取的结果与默认 cap=30 完全一致
    default = ablation._draw_with_pair_cap(tier1, 60, "definitional")
    assert [s.id for s in loose] == [s.id for s in default]


def test_shortfall_is_reported_not_padded(capsys):
    """凑不齐就如实报 —— 不跨约束硬凑（裁决原话）。"""
    pool = ablation._candidate_pool("pool_minus_heldout")
    tier1 = [s for s in pool if ablation.tier_of(s) == "definitional"]
    # 上限压到 2 → 5 对最多只能凑 10 条，要 60 必然凑不齐
    got = ablation._draw_with_pair_cap(tier1, 60, "definitional", cap=2)
    assert len(got) == 10, f"应只凑到 10 条，实际 {len(got)}"


def test_pair_of_is_single_valued_within_a_tier():
    """同档内每个 stable_code 只属于一对 —— per-pair 计数才不会重复计。

    跨档重码是有的（7 同属 Tier1 的 (7,24) 与 Tier2 的 (7,27)），
    由 `tier_of` 先定档来消歧。
    """
    from services import taxonomy

    for tier, pairs in taxonomy.pairs_by_tier().items():
        codes = [c for p in pairs for c in p]
        assert len(codes) == len(set(codes)), f"{tier} 档内有重码: {codes}"


def test_manifest_carries_the_methods_wording(subset):
    """裁决③：措辞约束跟着清单走，不靠人记得。"""
    confusing, control = subset
    m = ablation.manifest(confusing, control)
    assert "不得表述为" in m["methods_wording"]
    assert "所有阈值型混淆对" in m["methods_wording"]
    assert "Tier 3" in m["methods_wording"] and "留待后续" in m["methods_wording"]


def test_bad_tier_name_fails_loudly():
    with pytest.raises(SystemExit):
        ablation.parse_tier_quota("definitionall=60")
    assert ablation.parse_tier_quota(None) is None
    assert ablation.parse_tier_quota("definitional=5") == {"definitional": 5}


# --------------------------------------------------------------------------- #
# A4 · 27 张 parked 金标不进任何抽样
# --------------------------------------------------------------------------- #
def test_unrepresentable_gold_never_enters_the_ablation_set(subset):
    """A4 维持 33 类：gold ∈ {35,36,38} 的 27 张 parked，Day 12 前不进任何指标。"""
    from services import taxonomy

    confusing, control = subset
    for s in confusing + control:
        assert s.gold_specific in taxonomy.load().specifics, s.gold_specific


def test_parked_gold_is_archived_not_deleted():
    """parked ≠ 删掉。eval 之后还要决定去留，所以必须留一份可查的清单。"""
    from pathlib import Path

    # 直接数原文件行数：`read_pool` 会把 33 类之外的编号过滤掉，
    # 而这份文件里**每一行都是**那种编号 —— 用它读会永远得到空列表
    raw = Path(f"{split_mod.settings.split_dir}/unrepresentable_gold.csv").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(raw) - 1 == 27, f"parked 金标应为 27 张，实际 {len(raw) - 1}"
    assert split_mod.read_pool(
        f"{split_mod.settings.split_dir}/unrepresentable_gold.csv"
    ) == [], "这份清单里的编号本就不在 33 类内，read_pool 应全部过滤掉"


# --------------------------------------------------------------------------- #
# A7 · 22 → 32 合并
# --------------------------------------------------------------------------- #
def test_pool_uses_merged_gold_not_raw():
    """Annex 4 里 22 与 32 定义逐字相同 → 合并。指标必须用合并后的列。

    用 `gold_code_raw` 会让 80 张 gold=22 的图全判错（预测空间里根本没有 22）。
    """
    from pathlib import Path
    import csv as _csv

    path = Path(f"{split_mod.settings.split_dir}/pool_4942.csv")
    with path.open(encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    merged = [r for r in rows if r["gold_code_raw"] == "22"]
    assert len(merged) == 80, f"gold=22 应为 80 张，实际 {len(merged)}"
    assert all(r["gold_specific"] == "32" for r in merged)
    assert not any(r["gold_specific"] == "22" for r in rows)


# --------------------------------------------------------------------------- #
# 切分 manifest 的种子（小修复回归）
# --------------------------------------------------------------------------- #
def test_split_manifest_records_the_resolved_seed(tmp_path):
    """`kwargs` 里 seed=None 会遮蔽 `dict.get` 的默认值 —— 曾把 manifest 的 seed 记成 null。

    切分本身没错（内部另做了 None 解析），但 manifest 是"这份切分怎么来的"的
    唯一凭据，记 null 等于这份切分不可复现 —— 比切错还糟，因为看起来一切正常。
    """
    pool = f"{split_mod.settings.split_dir}/pool_4942.csv"
    m = split_mod.build(pool, tmp_path, seed=None, dev=None, ev=None, smoke=None)
    assert m["seed"] == split_mod.settings.split_seed
    assert m["seed"] is not None
    assert m["sizes_requested"]["dev"] == split_mod.settings.split_dev_size

    on_disk = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["seed"] == split_mod.settings.split_seed
