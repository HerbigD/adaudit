"""FastAPI 入口。LangGraph 嵌在本进程内（不拆独立服务）—— W6 红线是全链路串通。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import db
from config import settings
from graph import builder
from routers import audits, batches, review
from services import cache_store, memory, taxonomy, usage


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await builder.get_app()          # 预热编译 + checkpointer
    yield
    await builder.close_app()


app = FastAPI(title="AdAudit v2 API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audits.router)
app.include_router(review.router)
app.include_router(batches.router)

# 上传的广告图直接静态托管，前端 <img src="/static/uploads/..."> 即可
Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
app.mount("/static/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

meta = APIRouter(prefix="/api", tags=["meta"])


@meta.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "env": settings.app_env,
        "vlm_provider": settings.vlm_provider,
        "llm_provider": settings.llm_provider,
        "search_provider": settings.search_provider,
        "model": settings.vlm_model or settings.qwen_model,
        "usage": usage.snapshot(),
        "thresholds": {
            "direct": settings.direct_threshold,
            "verified": settings.verified_threshold,
            "general_fallback": settings.general_fallback_threshold,
        },
        "search_budget": settings.search_max_queries,
        "taxonomy": {
            "version": taxonomy.load().version,
            "confirmed_ratio": taxonomy.cascade()["confirmed_ratio"],
            **taxonomy.token_report(),
        },
        "cache": cache_store.stats(),
        "memory": memory.stats(),
    }


@meta.get("/usage")
async def get_usage() -> dict[str, Any]:
    """当日 token 记账与预算余量（Day6 任务 0）。UI 顶栏与批次页消费这个。"""
    return usage.snapshot()


@meta.get("/taxonomy")
async def get_taxonomy() -> dict[str, Any]:
    """两级级联选择器数据源（taxonomy.json 的第二份产物）。"""
    return taxonomy.cascade()


@meta.get("/taxonomy/tokens")
async def taxonomy_tokens() -> dict[str, Any]:
    """验收项：taxonomy prompt 块 token 数 ≤ 预算。"""
    report = taxonomy.token_report()
    budget = settings.taxonomy_prompt_token_budget
    return {
        **report,
        "budget": budget,
        "within_budget": report["taxonomy_block"] <= budget,
    }


@meta.get("/graph")
async def graph_diagram() -> dict[str, str]:
    """README 架构图 / 前端调试用的 mermaid 源码。"""
    return {"mermaid": builder.mermaid()}


@meta.get("/eval/metrics")
async def eval_metrics(limit: int = 100) -> dict[str, Any]:
    """W7 用。当前返回评测集规模与占位指标结构。"""
    from eval import dataset

    samples = dataset.load(limit=limit)
    return {
        "dataset_size": len(samples),
        "note": "跑批用 `python -m eval.runner --arm full --limit 300`",
    }


app.include_router(meta)
