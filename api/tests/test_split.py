"""A2 数据切分：互斥、分层、可复现。

这三条性质是"我们没在测试集上调参"这句话的全部技术依据，
所以每一条都单独立个用例，而不是笼统跑一遍看不报错。
"""

from __future__ import annotations

import csv
import json

import pytest

from config import settings
from eval import split

# 规模照真实金标池来（单标签池 4,942）：切分行为对池子大小敏感 ——
# 用几百条的小池测，dev+eval+smoke 会把池子抽干，测出来的是"抽干"而不是"分层"。
POOL_SIZES = {"IN": 3200, "BD": 780, "PK": 640, "LK": 322}


def _write_pool(path, n_by_country=None, multi=0, bad=0):
    n_by_country = n_by_country or POOL_SIZES
    langs = {"IN": ["en", "hi", "ta"], "BD": ["bn", "en"], "PK": ["ur", "en"], "LK": ["si", "en"]}
    codes = [2, 12, 5, 19, 8, 23, 7, 24, 16, 17, 21, 25, 29, 3, 18]
    i = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "image_path", "gold_specific", "country", "ad_language"])
        # id 里带国家前缀：扩容测试要求"同一条样本在两份池子里 id 相同"，
        # 用全局递增序号会让 IN 多 400 条时把后面所有国家的 id 全部推移
        for c, n in n_by_country.items():
            for k in range(n):
                i += 1
                w.writerow([f"{c}{k:05d}", f"img/{c}/{k}.jpg",
                            codes[i % len(codes)], c, langs[c][k % len(langs[c])]])
        for k in range(multi):
            i += 1
            w.writerow([f"m{k}", f"img/m{k}.jpg", "19;21", "IN", "en"])
        for k in range(bad):
            i += 1
            w.writerow([f"b{k}", f"img/b{k}.jpg", "99", "IN", "en"])
    return path


@pytest.fixture
def pool(tmp_path):
    return _write_pool(tmp_path / "pool.csv", multi=15, bad=4)


def test_multi_label_and_invalid_rows_never_enter_the_split(pool):
    """A1 封存的多标签样本必须被挡在切分之外，且是**显式跳过**而不是静默丢弃。

    静默丢弃最坏：金标池 4,942 条、切分后总数对不上，没人会去查那 15 条去哪了。
    """
    rows = split.read_pool(pool)
    assert len(rows) == sum(POOL_SIZES.values())
    assert all(";" not in str(r.gold_specific) for r in rows)


def test_three_splits_are_mutually_exclusive(pool, tmp_path):
    m = split.build(pool, tmp_path / "out")
    ids = {}
    for name in split.SPLITS:
        for r in split.load_split(name, tmp_path / "out"):
            assert r.id not in ids, f"{r.id} 同时出现在 {ids.get(r.id)} 和 {name}"
            ids[r.id] = name
    assert m["sizes"]["dev"] == settings.split_dev_size
    assert m["sizes"]["eval"] == settings.split_eval_size
    assert m["sizes"]["smoke"] == settings.split_smoke_size


def test_country_proportions_are_preserved(pool, tmp_path):
    """分层的意义：四国在 dev / eval 里的占比要跟池子一致。

    不分层时 LK（占 6.7%）在 300 条 eval 里可能只剩个位数，
    D3 的按国家切片指标当场失去意义。
    """
    m = split.build(pool, tmp_path / "out")
    pool_dist = m["pool_distribution"]["country"]
    total = sum(pool_dist.values())
    for name in ("dev", "eval"):
        d = m["distribution"][name]
        for country, n in pool_dist.items():
            share_pool = n / total
            share_split = d["country"].get(country, 0) / d["n"]
            assert abs(share_pool - share_split) < 0.03, (name, country)


def test_smoke_prioritises_coverage_over_proportion(pool, tmp_path):
    """冒烟集只有十来条，按比例切会几乎全给印度。

    但它的用途恰恰是"五语种 OCR 各来一张"，所以走覆盖优先。
    """
    m = split.build(pool, tmp_path / "out")
    smoke = m["distribution"]["smoke"]
    assert len(smoke["country"]) == 4
    assert len(smoke["language"]) >= 5


def test_same_seed_same_split(pool, tmp_path):
    a = split.build(pool, tmp_path / "a")
    b = split.build(pool, tmp_path / "b")
    for name in split.SPLITS:
        ids_a = [r.id for r in split.load_split(name, tmp_path / "a")]
        ids_b = [r.id for r in split.load_split(name, tmp_path / "b")]
        assert ids_a == ids_b
    assert a["seed"] == b["seed"] == settings.split_seed


def test_different_seed_different_split(pool, tmp_path):
    a = split.build(pool, tmp_path / "a")
    b = split.build(pool, tmp_path / "b", seed=settings.split_seed + 1)
    ids_a = {r.id for r in split.load_split("eval", tmp_path / "a")}
    ids_b = {r.id for r in split.load_split("eval", tmp_path / "b")}
    assert ids_a != ids_b
    assert a["seed"] != b["seed"]


def test_growing_the_pool_keeps_existing_members_stable(tmp_path):
    """金标池扩容后，dev/eval 应大幅重叠 —— 否则每次补标注都要重跑全部指标。

    这就是排序用 `hash(seed, id)` 而不是 `random.shuffle` 的原因：
    shuffle 的输出取决于列表长度，加一条样本就会把整个序列重排。
    """
    small = _write_pool(tmp_path / "small.csv")
    big = _write_pool(tmp_path / "big.csv", {**POOL_SIZES, "IN": POOL_SIZES["IN"] + 400})

    split.build(small, tmp_path / "s")
    split.build(big, tmp_path / "b")
    a = {r.id for r in split.load_split("eval", tmp_path / "s")}
    b = {r.id for r in split.load_split("eval", tmp_path / "b")}
    assert len(a & b) / len(a) > 0.75


def test_manifest_records_the_discipline(pool, tmp_path):
    """manifest 要能独立回答"这份切分是怎么来的、该怎么用"。"""
    split.build(pool, tmp_path / "out")
    m = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert m["seed"] == settings.split_seed
    assert m["stratify_by"] == ["country", "language"]
    assert m["mutually_exclusive"] is True
    assert "只准跑一次" in m["discipline"]


def test_allocate_hits_the_target_exactly():
    """最大余数法：逐层取整会让总数飘，飘出来的差额只能从最后一层硬砍。"""
    sizes = {"a": 3200, "b": 780, "c": 640, "d": 322}
    for target in (12, 200, 300, 1):
        q = split._allocate(sizes, target)
        assert sum(q.values()) == target, (target, q)
    # 目标超过可用量时取满即止，不报错
    assert sum(split._allocate({"a": 5}, 50).values()) == 5


def test_missing_split_file_gives_an_actionable_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        split.load_split("dev", tmp_path / "nope")
    assert "eval.split --pool" in str(exc.value)
