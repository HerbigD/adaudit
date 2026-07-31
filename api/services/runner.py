"""跑图 / resume 图 + LangGraph 事件 → 6 种 SSE 事件的翻译层 + 落库。

前端只认这 6 种事件（方案 §4）：
  node_start / node_log / node_end / classified / need_human / done
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langgraph.types import Command

import db
from config import settings
from graph import builder
from graph.state import AuditState
from services import broker

_semaphore = asyncio.Semaphore(settings.max_concurrent_graphs)

# custom stream 里的 kind → SSE event 名
_KIND_MAP = {
    "node_start": "node_start",
    "node_log": "node_log",
    "node_end": "node_end",
    "classified": "classified",
    "need_human": "need_human",
    "done": "done",
}


def _cfg(audit_id: str) -> dict[str, Any]:
    """一条线程一张图：每张广告 = 一个 thread_id = audit_id。"""
    return {"configurable": {"thread_id": audit_id}}


async def _drive(app, payload, audit_id: str) -> None:
    """消费 astream 的 custom 事件并转发到 broker。"""
    async for chunk in app.astream(payload, config=_cfg(audit_id), stream_mode="custom"):
        kind = chunk.get("kind")
        event = _KIND_MAP.get(kind)
        if not event:
            continue
        data = {k: v for k, v in chunk.items() if k != "kind"}
        broker.publish(audit_id, event, data)


async def _persist(app, audit_id: str) -> dict[str, Any]:
    """把图的最终状态写回 audits 表；返回 status。"""
    snap = await app.aget_state(_cfg(audit_id))
    state: AuditState = snap.values or {}
    pending = bool(snap.next)             # 还有待执行节点 = 停在 interrupt

    initial = state.get("initial")
    revised = state.get("revised")
    final = state.get("final")

    if state.get("error") and not final and not pending:
        status = "failed"
    elif pending:
        status = "pending_human"
    elif final is not None:
        status = "done" if state.get("human_choice") else (
            state.get("route_2") or state.get("route_1") or "done"
        )
    else:
        status = "failed"

    db.update_audit(
        audit_id,
        status=status,
        initial_json=initial.model_dump_json() if initial else None,
        revised_json=revised.model_dump_json() if revised else None,
        final_json=final.model_dump_json() if final else None,
        route_1=state.get("route_1"),
        route_2=state.get("route_2"),
        human_choice=state.get("human_choice"),
        trace_json=json.dumps(
            [t.model_dump(mode="json") for t in state.get("trace", [])], ensure_ascii=False
        ),
    )

    # Day7：补齐缓存命中的后续路由与人工结果。
    # 放这里而不是节点里：图有两个终点（output→END 与 feedback_ingest→END），
    # 节点里写就得写两处、还会漏掉 interrupt 挂起的中间态；
    # `_persist` 是 start 与 resume 都必经的**唯一**落库点。
    # 未命中的审计不会有对应行，`finalize_cache_hit` 的 UPDATE 自然空转。
    if state.get("cache_hit"):
        db.finalize_cache_hit(
            audit_id,
            route_1=state.get("route_1"),
            route_2=state.get("route_2"),
            human_choice=state.get("human_choice"),
            cached_code=revised.specific_code if revised else None,
            final_code=final.specific_code if final else None,
        )
    return {"status": status, "state": state}


async def start(audit_id: str, image_path: str) -> None:
    """后台跑图。上传接口 fire-and-forget 调用它。"""
    async with _semaphore:
        app = await builder.get_app()
        db.update_audit(audit_id, status="running")
        try:
            await _drive(
                app,
                {"audit_id": audit_id, "ad_image": image_path, "evidence": [], "trace": []},
                audit_id,
            )
            result = await _persist(app, audit_id)
            if result["status"] != "pending_human":
                broker.publish(audit_id, "done", _done_payload(result["state"]))
        except Exception as exc:  # noqa: BLE001
            db.update_audit(audit_id, status="failed")
            broker.publish(audit_id, "node_log", {"node": "graph", "msg": f"执行失败：{exc}"})
        finally:
            broker.close(audit_id)


async def resume(audit_id: str, choice: str, manual_code: int | None = None) -> dict[str, Any]:
    """人工裁定后恢复执行（human_review 的 interrupt 之后）。"""
    app = await builder.get_app()
    broker.reopen(audit_id)
    db.update_audit(audit_id, status="running")
    try:
        await _drive(
            app, Command(resume={"choice": choice, "manual_code": manual_code}), audit_id
        )
        result = await _persist(app, audit_id)
        broker.publish(audit_id, "done", _done_payload(result["state"]))
        return result
    finally:
        broker.close(audit_id)


def _done_payload(state: AuditState) -> dict[str, Any]:
    final = state.get("final")
    return {
        "final": final.model_dump(mode="json") if final else None,
        "route": state.get("route_2") or state.get("route_1"),
        "human_choice": state.get("human_choice"),
    }


async def get_state(audit_id: str) -> AuditState:
    app = await builder.get_app()
    snap = await app.aget_state(_cfg(audit_id))
    return snap.values or {}
