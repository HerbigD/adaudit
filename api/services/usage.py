"""真实调用的 token 记账与成本熔断（Day6 任务 0，先行于任何真实调用）。

## 为什么主熔断是 token 而不是钱

token 没有货币歧义：同一把 key 打到中国站计 CNY、打到国际站计 USD，
汇率、阶梯价、促销折扣都可能变，唯独"这次调用烧了多少 token"是 provider 直接返回的事实。
所以 `daily_token_budget` 是**硬闸**，成本只是拿配置里的单价换算出来的**估算值**，
供人看、不参与拦截决策。

## 跨进程不丢

uvicorn 可能多 worker、跑批脚本又是另一个进程，计数只放内存必然漏。
落 `data/usage.json`，每次 read-modify-write 走 `fcntl.flock` 排他锁 + 原子替换。

## 用法

    usage.guard(model)                       # 调用前：超预算直接抛 BudgetExceeded
    usage.record(model, tokens_in, tokens_out)   # 调用后：记账

    with usage.collect() as u:               # 节点里：把这一段的用量收进 StepTrace
        result = await vlm.classify(...)
    t.tokens_in, t.tokens_out, t.cost_usd = u.tokens_in, u.tokens_out, u.cost
"""

from __future__ import annotations

import contextvars
import fcntl
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import settings

logger = logging.getLogger(__name__)

# 记账只针对真实 provider；mock / 规则兜底不烧钱也不占预算
MOCK_MODELS = {"mock", "mock-vlm", "mock-llm", "mock-extract", "mock-search",
               "rule-fallback", "human", "cache", "seed", "degraded"}


class BudgetExceeded(RuntimeError):
    """当日 token 预算已用尽，拒绝发起真实调用。"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _path() -> Path:
    return Path(settings.usage_path)


def _blank(date: str) -> dict[str, Any]:
    return {"date": date, "calls": 0, "tokens_in": 0, "tokens_out": 0,
            "cost": 0.0, "currency": settings.cost_currency, "by_model": {}}


# --------------------------------------------------------------------------- #
# 持久化（跨进程）
# --------------------------------------------------------------------------- #
@contextmanager
def _locked() -> Iterator[dict[str, Any]]:
    """排他锁下读出当日账本，退出时原子写回。跨日自动归零。"""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = p.with_suffix(".lock")
    with lock.open("a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — 文件不存在/损坏都从零开始
                data = _blank(_today())
            if data.get("date") != _today():
                data = _blank(_today())
            yield data
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def snapshot() -> dict[str, Any]:
    """当前账本 + 预算余量。UI 与 /api/usage 直接消费。"""
    with _locked() as data:
        used = data["tokens_in"] + data["tokens_out"]
        budget = settings.daily_token_budget
        return {
            **data,
            "total_tokens": used,
            "budget": budget,
            "remaining": max(0, budget - used),
            "used_ratio": round(used / budget, 4) if budget else 0.0,
            "exceeded": used >= budget,
            "price_in_per_mtok": settings.llm_price_in_per_mtok,
            "price_out_per_mtok": settings.llm_price_out_per_mtok,
        }


def estimate_cost(tokens_in: int, tokens_out: int) -> float:
    """按配置单价估算。单价与币种跟随账单口径，只改配置不改代码。"""
    return round(
        tokens_in / 1e6 * settings.llm_price_in_per_mtok
        + tokens_out / 1e6 * settings.llm_price_out_per_mtok,
        6,
    )


# --------------------------------------------------------------------------- #
# 熔断与记账
# --------------------------------------------------------------------------- #
def is_mock(model: str | None) -> bool:
    m = (model or "").lower()
    return not m or m in MOCK_MODELS or m.startswith("mock")


def guard(model: str | None = None) -> None:
    """真实调用前的闸门。超预算抛 BudgetExceeded —— 调用方负责落 trace 并转人工。"""
    if is_mock(model):
        return
    snap = snapshot()
    if snap["exceeded"]:
        raise BudgetExceeded(
            f"当日 token 预算已用尽：{snap['total_tokens']}/{snap['budget']}"
            f"（估算成本 {snap['cost']:.4f} {snap['currency']}）。"
            f"调高 DAILY_TOKEN_BUDGET 或等次日归零。"
        )


def record(model: str, tokens_in: int, tokens_out: int) -> float:
    """调用后记账，返回本次估算成本。mock 不入账。"""
    if is_mock(model):
        return 0.0
    tokens_in, tokens_out = int(tokens_in or 0), int(tokens_out or 0)
    cost = estimate_cost(tokens_in, tokens_out)
    with _locked() as data:
        data["calls"] += 1
        data["tokens_in"] += tokens_in
        data["tokens_out"] += tokens_out
        data["cost"] = round(data["cost"] + cost, 6)
        data["currency"] = settings.cost_currency
        m = data["by_model"].setdefault(
            model, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0}
        )
        m["calls"] += 1
        m["tokens_in"] += tokens_in
        m["tokens_out"] += tokens_out
        m["cost"] = round(m["cost"] + cost, 6)
        used = data["tokens_in"] + data["tokens_out"]
    if used >= settings.daily_token_budget:
        logger.warning("当日 token 预算已达上限：%s/%s", used, settings.daily_token_budget)
    _bump_ctx(tokens_in, tokens_out, cost)
    return cost


# --------------------------------------------------------------------------- #
# 节点级归集（写进 StepTrace）
# --------------------------------------------------------------------------- #
@dataclass
class Collected:
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    calls: int = 0
    models: list[str] = field(default_factory=list)


# **栈**而不是单个槽：collect() 会嵌套（脚本收整轮、节点各收自己那一段），
# 只留一个槽的话内层会把外层顶掉，外层永远收到 0。
# 用 tuple 存活跃收集器，每次记账**逐个**加上去。
# 跨 asyncio 任务也成立：子任务拿到的是父 context 的拷贝，但 Collected 对象是同一个，
# 所以 LangGraph 把节点放进独立 task 跑，外层依然收得到。
_CTX: contextvars.ContextVar[tuple[Collected, ...]] = contextvars.ContextVar(
    "usage_ctx", default=()
)


def _bump_ctx(tokens_in: int, tokens_out: int, cost: float) -> None:
    for c in _CTX.get():
        c.tokens_in += tokens_in
        c.tokens_out += tokens_out
        c.cost = round(c.cost + cost, 6)
        c.calls += 1


@contextmanager
def collect() -> Iterator[Collected]:
    """归集这一段代码里发生的全部真实调用用量（可嵌套，asyncio 安全）。"""
    c = Collected()
    token = _CTX.set(_CTX.get() + (c,))
    try:
        yield c
    finally:
        _CTX.reset(token)


def note_model(model: str) -> None:
    for c in _CTX.get():
        if model not in c.models:
            c.models.append(model)


# --------------------------------------------------------------------------- #
# 测试用
# --------------------------------------------------------------------------- #
def reset() -> None:
    with _locked() as data:
        data.clear()
        data.update(_blank(_today()))
