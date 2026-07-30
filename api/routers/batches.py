"""批次聚合 / 报告生成 / 跨批次看板。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
from services import report

router = APIRouter(prefix="/api/batches", tags=["batches"])


class BatchCreate(BaseModel):
    name: str | None = None


@router.post("")
async def create_batch(payload: BatchCreate) -> dict[str, str]:
    return {"batch_id": db.create_batch(payload.name)}


@router.get("")
async def list_batches() -> list[dict[str, Any]]:
    return db.list_batches()


@router.get("/{batch_id}")
async def get_batch(batch_id: str) -> dict[str, Any]:
    row = db.get_batch(batch_id)
    if not row:
        raise HTTPException(404, "批次不存在")
    audits = db.list_audits(batch_id=batch_id, limit=100000)
    stats = report.aggregate(batch_id)
    pending = sum(1 for a in audits if a["status"] == "pending_human")
    status = "report_ready" if row.get("report_md") else (
        "review_pending" if pending else
        ("processing" if any(a["status"] in ("queued", "running") for a in audits) else "review_pending")
    )
    db.update_batch(batch_id, status=status)
    return {**row, "status": status, "stats": stats, "audits": audits, "pending_human": pending}


@router.post("/{batch_id}/report")
async def make_report(batch_id: str) -> dict[str, Any]:
    if not db.get_batch(batch_id):
        raise HTTPException(404, "批次不存在")
    md = await report.generate_report(batch_id)
    return {"batch_id": batch_id, "report_md": md, "stats": report.aggregate(batch_id)}


@router.get("/{batch_id}/trend")
async def trend(batch_id: str) -> dict[str, Any]:
    """跨批次看板曲线：人工复核率下降 + 缓存命中率上升（记忆机制生效的证据）。"""
    points = []
    for b in reversed(db.list_batches(limit=30)):
        s = report.aggregate(b["id"])
        points.append(
            {
                "batch_id": b["id"],
                "name": b["name"],
                "created_at": b["created_at"],
                "human_review_rate": s["human_review_rate"],
                "search_trigger_rate": s["search_trigger_rate"],
                "cache_products": s["cache"]["products"],
                "cache_hits": s["cache"]["total_hits"],
            }
        )
    return {"points": points, "current": batch_id}
