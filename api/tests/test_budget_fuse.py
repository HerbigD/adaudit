"""Day 6 任务 0：成本熔断验收测试。

验收要求："人为把预算调到极小值，验证超预算后调用被拒且 trace 有据可查"。
这里不联网 —— 用一个假 provider 冒充真实调用，专测熔断这条闸。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import settings
from services import usage


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """每个用例一本干净账本，且不碰真实 data/usage.json。"""
    monkeypatch.setattr(settings, "usage_path", str(tmp_path / "usage.json"))
    monkeypatch.setattr(settings, "daily_token_budget", 1_000)
    monkeypatch.setattr(settings, "llm_price_in_per_mtok", 2.0)
    monkeypatch.setattr(settings, "llm_price_out_per_mtok", 8.0)
    monkeypatch.setattr(settings, "cost_currency", "CNY")
    yield


# --------------------------------------------------------------------------- #
# 记账
# --------------------------------------------------------------------------- #
def test_records_tokens_and_estimates_cost():
    cost = usage.record("qwen3.7-plus", 1000, 500)
    # 1000/1e6*2 + 500/1e6*8 = 0.002 + 0.004
    assert cost == pytest.approx(0.006)
    snap = usage.snapshot()
    assert snap["tokens_in"] == 1000 and snap["tokens_out"] == 500
    assert snap["total_tokens"] == 1500
    assert snap["currency"] == "CNY"
    assert snap["by_model"]["qwen3.7-plus"]["calls"] == 1


def test_mock_adapters_are_not_billed():
    for m in ("mock-vlm", "rule-fallback", "mock-extract", "human", "cache"):
        usage.record(m, 999_999, 999_999)
    snap = usage.snapshot()
    assert snap["total_tokens"] == 0 and snap["calls"] == 0
    usage.guard("mock-vlm")          # mock 永不熔断


def test_ledger_persists_across_processes(tmp_path):
    """换一个"进程"（重新读文件）后计数还在 —— 跑批脚本与 uvicorn 不是同一个进程。"""
    usage.record("qwen3.7-plus", 400, 100)
    raw = json.loads(Path(settings.usage_path).read_text(encoding="utf-8"))
    assert raw["tokens_in"] == 400 and raw["tokens_out"] == 100
    assert usage.snapshot()["total_tokens"] == 500


# --------------------------------------------------------------------------- #
# 熔断
# --------------------------------------------------------------------------- #
def test_guard_passes_under_budget():
    usage.record("qwen3.7-plus", 400, 100)      # 500 / 1000
    usage.guard("qwen3.7-plus")                 # 不抛


def test_guard_refuses_over_budget():
    usage.record("qwen3.7-plus", 900, 200)      # 1100 > 1000
    with pytest.raises(usage.BudgetExceeded) as e:
        usage.guard("qwen3.7-plus")
    assert "1100/1000" in str(e.value)


def test_snapshot_exposes_budget_state_for_ui():
    usage.record("qwen3.7-plus", 800, 400)
    snap = usage.snapshot()
    assert snap["exceeded"] is True
    assert snap["remaining"] == 0
    assert snap["used_ratio"] > 1.0
    assert snap["budget"] == 1_000


# --------------------------------------------------------------------------- #
# 熔断 → 节点 → trace 全链路
# --------------------------------------------------------------------------- #
async def test_classify_node_refuses_and_records_in_trace(tmp_path, monkeypatch):
    """超预算后 classify_initial 被拒调，trace 里 fallback_reason=budget_exceeded 有据可查。"""
    from graph.nodes.classify_initial import classify_initial

    usage.record("qwen3.7-plus", 900, 200)       # 先把预算烧穿

    async def _should_not_be_called(*a, **k):
        raise AssertionError("熔断后仍然发起了真实调用")

    monkeypatch.setattr(settings, "vlm_provider", "qwen")
    monkeypatch.setattr("services.vlm.QwenVLM._raw_classify", _should_not_be_called)

    img = tmp_path / "ad.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    out = await classify_initial({"audit_id": "t", "ad_image": str(img), "trace": []})

    assert out["route_1"] == "human"
    assert out["initial"] is None
    t = out["trace"][0]
    assert t.status == "fallback"
    assert t.fallback_reason == "budget_exceeded"
    assert "熔断" in t.summary
    assert t.extra["budget"]["exceeded"] is True
    assert t.extra["budget"]["total_tokens"] == 1100


async def test_adjudicate_node_refuses_and_does_not_silently_fall_back(tmp_path, monkeypatch):
    """熔断时不能静默退回规则兜底 —— 那会让"省钱的降级"看起来像正常裁决。"""
    from graph.nodes.adjudicate_with_evidence import adjudicate_with_evidence
    from graph.state import Classification, Evidence, NutrientValue

    usage.record("qwen3.7-plus", 900, 200)
    monkeypatch.setattr(settings, "llm_provider", "qwen")

    initial = Classification(
        brand="B", product_name="P", general_id=1, specific_code=2,
        candidate_codes=[2, 12], specific_confidence=0.5, general_confidence=0.9,
    )
    ev = Evidence(id="ev_001", provenance="web", source_type="official",
                  nutrients=[NutrientValue(nutrient="sugar", value=20, unit="g/100g",
                                           normalized=20)])
    out = await adjudicate_with_evidence(
        {"initial": initial, "evidence": [ev], "search_status": "ok", "trace": []}
    )

    assert out["route_2"] == "human"
    assert "revised" not in out                  # 没有伪造一个裁决结果
    t = out["trace"][0]
    assert t.fallback_reason == "budget_exceeded"
    assert t.extra["budget"]["exceeded"] is True


def test_cost_estimate_follows_config_currency(monkeypatch):
    """换站只改配置：单价与币种变了，估算跟着变，代码不动。"""
    monkeypatch.setattr(settings, "llm_price_in_per_mtok", 0.4)
    monkeypatch.setattr(settings, "llm_price_out_per_mtok", 1.2)
    monkeypatch.setattr(settings, "cost_currency", "USD")
    cost = usage.record("qwen3.7-plus", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.6)
    assert usage.snapshot()["currency"] == "USD"


# --------------------------------------------------------------------------- #
# 嵌套归集（Day6 真跑时发现：内层 collect 曾把外层顶掉，外层永远收 0）
# --------------------------------------------------------------------------- #
def test_nested_collect_bubbles_up_to_every_active_collector():
    with usage.collect() as outer:
        with usage.collect() as inner:
            usage.record("qwen3.7-plus", 100, 50)
        usage.record("qwen3.7-plus", 10, 5)

    assert (inner.tokens_in, inner.tokens_out, inner.calls) == (100, 50, 1)
    assert (outer.tokens_in, outer.tokens_out, outer.calls) == (110, 55, 2)
    assert outer.cost == pytest.approx(usage.estimate_cost(110, 55))


async def test_collect_survives_asyncio_tasks():
    """LangGraph 把节点放进独立 task 跑 —— 外层收集器必须照样收得到。"""
    import asyncio

    async def node():
        with usage.collect():                 # 节点自己也收一份
            usage.record("qwen3.7-plus", 200, 100)

    with usage.collect() as outer:
        await asyncio.gather(asyncio.create_task(node()), asyncio.create_task(node()))

    assert outer.calls == 2
    assert outer.tokens_in == 400 and outer.tokens_out == 200
