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
    # Day6：统一单一模型全链路（多模态），classify 传图、抽取/裁决传文本
    qwen_model: str = "qwen3.7-plus"
    openai_model: str = "gpt-4o"

    # 分别覆盖能力：留空则都用 qwen_model
    vlm_model: str | None = None
    llm_model: str | None = None

    # 非思考模式 —— 分类/抽取是结构化任务，thinking 只会拖长延迟与 token
    qwen_enable_thinking: bool = False
    # JSON mode 探测：auto = 先试 response_format，400 则降级到 prompt 契约 + 运行时校验
    qwen_json_mode: Literal["auto", "on", "off"] = "auto"

    gemini_api_key: str | None = None
    dashscope_api_key: str | None = None
    openai_api_key: str | None = None
    # 中国站 / 国际站；换站只改这一行
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 裁决用的纯文本 LLM（adjudicate / 报告生成），默认复用 VLM provider
    llm_provider: Literal["mock", "gemini", "qwen", "openai"] = "mock"

    # ---------- 成本熔断（Day6 任务 0，先行于任何真实调用） ----------
    # 主熔断用 token 而不是钱：token 无货币歧义，是 provider 直接返回的事实。
    # 所有真实调用的 tokens_in+out 累计落 data/usage.json（跨进程不丢），
    # 超预算立即拒调并落 trace（fallback_reason=budget_exceeded）。
    daily_token_budget: int = 500_000
    usage_path: str = str(DATA_DIR / "usage.json")
    # 成本估算配置化。币种跟随百炼账单：中国站 = CNY、国际站 = USD，只改配置不改代码。
    # 百炼是阶梯价（按上下文长度分档），这里只填一个数 —— 填实际会落到的那一档。
    # 默认值 = qwen3.7-plus 国际站最短档（$0.32/$1.28 per Mtok，2026-07）；
    # 本项目 prompt 约 1.5k–4k token，落最短档，所以估算值是**下限**。
    llm_price_in_per_mtok: float = 0.32
    llm_price_out_per_mtok: float = 1.28
    cost_currency: str = "USD"

    # ---------- 搜索后端 ----------
    # dashscope = 百炼内置联网（复用同一把 key）；mock = 离线假数据
    search_provider: Literal["mock", "dashscope"] = "mock"

    # ---------- 置信度阈值（条件边①②） ----------
    direct_threshold: float = 0.85          # 初分类 ≥ 此值 → 直出

    # 重裁决 ≥ 此值 → direct_verified。
    # **刻意低于 DIRECT_THRESHOLD（0.85）**，这不是笔误：
    # 初分类只有"看图"一个信息源，0.85 是在要求模型对纯视觉判断非常笃定；
    # 重裁决多了一层外部营养证据，同样的 0.75 背后实际信息量更大 —— 证据加持
    # 本就该换来更低的直出门槛，否则取证白做，慢路径样本会全部堆到人工。
    # 反过来，证据质量不够时（search_status=degraded）这个门槛会被 +0.05 抬回去，
    # 见 edges.verified_threshold_for()。
    verified_threshold: float = 0.75
    # 子类低但父类高 → 粒度自适应输出（按父类展示）
    general_fallback_threshold: float = 0.80

    # ---------- 搜索预算（方案 §7 / Day5 §4） ----------
    search_max_queries: int = 3             # 每张广告最多几次查询
    search_timeout_s: float = 10.0          # 单次查询超时
    search_total_timeout_s: float = 25.0    # 整链路上限（含抽取）
    search_retries: int = 1                 # 预算内重试次数（仅超时/网络错误）
    search_candidates_topk: int = 3         # 进 LLM 抽取的候选数
    search_hits_per_query: int = 5          # 每条查询取 top-N 进筛选
    max_evidence: int = 5                   # 一次取证最多产出几条 Evidence
    degraded_threshold_bump: float = 0.05   # degraded 时 route_2 阈值上调
    conflict_relative_gap: float = 0.50     # 冲突判定：相对偏差阈值
    default_country: str | None = None      # 推不出国家时的域名表兜底（None = 只用 _global）

    # ---------- 数据驱动词表（新增国家只改 JSON） ----------
    sources_by_country_path: str = str(
        Path(__file__).resolve().parent / "data" / "sources_by_country.json"
    )
    category_terms_path: str = str(
        Path(__file__).resolve().parent / "data" / "category_terms.json"
    )

    # ---------- Taxonomy ----------
    # 单一事实来源，代码里不另造分类数据
    taxonomy_path: str = str(Path(__file__).resolve().parent / "data" / "taxonomy.json")
    taxonomy_prompt_token_budget: int = 2000

    # ---------- A3 消融：prompt 里放哪些混淆对 ----------
    # A  = 不放任何对（回答"置信度信号是不是模型内生的"）
    # B  = 仅 Tier 1 definitional（共享数值切分线，从 Annex 4 阈值自动推出）
    # B2 = Tier 1 + Tier 2 definitional_compositional —— **线上默认**
    # C  = 再加 Tier 3 dev 经验对（只准在 held-out 上报，且报告里必须声明）
    pairs_arm: Literal["A", "B", "B2", "C"] = "B2"

    # ---------- A2 数据切分（Day6 决议） ----------
    # 单标签金标池 → dev 200 / eval 300，按 country（有 language 时再叠 language）分层。
    # **种子写进配置**：切分必须可复现，否则"我们没看过测试集"这句话无从验证。
    split_seed: int = 20260731
    split_dev_size: int = 200
    split_eval_size: int = 300
    split_smoke_size: int = 12          # 冒烟集，与 dev/eval 互斥
    split_dir: str = str(Path(__file__).resolve().parent / "data" / "splits")

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
