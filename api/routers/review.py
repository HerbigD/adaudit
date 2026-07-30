"""人工复核队列 + 裁定 resume。

队列不需要自己实现任务系统：checkpointer 让 interrupt 挂起的图存在 SQLite，
队列页直接查 audits.status='pending_human'。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
from services import runner, taxonomy

router = APIRouter(prefix="/api/review", tags=["review"])


class Decision(BaseModel):
    choice: Literal["original", "prediction", "manual"]
    manual_code: int | None = None


def _reason(row: dict[str, Any]) -> str:
    """卡点原因：先看 trace 里的兜底原因，再看是否无锚点（与 human_review 节点一致）。"""
    reason = _reason_from_trace(row.get("trace"))
    if reason == "置信度不足":
        initial = row.get("initial") or {}
        if initial and not initial.get("name_or_brand_legible", True):
            return "无法识别产品名/品牌"
    return reason


def _reason_from_trace(trace: list[dict] | None) -> str:
    labels = {
        "no_result": "搜索无结果",
        "timeout": "搜索超时",
        "budget_exceeded": "搜索预算耗尽",
        "conflict": "证据冲突",
        "invalid_output": "感知输出非法",
        "error": "取证异常",
    }
    for step in reversed(trace or []):
        if step.get("fallback_reason"):
            return labels.get(step["fallback_reason"], step["fallback_reason"])
    return "置信度不足"


@router.get("/queue")
async def queue() -> list[dict[str, Any]]:
    rows = db.list_audits(status="pending_human")
    for row in rows:
        row["reason"] = _reason(row)
    return rows


@router.get("/taxonomy")
async def get_taxonomy() -> dict[str, Any]:
    """33 类级联选择器的数据源。"""
    return {
        "generals": taxonomy.GENERAL_CATEGORIES,
        "specifics": [
            {"code": c.code, "name": c.name, "general": c.general}
            for c in taxonomy.SPECIFIC_CATEGORIES
        ],
    }


@router.get("/{audit_id}")
async def detail(audit_id: str) -> dict[str, Any]:
    row = db.get_audit(audit_id)
    if not row:
        raise HTTPException(404, "audit 不存在")
    state = await runner.get_state(audit_id)
    row["evidence"] = [e.model_dump(mode="json") for e in state.get("evidence", [])]
    row["reason"] = _reason(row)
    return row


@router.post("/{audit_id}/decide")
async def decide(audit_id: str, decision: Decision) -> dict[str, Any]:
    """人工裁定 → resume 图 → feedback_ingest 回流 → END。"""
    row = db.get_audit(audit_id)
    if not row:
        raise HTTPException(404, "audit 不存在")
    if row["status"] != "pending_human":
        raise HTTPException(409, f"当前状态 {row['status']} 不可裁定")
    if decision.choice == "manual" and not taxonomy.is_valid(decision.manual_code):
        raise HTTPException(400, "manual_code 不在 33 类内")

    result = await runner.resume(audit_id, decision.choice, decision.manual_code)
    final = result["state"].get("final")
    return {
        "audit_id": audit_id,
        "status": result["status"],
        "final": json.loads(final.model_dump_json()) if final else None,
        "ingested": ["eval 集", "修正记忆库", "产品缓存库"],
    }
