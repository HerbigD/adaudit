"""上传 / 查询 / SSE 流。"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

import db
from config import settings
from services import broker, runner

router = APIRouter(prefix="/api/audits", tags=["audits"])

ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _save(upload: UploadFile) -> str:
    ext = Path(upload.filename or "ad.jpg").suffix.lower() or ".jpg"
    if ext not in ALLOWED:
        raise HTTPException(400, f"不支持的图片格式: {ext}")
    # 保留原名词干，mock VLM 靠它路由（含 'low' / 'nobrand' 走不同分支）
    stem = Path(upload.filename or "ad").stem[:40]
    dest = Path(settings.upload_dir) / f"{stem}-{uuid.uuid4().hex[:8]}{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return str(dest)


@router.post("")
async def create_audits(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    batch_name: str | None = Form(None),
) -> dict[str, Any]:
    """上传图片（单张或多张=建批次），创建 audit 记录并异步启动图。"""
    if not files:
        raise HTTPException(400, "没有上传文件")

    batch_id = db.create_batch(batch_name) if (len(files) > 1 or batch_name) else None
    created: list[dict[str, str]] = []
    for upload in files:
        path = _save(upload)
        audit_id = db.create_audit(path, batch_id)
        created.append({"audit_id": audit_id, "image_path": path})
        background.add_task(runner.start, audit_id, path)

    return {
        "batch_id": batch_id,
        "audits": created,
        "redirect": f"/batches/{batch_id}" if batch_id else f"/audits/{created[0]['audit_id']}",
    }


@router.get("/{audit_id}")
async def get_audit(audit_id: str) -> dict[str, Any]:
    row = db.get_audit(audit_id)
    if not row:
        raise HTTPException(404, "audit 不存在")
    return row


@router.get("/{audit_id}/stream")
async def stream_audit(audit_id: str) -> StreamingResponse:
    """SSE：Agent 过程实时流。事件类型见方案 §4（6 种）。"""
    if not db.get_audit(audit_id):
        raise HTTPException(404, "audit 不存在")

    async def gen():
        try:
            async for item in broker.channel(audit_id).subscribe():
                event = item["event"]
                data = json.dumps(item["data"], ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"
        except asyncio.CancelledError:  # 客户端断开
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("")
async def list_audits(status: str | None = None, batch_id: str | None = None) -> list[dict]:
    return db.list_audits(status=status, batch_id=batch_id)
