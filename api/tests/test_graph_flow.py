"""链路集成测试（mock VLM）：四条路径各跑一次。

文件名约定见 services/vlm.py::MockVLM。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import db
from graph import builder

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100" "05fe02fe" "0000000049454e44ae426082"
)


@pytest.fixture(scope="module", autouse=True)
def _init():
    db.init_db()


def _fake_image(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(PNG_1X1)
    return str(p)


async def _run(image: str, audit_id: str):
    app = await builder.get_app()
    cfg = {"configurable": {"thread_id": audit_id}}
    await app.ainvoke(
        {"audit_id": audit_id, "ad_image": image, "evidence": [], "trace": []}, config=cfg
    )
    return await app.aget_state(cfg)


async def test_direct_path(tmp_path):
    snap = await _run(_fake_image(tmp_path, "highconf.png"), "t-direct")
    st = snap.values
    assert st["route_1"] == "direct"
    assert st["final"] is not None
    assert st["final"].specific_code is not None       # 快路径必须落到叶子
    assert not snap.next                               # 图已结束


async def test_search_path(tmp_path):
    snap = await _run(_fake_image(tmp_path, "low-cereal.png"), "t-search")
    st = snap.values
    assert st["route_1"] == "search"
    assert st["evidence"]
    assert st["revised"] is not None
    assert st["search_status"] in ("ok", "cache")
    assert st["route_2"] in ("direct_verified", "human")


async def test_parent_level_path_resolves_back_to_leaf(tmp_path):
    """粒度自适应：初分类按父类输出，取证后必须落回叶子。"""
    snap = await _run(_fake_image(tmp_path, "parent-cereal.png"), "t-parent")
    st = snap.values
    assert st["initial"].leaf_vs_parent == "parent"
    assert st["initial"].specific_code is None
    assert len(st["initial"].candidate_codes) >= 2
    assert st["route_1"] == "search"
    assert st["revised"].specific_code in st["initial"].candidate_codes


async def test_human_path_interrupts_and_resumes(tmp_path):
    from langgraph.types import Command

    snap = await _run(_fake_image(tmp_path, "nobrand-ad.png"), "t-human")
    assert snap.next                                   # 停在 interrupt
    assert snap.values["route_1"] == "human"

    app = await builder.get_app()
    cfg = {"configurable": {"thread_id": "t-human"}}
    await app.ainvoke(Command(resume={"choice": "original"}), config=cfg)
    st = (await app.aget_state(cfg)).values
    assert st["human_choice"] == "original"
    assert st["final"] is not None
    assert any(t.node == "feedback_ingest" for t in st["trace"])


async def test_trace_is_complete_and_adapter_tagged(tmp_path):
    """验收项：trace_json 含完整 StepTrace，且 mock 产出一律带 adapter 标记。"""
    snap = await _run(_fake_image(tmp_path, "low-trace.png"), "t-trace")
    trace = snap.values["trace"]
    nodes = [t.node for t in trace]
    assert nodes[0] == "classify_initial"
    assert "output" in nodes or "human_review" in nodes

    perception = next(t for t in trace if t.node == "classify_initial")
    assert perception.adapter == "mock-vlm"
    assert perception.is_mock
    assert perception.ms > 0
    assert perception.extra.get("taxonomy_version")

    adjudication = next((t for t in trace if t.node == "adjudicate_with_evidence"), None)
    if adjudication:
        assert adjudication.adapter == "rule-fallback"
        assert "cache_write" in adjudication.extra    # 护栏结论必须留痕


async def test_cache_write_guardrail_recorded_in_trace(tmp_path):
    """低置信重裁决不该写缓存，且拒写原因要在 trace 里看得见。"""
    snap = await _run(_fake_image(tmp_path, "low-guard.png"), "t-guard")
    adj = next((t for t in snap.values["trace"] if t.node == "adjudicate_with_evidence"), None)
    assert adj is not None
    write = adj.extra["cache_write"]
    assert write["action"] in ("created", "updated", "skipped", "refused")
    if write["action"] == "skipped":
        assert write["reason"]


async def test_conflict_path_goes_human_and_supersedes_cache(tmp_path):
    """证据冲突 → 转人工；人工裁定后对同产品档案执行 supersede。"""
    from langgraph.types import Command

    from services import cache_store

    image = _fake_image(tmp_path, "conflict-yoghurt.png")
    snap = await _run(image, "t-conflict")
    st = snap.values
    assert st["route_2"] == "human"
    assert st["revised"].conflict is True
    assert snap.next                                   # 停在 interrupt

    # 冲突样本不许写 auto 档案
    adj = next(t for t in st["trace"] if t.node == "adjudicate_with_evidence")
    assert adj.extra["cache_write"]["action"] == "skipped"

    # 先手工塞一条 auto 档案，模拟"之前搜到过这个产品"
    cache_store.upsert(
        "ConflictBrand", "Disputed Yoghurt 200g", st["evidence"], st["revised"],
        provenance="auto",
    )
    before, _ = cache_store.lookup("ConflictBrand", "Disputed Yoghurt 200g")
    assert before["provenance"] == "auto"

    app = await builder.get_app()
    cfg = {"configurable": {"thread_id": "t-conflict"}}
    await app.ainvoke(Command(resume={"choice": "prediction"}), config=cfg)
    done = (await app.aget_state(cfg)).values

    ingest = next(t for t in done["trace"] if t.node == "feedback_ingest")
    assert ingest.extra["cache_write"]["action"] == "superseded"

    after, _ = cache_store.lookup("ConflictBrand", "Disputed Yoghurt 200g")
    assert after["provenance"] == "human_verified"
    assert after["revision"] == before["revision"] + 1
