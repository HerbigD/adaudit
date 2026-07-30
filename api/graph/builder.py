"""StateGraph 组装 + SQLite checkpointer。

与方案 §3 代码片段的唯一差异：`direct` / `direct_verified` 不直接连 END，
而是先经过 `output` 节点收敛 final —— 方案 §5 要求"图结束前 final 必定有值，
下游只消费 final"，两条快路径必须共用同一个收敛点。

主干无环：这不是 ReAct 式循环 Agent，而是有向无环状态机，路径由数据决定。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from langgraph.graph import END, START, StateGraph

from config import settings
from graph import edges, nodes
from graph.state import AuditState

_compiled = None
_saver_cm = None


def build() -> StateGraph:
    g = StateGraph(AuditState)

    g.add_node("classify_initial", nodes.classify_initial)
    g.add_node("cache_lookup", nodes.cache_lookup)
    g.add_node("web_search", nodes.web_search)
    g.add_node("adjudicate_with_evidence", nodes.adjudicate_with_evidence)
    g.add_node("human_review", nodes.human_review)      # 内部 interrupt()
    g.add_node("feedback_ingest", nodes.feedback_ingest)
    g.add_node("output", nodes.output)

    g.add_edge(START, "classify_initial")

    # 条件边①：快路径 / 取证路径 / 直接人工
    g.add_conditional_edges(
        "classify_initial",
        edges.route_1,
        {"direct": "output", "search": "cache_lookup", "human": "human_review"},
    )
    # 缓存命中就不发网络调用
    g.add_conditional_edges(
        "cache_lookup",
        edges.cache_hit,
        {"hit": "adjudicate_with_evidence", "miss": "web_search"},
    )
    g.add_edge("web_search", "adjudicate_with_evidence")

    # 条件边②：经搜索验证直出 / 转人工（含搜索兜底）
    g.add_conditional_edges(
        "adjudicate_with_evidence",
        edges.route_2,
        {"direct_verified": "output", "human": "human_review"},
    )

    g.add_edge("human_review", "feedback_ingest")
    g.add_edge("feedback_ingest", END)
    g.add_edge("output", END)
    return g


async def get_app():
    """进程内单例。checkpointer 让 pending_human 的图实例落在 SQLite —— 不需要自己实现任务队列。"""
    global _compiled, _saver_cm
    if _compiled is not None:
        return _compiled

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from graph.state import Classification, Evidence, StepTrace

    # 显式登记会进 checkpoint 的自定义类型：不登记只是打 warning，
    # 但未来版本会直接拒绝反序列化（LANGGRAPH_STRICT_MSGPACK）。
    _saver_cm = AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path)
    saver = await _saver_cm.__aenter__()
    saver.serde = JsonPlusSerializer(
        allowed_msgpack_modules=[Classification, Evidence, StepTrace]
    )
    _compiled = build().compile(checkpointer=saver)
    return _compiled


async def close_app() -> None:
    global _compiled, _saver_cm
    if _saver_cm is not None:
        await _saver_cm.__aexit__(None, None, None)
    _compiled, _saver_cm = None, None


def mermaid() -> str:
    """README 架构图用。"""
    return build().compile().get_graph().draw_mermaid()
