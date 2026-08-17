"""节点 → SSE 的事件通道。

用 LangGraph 的 custom stream（`get_stream_writer()`）而不是自己传回调：
节点里 `await emit_log(...)` 写出的消息，会以 stream_mode="custom" 出现在
`app.astream(...)` 里，路由层再翻译成方案 §4 定义的 6 种 SSE 事件。
前端因此不需要懂 LangGraph。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from graph.state import StepTrace


def _writer():
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # noqa: BLE001 — 图外直接调用节点（单测）时没有 writer
        return None


def emit(kind: str, **payload: Any) -> None:
    w = _writer()
    if w:
        w({"kind": kind, **payload})


async def emit_log(node: str, msg: str) -> None:
    """→ SSE `node_log`，承载「正在搜索 XX 品牌营养成分…」这类人性化过程展示。"""
    emit("node_log", node=node, msg=msg)


@contextmanager
def step(node: str, summary: str = "") -> Iterator[StepTrace]:
    """节点计时 + trace 记录 + node_start/node_end 事件。

    用法：
        with step("web_search") as t:
            ...
            t.summary = "命中 2 条"
            t.queries_used = 2
    """
    emit("node_start", node=node)
    t = StepTrace(node=node, summary=summary)
    started = time.perf_counter()
    try:
        yield t
    except Exception as exc:  # noqa: BLE001
        t.status = "error"
        t.summary = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        t.ms = int((time.perf_counter() - started) * 1000)
        emit(
            "node_end",
            node=node,
            ms=t.ms,
            status=t.status,
            summary=t.summary,
            fallback_reason=t.fallback_reason,
            # `extra` 一并推给前端：时间线要区分"缓存命中跳过搜索"与"正在搜索"，
            # 靠的是 extra.cache_id / extra.strict_rejected 这类**结构化标记**，
            # 不是去 summary 字符串里找"缓存命中"四个字。
            # 教训见 services/taxonomy.py 的 HFSS_VERDICTS —— 从文案推语义那次。
            extra=t.extra or {},
        )
