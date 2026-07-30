"""全局配置：模型选择、置信度阈值、搜索预算。

设计要点（对应方案 §3 工程决策 1）：
置信度阈值不写死在 edge 函数里，全部放这里 —— eval 阶段可以扫参调优。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- 运行模式 ----------
    # mock: 不调任何外部 API，全链路用假数据跑通（W1–W2 验收用）
    app_env: Literal["mock", "dev", "prod"] = "mock"

    # ---------- 模型 ----------
    # vlm.py 统一 classify(image) -> Classification 接口，这里切 provider
    vlm_provider: Literal["mock", "gemini", "qwen", "openai"] = "mock"
    gemini_model: str = "gemini-2.5-flash"
    qwen_model: str = "qwen-vl-max-latest"
    openai_model: str = "gpt-4o"

    gemini_api_key: str | None = None
    dashscope_api_key: str | None = None
    openai_api_key: str | None = None

    # 裁决用的纯文本 LLM（adjudicate / 报告生成），默认复用 VLM provider
    llm_provider: Literal["mock", "gemini", "qwen", "openai"] = "mock"

    # ---------- 置信度阈值（条件边①②） ----------
    direct_threshold: float = 0.85          # 初分类 ≥ 此值 → 直出
    verified_threshold: float = 0.75        # 重裁决 ≥ 此值 → direct_verified
    # 子类低但父类高 → 粒度自适应输出（按父类展示）
    general_fallback_threshold: float = 0.80

    # ---------- 搜索预算（方案 §7 成本控制） ----------
    search_max_queries: int = 3             # 每张广告最多几次查询
    search_timeout_s: float = 10.0          # 单次查询超时
    search_retries: int = 1                 # 预算内重试次数

    # ---------- 存储 ----------
    db_path: str = str(DATA_DIR / "adaudit.db")
    # checkpointer 单独一个库文件：LangGraph 跑图时会长时间持有写事务，
    # 和业务表放同一文件会在 feedback_ingest 这类"节点内写业务表"的地方
    # 直接撞出 `database is locked`。分开后两边互不阻塞，语义不变。
    checkpoint_db_path: str = str(DATA_DIR / "checkpoints.db")
    chroma_path: str = str(DATA_DIR / "chroma")
    upload_dir: str = str(UPLOAD_DIR)

    # ---------- 检索 ----------
    cache_hit_threshold: float = 0.82       # 混合检索相似度阈值，≥ 视为命中
    memory_topk: int = 3                    # few-shot 修正记忆注入条数

    # ---------- 并发 ----------
    max_concurrent_graphs: int = 4          # 同批次并发跑图上限
    vlm_qps: float = 2.0                    # 全局限流器

    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]


settings = Settings()

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
