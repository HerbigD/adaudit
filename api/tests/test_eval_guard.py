"""Day 3 加固 3：eval runner 的 mock 断言位。"""

from __future__ import annotations

import pytest

from eval import metrics
from eval.runner import MockResultRefused, _postflight, _preflight


def pred(adapters: tuple[str, ...]) -> metrics.Prediction:
    return metrics.Prediction(
        audit_id="a",
        gold_specific=12,
        initial_specific=12,
        final_specific=12,
        initial_confidence=0.9,
        final_confidence=0.9,
        route_1="direct",
        route_2=None,
        used_evidence=False,
        cache_hit=False,
        adapters=adapters,
    )


def test_preflight_refuses_mock_config():
    """默认配置就是 mock —— 跑批必须被拦住。"""
    with pytest.raises(MockResultRefused) as e:
        _preflight(allow_mock=False)
    assert "VLM_PROVIDER=mock" in str(e.value)


def test_preflight_allows_with_flag_but_returns_warnings():
    warnings = _preflight(allow_mock=True)
    assert warnings and any("mock" in w.lower() for w in warnings)


def test_postflight_refuses_tainted_predictions():
    preds = [pred(("gemini",)), pred(("mock-vlm",))]
    with pytest.raises(MockResultRefused) as e:
        _postflight(preds, allow_mock=False)
    assert "mock-vlm" in str(e.value)


def test_postflight_refuses_rule_fallback():
    with pytest.raises(MockResultRefused):
        _postflight([pred(("gemini", "rule-fallback"))], allow_mock=False)


def test_postflight_passes_for_real_adapters():
    _postflight([pred(("gemini",)), pred(("qwen",))], allow_mock=False)


def test_summarize_exposes_adapter_provenance():
    s = metrics.summarize([pred(("mock-vlm",)), pred(("gemini",))])
    assert s["contains_mock"] is True
    assert s["adapters"] == ["gemini", "mock-vlm"]
