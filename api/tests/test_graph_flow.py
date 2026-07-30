"""链路集成测试（mock VLM）：三条路径各跑一次。

文件名约定（MockVLM）：
  普通名        → 高置信 → direct
  含 'low'      → 低置信 + 有品牌 → search → 缓存/搜索 → 重裁决
  含 'nobrand'  → 低置信 + 无品牌 → 直接 human（interrupt 挂起）
"""

from __future__ import annotations

from pathlib import Path

import pytest

import db
from graph import builder


@pytest.fixture(scope="module", autouse=True)
def _init():
    db.init_db()


def _fake_image(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    # 1x1 PNG，节点不真读像素（mock provider），但路径必须存在
    p.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000100" "05fe02fe" "0000000049454e44ae426082"
    ))
    return str(p)


async def _run(image: str, audit_id: str):
    app = await builder.get_app()
    cfg = {"configurable": {"thread_id": audit_id}}
    await app.ainvoke(
        {"audit_id": audit_id, "ad_image": image, "evidence": [], "trace": []}, config=cfg
    )
    snap = await app.aget_state(cfg)
    return snap


async def test_direct_path(tmp_path):
    snap = await _run(_fake_image(tmp_path, "highconf.png"), "t-direct")
    st = snap.values
    assert st["route_1"] == "direct"
    assert st["final"] is not None
    assert not snap.next                       # 图已结束


async def test_search_path(tmp_path):
    snap = await _run(_fake_image(tmp_path, "low-cereal.png"), "t-search")
    st = snap.values
    assert st["route_1"] == "search"
    assert st["evidence"]                      # 缓存或搜索至少产出一条证据
    assert st["revised"] is not None
    assert st["route_2"] in ("direct_verified", "human")


async def test_human_path_interrupts_and_resumes(tmp_path):
    from langgraph.types import Command

    image = _fake_image(tmp_path, "nobrand-ad.png")
    snap = await _run(image, "t-human")
    assert snap.next                           # 停在 interrupt
    assert snap.values["route_1"] == "human"

    app = await builder.get_app()
    cfg = {"configurable": {"thread_id": "t-human"}}
    await app.ainvoke(Command(resume={"choice": "original"}), config=cfg)
    st = (await app.aget_state(cfg)).values
    assert st["human_choice"] == "original"
    assert st["final"] is not None
    assert any(t.node == "feedback_ingest" for t in st["trace"])   # 回流已执行
