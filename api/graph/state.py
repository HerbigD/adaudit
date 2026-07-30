"""AuditState 与三个 Pydantic 模型（Classification / Evidence / StepTrace）。

一套 schema 同时用于：LLM 结构化输出校验、图 state、JSON 落库。
"""

from __future__ import annotations

import time
from operator import add
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

from services import taxonomy

Route1 = Literal["direct", "search", "human"]
Route2 = Literal["direct_verified", "human"]
HumanChoice = Literal["original", "prediction", "manual"]


class Classification(BaseModel):
    """一次分类结果。initial 与 revised 共用此模型 —— 双选项 UI 才能并列展示。"""

    product_name: str | None = None
    brand: str | None = None
    general_category: str
    specific_code: int
    specific_confidence: float = Field(ge=0.0, le=1.0)
    general_confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    alternative_code: int | None = None
    name_or_brand_legible: bool = True

    # 仅 revised 使用
    evidence_refs: list[int] = Field(default_factory=list)
    conflict: bool = False

    # 元信息
    source: Literal["vlm", "adjudicator", "human", "cache"] = "vlm"
    model: str | None = None

    @field_validator("specific_code")
    @classmethod
    def _valid_code(cls, v: int) -> int:
        if not taxonomy.is_valid(v):
            raise ValueError(f"specific_code {v} 不在 33 类内")
        return v

    @property
    def specific_name(self) -> str:
        return taxonomy.BY_CODE[self.specific_code].name

    @property
    def display_level(self) -> Literal["specific", "general"]:
        """粒度自适应输出：子类低置信但父类高置信时，按父类展示。"""
        from config import settings

        if (
            self.specific_confidence < settings.direct_threshold
            and self.general_confidence >= settings.general_fallback_threshold
        ):
            return "general"
        return "specific"


class Evidence(BaseModel):
    """一条营养证据。来源可能是缓存档案，也可能是联网搜索抽取。"""

    source: Literal["cache", "web"]
    url: str | None = None
    title: str | None = None
    snippet: str | None = None

    # 结构化营养（每 100g / 100ml）
    sugar_g: float | None = None
    fat_g: float | None = None
    sat_fat_g: float | None = None
    fibre_g: float | None = None
    salt_g: float | None = None
    energy_kj: float | None = None

    confidence: float = 0.5
    retrieved_at: float = Field(default_factory=time.time)


class StepTrace(BaseModel):
    """每个节点一条。eval 归因、成本核算、失败案例 Top-10 全部从这里取材。"""

    node: str
    status: Literal["ok", "skipped", "fallback", "error"] = "ok"
    ms: int = 0
    summary: str = ""
    # 成本与预算
    queries_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    # 兜底原因：no_result | timeout | conflict | budget_exceeded | invalid_output
    fallback_reason: str | None = None
    at: float = Field(default_factory=time.time)


class AuditState(TypedDict, total=False):
    """图状态。字段写入者见方案 §5 表格。"""

    audit_id: str
    ad_image: str                                  # 图片路径/引用，全程只读

    initial: Classification | None                 # classify_initial 写
    route_1: Route1                                # 条件边① 显式落进 state
    evidence: Annotated[list[Evidence], add]       # 追加式 reducer：缓存+搜索可累加
    cache_hit: bool                                # cache_lookup 写
    revised: Classification | None                 # adjudicate 写，与 initial 并存
    route_2: Route2                                # 条件边② 显式落进 state
    human_choice: HumanChoice | None               # interrupt resume 后填入
    manual_code: int | None                        # human_choice == "manual" 时
    final: Classification | None                   # 收敛点，下游只消费 final
    trace: Annotated[list[StepTrace], add]         # 全节点追加
    error: str | None
