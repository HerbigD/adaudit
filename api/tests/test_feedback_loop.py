"""Day8 · feedback_ingest 三处回流：幂等 + 三处可查 + MEMORY_ENABLED 开关对比。

三处 = `eval_samples`（标注扩充）/ 记忆库（few-shot 向量）/ `product_cache`（档案 supersede）。
"""

from __future__ import annotations

import pytest

import db
from config import settings
from graph.state import Classification, Evidence, NutrientValue
from services import cache_store, memory, vectorstore


def _final(code: int = 19, brand: str = "Amul", name: str = "Amul Gold Milk 1L"):
    return Classification(
        general_id=3, specific_code=code, brand=brand, product_name=name,
        specific_confidence=0.92, general_confidence=0.95,
        reasoning="人工裁定：全脂奶，脂肪 >3g/100ml",
    )


def _evidence():
    return [
        Evidence(
            id="ev_1", source_url="https://amul.com/gold",
            nutrients=[NutrientValue(nutrient="fat", value=6.0,
                                     unit="g/100ml", normalized=6.0)],
        )
    ]


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM eval_samples WHERE audit_id IS NOT NULL")
        cur.execute("DELETE FROM product_cache")
    yield


def _eval_rows(audit_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM eval_samples WHERE audit_id=?", (audit_id,))
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #
def test_remember_is_idempotent_by_audit_id():
    """同一次审计回流三遍，eval 集只应留一行。

    不幂等的后果不是"多几行"那么轻：eval 集是拿去算准确率的，
    重复样本等于**给某几张图加权** —— 而 `resume` 会重新驱动整张图，
    人工也可能改主意再裁一次，重复是常态不是意外。
    """
    aid = "t-idem-" + db.new_id()[:8]
    ids = {memory.remember("/img/a.png", _final(), audit_id=aid) for _ in range(3)}
    assert len(ids) == 1, "三次调用应返回同一个 sample_id"
    assert len(_eval_rows(aid)) == 1


def test_repeated_feedback_updates_in_place_rather_than_appending():
    """人工改主意：第二次裁定应**改写**那一行，不是再加一行。"""
    aid = "t-redecide-" + db.new_id()[:8]
    memory.remember("/img/a.png", _final(code=19), audit_id=aid)
    memory.remember("/img/a.png", _final(code=5), audit_id=aid)

    rows = _eval_rows(aid)
    assert len(rows) == 1
    assert rows[0]["gold_specific"] == "5", "应是最后一次裁定"


def test_memory_vector_id_is_the_audit_id_so_shots_do_not_duplicate():
    """向量库那一处同样要幂等 —— 否则同一条修正会被 few-shot 召回两次。"""
    aid = "t-vec-" + db.new_id()[:8]
    before = vectorstore.collection("memory").count()
    for _ in range(3):
        memory.remember("/img/a.png", _final(), audit_id=aid)
    assert vectorstore.collection("memory").count() == before + 1


def test_without_audit_id_it_falls_back_to_one_row_per_call():
    """图外调用（没有 audit_id）保持旧行为：每次一行。

    这条不是"也支持不幂等"，是**明确边界** —— 幂等键从哪来必须清楚，
    否则哪天有人忘了传 audit_id，重复写入会静默回来。
    """
    ids = {memory.remember("/img/b.png", _final()) for _ in range(2)}
    assert len(ids) == 2


# --------------------------------------------------------------------------- #
# 三处写入可查
# --------------------------------------------------------------------------- #
def test_all_three_sinks_are_written_and_queryable():
    aid = "t-three-" + db.new_id()[:8]

    sample_id = memory.remember("/img/c.png", _final(), audit_id=aid)
    res = cache_store.supersede_with_human_verdict(
        "Amul", "Amul Gold Milk 1L", _evidence(), _final(), audit_id=aid
    )

    # ① eval 集
    rows = _eval_rows(aid)
    assert len(rows) == 1 and rows[0]["source"] == "human_feedback"
    assert rows[0]["id"] == sample_id

    # ② 记忆库 —— 能被检索回来
    shots = memory.retrieve("Amul Amul Gold Milk 1L")
    assert any("Amul Gold Milk" in s for s in shots), shots

    # ③ 产品缓存库 —— provenance 升到 human_verified
    rec, _ = cache_store.lookup("Amul", "Amul Gold Milk 1L")
    assert rec and rec["provenance"] == "human_verified"
    assert res["action"] in ("created", "updated", "superseded")


def test_human_verdict_supersedes_an_auto_archive():
    """单向棘轮：人工裁定盖 auto 档案，反向不成立。"""
    aid = "t-sup-" + db.new_id()[:8]
    cache_store.upsert("Amul", "Amul Gold Milk 1L", _evidence(), _final(code=5))
    before, _ = cache_store.lookup("Amul", "Amul Gold Milk 1L")
    assert before["provenance"] == "auto"

    cache_store.supersede_with_human_verdict(
        "Amul", "Amul Gold Milk 1L", _evidence(), _final(code=19), audit_id=aid
    )
    after, _ = cache_store.lookup("Amul", "Amul Gold Milk 1L")
    assert after["provenance"] == "human_verified"
    assert after["revision"] > before["revision"]

    # auto 不能再盖回去
    res = cache_store.upsert("Amul", "Amul Gold Milk 1L", _evidence(), _final(code=5))
    assert res["action"] == "refused"


# --------------------------------------------------------------------------- #
# MEMORY_ENABLED 开关对比
# --------------------------------------------------------------------------- #
def test_memory_switch_controls_injection_at_the_same_call_site(monkeypatch):
    """开/关的差别只应在**注入内容**上，不在代码路径上。

    所以开关放在 `memory.retrieve` 里返回空，而不是在调用点加 if ——
    调用点加 if 会让两臂走不同的代码，比出来的差异就不知道是开关造成的
    还是路径不同造成的。
    """
    aid = "t-switch-" + db.new_id()[:8]
    memory.remember("/img/d.png", _final(), audit_id=aid)
    query = "Amul Amul Gold Milk 1L"

    monkeypatch.setattr(settings, "memory_enabled", True)
    on = memory.retrieve(query)
    monkeypatch.setattr(settings, "memory_enabled", False)
    off = memory.retrieve(query)

    assert on, "开时应召回样例"
    assert off == [], "关时应返回空列表（不是抛错、不是绕过）"


async def test_trace_carries_the_injection_evidence(tmp_path, monkeypatch):
    """"prompt 日志含注入证据"——trace 里要能看到**注进去的是什么**，不只是几条。

    只记条数不够：两次跑批条数相同但内容不同，看起来会一模一样。
    """
    from graph.nodes.classify_initial import classify_initial

    img = tmp_path / "low-cereal.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    memory.remember("/img/e.png", _final(brand="MockBrand", name="Mock Cereal 500g"),
                    audit_id="t-inj-" + db.new_id()[:8])

    monkeypatch.setattr(settings, "memory_enabled", True)
    out = await classify_initial({"audit_id": "a", "ad_image": str(img)})
    step = out["trace"][0]
    assert step.extra["memory_enabled"] is True
    assert step.extra["few_shots_injected"] >= 1
    assert step.extra["few_shots"], "注入内容必须留痕，不能只留条数"

    monkeypatch.setattr(settings, "memory_enabled", False)
    out2 = await classify_initial({"audit_id": "b", "ad_image": str(img)})
    step2 = out2["trace"][0]
    assert step2.extra["memory_enabled"] is False
    assert step2.extra["few_shots_injected"] == 0
    assert "few_shots" not in step2.extra
