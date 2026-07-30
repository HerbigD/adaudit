"""AuditState 与三个 Pydantic 模型（Classification / Evidence / StepTrace）。

一套 schema 同时用于：LLM 结构化输出校验、图 state、JSON 落库。
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

from services import taxonomy

Route1 = Literal["direct", "search", "human"]
Route2 = Literal["direct_verified", "human"]
HumanChoice = Literal["original", "prediction", "manual"]
LeafOrParent = Literal["leaf", "parent"]
SearchStatus = Literal[
    "ok",              # ≥1 条 Evidence 含目标维度 normalized 值
    "degraded",        # 只有降级证据，或全部 query_tier=3 → route_2 阈值上调
    "conflict",        # 落在判定维度上的大分歧 → route_2 强制 human
    "no_result",
    "timeout",
    "budget_exceeded",
    "error",
    "cache",           # 缓存命中，未发起网络调用
    "skipped",
]
Provenance = Literal["auto", "human_verified"]


class Classification(BaseModel):
    """一次分类结果。initial 与 revised 共用此模型 —— 双选项 UI 才能并列展示。

    粒度自适应：叶子置信低而父类置信高时，`specific_code` 置空、`leaf_vs_parent="parent"`，
    候选叶子留在 `candidate_codes` 里 —— 下游搜索取证要靠它锚定"哪两个类在争"。
    """

    product_name: str | None = None
    brand: str | None = None
    name_brand_identifiable: bool = True

    # Day5 §9 硬依赖：搜索链路多语言化靠这两个字段
    ad_language: str = "en"          # ISO 639-1；混合语言取信息密度最高者
    country: str | None = None       # ISO 3166-1 alpha-2；推不出为 None

    general_id: int
    general_category: str = ""                       # 展示名，由 general_id 回填
    specific_code: int | None = None
    candidate_codes: list[int] = Field(default_factory=list)
    leaf_vs_parent: LeafOrParent = "leaf"

    specific_confidence: float = Field(ge=0.0, le=1.0)
    general_confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""

    # 仅 revised 使用：引用 Evidence.id（ev_001…），groundedness 要能核到具体条目
    evidence_refs: list[str] = Field(default_factory=list)
    conflict: bool = False

    # 元信息：adapter 是"这条结果由谁产出"的硬标记（mock / rule-fallback / gemini / …），
    # eval runner 靠它拒绝把 mock 结果当成真实跑批结果。
    source: Literal["vlm", "adjudicator", "human", "cache"] = "vlm"
    model: str | None = None
    adapter: str | None = None

    @model_validator(mode="after")
    def _coerce(self) -> "Classification":
        tx = taxonomy.load()

        code = tx.normalize(self.specific_code) if self.specific_code is not None else None
        if self.specific_code is not None and code is None:
            raise ValueError(f"specific_code {self.specific_code!r} 不在 33 类内")
        object.__setattr__(self, "specific_code", code)

        cands = [c for c in (tx.normalize(x) for x in self.candidate_codes) if c is not None]
        object.__setattr__(self, "candidate_codes", list(dict.fromkeys(cands)))

        # 叶子已定则大类以叶子为准回填，避免模型自造父子关系
        if code is not None:
            object.__setattr__(self, "general_id", tx.specifics[code].parent_id)
        if self.general_id not in tx.generals:
            raise ValueError(f"general_id {self.general_id} 不在 12 大类内")
        object.__setattr__(self, "general_category", taxonomy.general_label(self.general_id))

        if code is None and self.leaf_vs_parent == "leaf":
            object.__setattr__(self, "leaf_vs_parent", "parent")
        if code is not None and self.leaf_vs_parent == "parent":
            object.__setattr__(self, "leaf_vs_parent", "leaf")
        return self

    # ---------- 展示 ----------
    @property
    def specific_name(self) -> str | None:
        s = taxonomy.get(self.specific_code) if self.specific_code else None
        return s.name_zh if s else None

    @property
    def display_level(self) -> LeafOrParent:
        return self.leaf_vs_parent

    def label(self) -> str:
        if self.specific_code is None:
            return f"{self.general_category}（细类待定）"
        return f"[{self.specific_code}] {self.specific_name}"


Nutrient = Literal["sugar", "fat", "fiber", "sodium", "protein"]
SourceType = Literal["official", "ecommerce", "nutrition_db", "cache", "other"]


class NutrientValue(BaseModel):
    """一个营养素读数。原始单位照录，`normalized` 才是裁决节点唯一该看的值。"""

    nutrient: Nutrient
    value: float
    unit: str                       # 原始单位照录，如 "g/100ml" "mg/100g"
    normalized: float | None = None  # 统一到 g/100g（固体）或 g/100ml（液体）
    confidence: float = 0.7


class Evidence(BaseModel):
    """一条营养证据 —— 裁决节点的**契约**，不是文本。

    搜索结果原文不允许直接糊进裁决 prompt：groundedness 指标要求结论能引用到
    具体 Evidence 条目（Day5 §1 原则 2）。
    """

    id: str = ""                    # ev_001 递增，裁决节点引用用
    product_query: str = ""         # 触发本条证据的查询词
    source_url: str = ""
    source_title: str = ""
    source_type: SourceType = "other"
    snippet: str = ""               # 原文片段（≤300 字符，groundedness 核验用）
    nutrients: list[NutrientValue] = Field(default_factory=list)
    conclusion_hint: str | None = None   # 降级模式：LLM 直接给的类别倾向
    provenance: Literal["web", "cache"] = "web"
    query_tier: int = 1             # 3 = 去品牌查询，裁决时降权
    extracted_by: str = ""          # adapter 标记：gemini / mock-extract / rule-fallback
    extracted_at: str = Field(default_factory=lambda: _now_iso())

    # 缓存证据专用：该档案是自动沉淀还是人工核过（Day3 加固 1）
    cache_provenance: Provenance | None = None

    # ---------- 读取 ----------
    def get(self, nutrient: Nutrient) -> float | None:
        """取 normalized 值；没有 normalized（缺份量等）返回 None。"""
        for nv in self.nutrients:
            if nv.nutrient == nutrient and nv.normalized is not None:
                return nv.normalized
        return None

    def raw(self, nutrient: Nutrient) -> NutrientValue | None:
        for nv in self.nutrients:
            if nv.nutrient == nutrient:
                return nv
        return None

    @property
    def is_degraded(self) -> bool:
        """降级证据：没有任何结构化营养读数。"""
        return not self.nutrients

    @property
    def confidence(self) -> float:
        if not self.nutrients:
            return 0.3
        return sum(nv.confidence for nv in self.nutrients) / len(self.nutrients)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StepTrace(BaseModel):
    """每个节点一条。eval 归因、成本核算、失败案例 Top-10 全部从这里取材。"""

    node: str
    status: Literal["ok", "skipped", "fallback", "error"] = "ok"
    ms: int = 0
    summary: str = ""

    # adapter 标记：mock / rule-fallback 一律要打，eval runner 据此拒绝跑批
    adapter: str | None = None

    # 成本与预算（W3 先留空位，接真实 provider 后填）
    queries_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    # 兜底原因：no_result | timeout | conflict | budget_exceeded | invalid_output
    fallback_reason: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    at: float = Field(default_factory=time.time)

    @property
    def is_mock(self) -> bool:
        return bool(self.adapter) and (
            self.adapter.startswith("mock") or self.adapter == "rule-fallback"
        )


def _append(a: list, b: list) -> list:
    return (a or []) + (b or [])


class AuditState(TypedDict, total=False):
    """图状态。字段写入者见方案 §5 表格。"""

    audit_id: str
    ad_image: str                                  # 图片路径/引用，全程只读

    initial: Classification | None                 # classify_initial 写
    route_1: Route1                                # 条件边① 显式落进 state
    evidence: Annotated[list[Evidence], _append]   # 追加式：缓存+搜索可累加
    cache_hit: bool                                # cache_lookup 写
    cache_provenance: Provenance | None            # 命中档案是 auto 还是人工核过
    search_status: SearchStatus | None             # web_search / cache_lookup 写
    revised: Classification | None                 # adjudicate 写，与 initial 并存
    route_2: Route2                                # 条件边② 显式落进 state
    human_choice: HumanChoice | None               # interrupt resume 后填入
    manual_code: int | None                        # human_choice == "manual" 时
    final: Classification | None                   # 收敛点，下游只消费 final
    trace: Annotated[list[StepTrace], _append]     # 全节点追加
    error: str | None
