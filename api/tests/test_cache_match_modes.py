"""Day7 · 缓存匹配 legacy / strict 双态锁死 + 命中观测台。

## 为什么两态都要测

`CACHE_MATCH_MODE` 默认 `legacy` —— **手术预备，今日不启用**。
只测 strict 的话，legacy 的行为会在某次重构里悄悄漂移，而线上跑的正是 legacy。
所以每个关键 case 都写成"legacy 下 X、strict 下 Y"的成对断言，把两态一起钉住。

## 核心回归：Amul Toned / Double Toned

Toned（≤3g 脂肪 → **5**）与 Double Toned 跨 5/19 分界，是这条风险最贵的形态。
数据侧核对指出非对称覆盖**只修了一半**，这里两个方向都立了用例。
"""

from __future__ import annotations

import pytest

import db
from config import settings
from services import cache_store

# (查询名, 档案名, strict 是否应否决, 说明)
CASES = [
    (
        "Amul Double Toned Milk", "Amul Toned Milk", True,
        "档案 token 是查询的子集 —— 非对称覆盖放行，靠维度词 double 才挡得住。"
        "这正是数据侧指出的『只修了一半』的那一半",
    ),
    (
        "Amul Toned Milk", "Amul Double Toned Milk", True,
        "反方向：档案多出 double，非对称覆盖直接否决",
    ),
    ("Amul Toned Milk 1L", "Amul Toned Milk", False, "只多了规格，同一产品"),
    ("Amul Toned Milk", "Amul Toned Milk", False, "完全相同"),
    (
        "Nestle Full Cream Milk", "Nestle Milk", True,
        "full cream 是脂肪维度词（>3g/100g → 19）",
    ),
    (
        "Maggi Instant Noodles 70g", "Maggi Noodles 70g", True,
        "instant 决定 1 vs 13（Annex 4 的 1 明写 exclude fried/flavoured）",
    ),
    (
        "Coke Zero Sugar 330ml", "Coke 330ml", True,
        "zero sugar 决定 25 vs 其他",
    ),
]


@pytest.mark.parametrize("query,archive,should_reject,why", CASES)
def test_strict_reject_matrix(query, archive, should_reject, why):
    reason = cache_store.strict_reject(query, archive)
    assert bool(reason) is should_reject, f"{why}｜实际={reason!r}"


def test_dimension_terms_come_from_json_not_code():
    """裁决明令：词表数据驱动，禁止硬编码。

    断言方式是"改 JSON 就该改行为"——直接查源码里有没有字面量太脆，
    真正要保证的是这条链路读的是文件。
    """
    import json
    from pathlib import Path

    raw = json.loads(Path(settings.category_terms_path).read_text(encoding="utf-8"))
    assert "dimension_terms" in raw, "维度词表必须在 category_terms.json 里"
    in_json = {
        t.lower()
        for k, v in raw["dimension_terms"].items()
        if not k.startswith("_")
        for t in v
    }
    assert set(cache_store.dimension_terms()) == in_json
    for must in ("double toned", "toned", "full cream", "skimmed"):
        assert must in in_json, f"决议点名的 {must!r} 不在词表里"


def test_longer_phrases_are_matched_before_their_prefixes():
    """`double toned` 必须先于 `toned` 被看到，否则差集判定会认错维度。"""
    terms = cache_store.dimension_terms()
    assert terms.index("double toned") < terms.index("toned")


# --------------------------------------------------------------------------- #
# 两态行为对照 —— 走完整 lookup，不只是 strict_reject
# --------------------------------------------------------------------------- #
@pytest.fixture
def amul_archive():
    """插一条 `Amul Toned Milk` 档案（人类要求的"手动插一条档案"那一步）。"""
    db.init_db()
    from graph.state import Classification, Evidence, NutrientValue

    ev = [Evidence(id="ev_1", source_url="https://amul.com/toned",
                   nutrients=[NutrientValue(nutrient="fat", value=3.0,
                                            unit="g/100ml", normalized=3.0)])]
    verdict = Classification(general_id=3, specific_code=5, brand="Amul",
                             product_name="Amul Toned Milk",
                             specific_confidence=0.9, general_confidence=0.95)
    res = cache_store.upsert("Amul", "Amul Toned Milk", ev, verdict)
    yield res["id"]
    with db.cursor() as cur:
        cur.execute("DELETE FROM product_cache WHERE id=?", (res["id"],))


def _lookup(name, mode):
    return cache_store.lookup("Amul", name, mode=mode)


def test_same_product_hits_in_both_modes(amul_archive):
    for mode in ("legacy", "strict"):
        rec, score = _lookup("Amul Toned Milk", mode)
        assert score >= settings.cache_hit_threshold, mode
        assert rec and not rec.get("strict_reject_reason"), mode


def test_double_toned_hits_in_legacy_but_is_rejected_in_strict(amul_archive):
    """**这条就是 OPEN-RISK-01 的锁**：同一次查询，两态结论必须相反。

    legacy 命中 → 拿 Toned（5）的营养去裁决 Double Toned，跨 5/19 判错且无痕迹。
    strict 否决 → 转联网搜索，慢但对。
    """
    rec_legacy, score_legacy = _lookup("Amul Double Toned Milk", "legacy")
    assert score_legacy >= settings.cache_hit_threshold
    assert rec_legacy and not rec_legacy.get("strict_reject_reason"), "legacy 下应命中"

    rec_strict, score_strict = _lookup("Amul Double Toned Milk", "strict")
    assert score_strict == pytest.approx(score_legacy), "得分算法不变，只是否决"
    assert rec_strict and rec_strict.get("strict_reject_reason"), "strict 下应被否决"
    assert "double toned" in rec_strict["strict_reject_reason"]


def test_rejected_hit_still_reports_its_score(amul_archive):
    """被否决时仍返回得分 —— trace 里要看得出"差点命中了什么"。

    返回 0.0 会让"库里根本没有"和"库里有但我们没用"长得一模一样，
    而这两件事对下一步该做什么的指向完全不同。
    """
    rec, score = _lookup("Amul Double Toned Milk", "strict")
    assert score > 0.8 and rec is not None


def test_default_mode_is_legacy():
    """今日决议：手术预备，**不启用**。默认值改了要有人知道。"""
    assert settings.cache_match_mode == "legacy"


# --------------------------------------------------------------------------- #
# 命中观测台
# --------------------------------------------------------------------------- #
def test_cache_hit_log_is_idempotent():
    """resume 会让整条链路重跑，一次命中不能被记成两次。"""
    db.init_db()
    aid = "t-idem-" + db.new_id()[:8]
    for _ in range(3):
        db.log_cache_hit(aid, "c1", 0.91, "auto", "legacy")
    assert len(db.cache_hit_rows([aid])) == 1


@pytest.mark.parametrize(
    "human_choice,cached,final,expect",
    [
        ("original", 5, 19, 1),      # 人工推翻缓存给出的叶子
        ("prediction", 5, 5, 0),     # 人工确认
        ("manual", 5, 12, 1),        # 人工手动改到第三个码
        (None, 5, 5, None),          # 还没走到人工 → NULL，**不是 0**
    ],
)
def test_overturn_is_three_valued(human_choice, cached, final, expect):
    """把"没人看过"算成"人工确认了"会让改判率虚低 —— 而这个指标存在的意义
    恰恰是发现缓存在悄悄喂错答案。所以 NULL 必须与 0 分开。
    """
    db.init_db()
    aid = "t-ovt-" + db.new_id()[:8]
    db.log_cache_hit(aid, "c1", 0.9, "auto", "legacy")
    db.finalize_cache_hit(aid, route_1="search", route_2="human",
                          human_choice=human_choice, cached_code=cached, final_code=final)
    assert db.cache_hit_rows([aid])[0]["overturned"] == expect


def test_finalize_is_a_noop_for_audits_that_never_hit():
    """未命中的审计不该出现在观测台里 —— 否则命中率分母就错了。"""
    db.init_db()
    aid = "t-miss-" + db.new_id()[:8]
    db.finalize_cache_hit(aid, route_1="search", route_2="direct_verified",
                          human_choice=None, cached_code=None, final_code=2)
    assert db.cache_hit_rows([aid]) == []


# --------------------------------------------------------------------------- #
# fallback 向量库跨进程可见性（Day7 实测踩到的缺陷）
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_vectorstore(tmp_path, monkeypatch):
    """把向量库指到本用例专属目录，**用完自动还原**。

    Day8 评审指出的移植性缺陷就在这：原来是
        settings.chroma_path = str(path)      # 裸赋值，永不还原
    于是这条用例跑完之后，**全会话**的 `chroma_path` 都指着一个即将被删的 tmp 目录。
    Linux 上它恰好排在靠后位置没炸；macOS 的 tmp 路径与用例顺序不同，整轮就会红一条。

    这正是 conftest 修过的那类"测试污染"在单测内部的复发 ——
    上次是库文件，这次是向量库路径。`monkeypatch` 会在用例结束时自动还原属性，
    `vectorstore.reset()` 前后各调一次则保证单例不把旧路径带进来 / 带出去。
    """
    from services import vectorstore

    monkeypatch.setattr(settings, "chroma_path", str(tmp_path / "chroma"))
    vectorstore.reset()
    yield tmp_path / "chroma"
    vectorstore.reset()


def test_fallback_vectorstore_picks_up_writes_from_another_process(isolated_vectorstore):
    """别的进程写进 fallback.json 的档案，本进程必须能看见。

    ## 这个缺陷长什么样

    原实现在 `_FallbackCollection.__init__` 里把 `store.data[name]` 存成实例属性，
    于是 collection 永远看的是**构造那一刻**的快照。API 进程启动后，
    脚本 / eval runner 写进去的档案对它完全不可见。

    表现极具迷惑性：命中得分停在 **0.75**（0.55 品牌 + 0.20 名称重叠，语义分为 0），
    刚好卡在 0.82 阈值下面 —— 看起来是"缓存没命中"，实际是"索引没刷新"。
    今日验收项"手动插一条档案验证二次免搜索"正是跨进程的，不修就永远过不了。

    顺带暴露的结构性事实：`W_EXACT_BRAND + W_NAME_OVERLAP = 0.75 < 0.82`，
    也就是**任何一次缓存命中都依赖语义分**。今日不动权重（决议：保持原始行为观察），
    但这条已登记进 OPEN-QUESTIONS。
    """
    import json

    from services import vectorstore

    path = isolated_vectorstore
    col = vectorstore.collection("products")
    # metadata 不能为空：chroma 拒绝空 dict，fallback 接受 —— 我们在
    # `_validate_upsert` 里统一拦掉，免得同一段代码只在装了 chroma 的机器上炸
    col.upsert(ids=["a"], documents=["MockBrand | A"], metadatas=[{"brand": "MockBrand"}])
    assert col.count() == 1

    # 模拟另一个进程：直接改盘上的 JSON，本进程不重启
    raw = json.loads((path / "fallback.json").read_text())
    raw["products"]["b"] = {"document": "MockBrand | B", "metadata": {"brand": "MockBrand"}}
    (path / "fallback.json").write_text(json.dumps(raw, ensure_ascii=False))

    assert col.count() == 2, "外部写入不可见 —— fallback 客户端没有按 mtime 重载"
    ids = vectorstore.collection("products").query(query_texts=["MockBrand | B"])["ids"][0]
    assert "b" in ids


def test_score_ceiling_without_semantic_is_below_the_hit_threshold():
    """把"命中依赖语义分"这条**结构性事实**钉在测试里，而不是留在某人的记忆里。

    完全同名（品牌精确 + 名称 100% 重叠）也只有 0.75，低于 0.82 阈值。
    后果：向量库一挂，缓存命中率直接归零，而 SQLite 档案还好端端躺着 ——
    看板上会表现为"记忆机制失效"，排查方向却会指向缓存写入。

    今日不改权重（决议：保持原始行为继续观察）。这条用例的作用是：
    哪天有人调了权重或阈值，这里会红，逼他看到这个耦合。
    """
    assert cache_store.W_EXACT_BRAND + cache_store.W_NAME_OVERLAP == pytest.approx(0.75)
    assert settings.cache_hit_threshold == 0.82
    assert cache_store.W_EXACT_BRAND + cache_store.W_NAME_OVERLAP < settings.cache_hit_threshold
