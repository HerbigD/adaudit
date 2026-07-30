"""搜索结果 → Evidence 抽取（糖 / 脂 / 纤维 / 盐）。

W1–W2 用正则打底（对英文营养成分表 recall 已经不低）；
TODO(W4): 正则抽不到时降级到一次小模型抽取（结构化输出），并把 confidence 拉低。
"""

from __future__ import annotations

import re

from graph.state import Evidence
from services.search import SearchHit

_NUM = r"([\d]+(?:[.,]\d+)?)"

PATTERNS: dict[str, list[str]] = {
    "sugar_g": [rf"sugars?\D{{0,20}}{_NUM}\s*g", rf"糖\D{{0,10}}{_NUM}\s*克?g?"],
    "fat_g": [rf"(?<!saturated )fat\D{{0,20}}{_NUM}\s*g", rf"脂肪\D{{0,10}}{_NUM}\s*克?g?"],
    "sat_fat_g": [rf"saturat\w*\D{{0,20}}{_NUM}\s*g", rf"饱和脂肪\D{{0,10}}{_NUM}\s*克?g?"],
    "fibre_g": [rf"fib(?:re|er)\D{{0,20}}{_NUM}\s*g", rf"膳食纤维\D{{0,10}}{_NUM}\s*克?g?"],
    "salt_g": [rf"salt\D{{0,20}}{_NUM}\s*g", rf"(?:钠|盐)\D{{0,10}}{_NUM}\s*(?:克|g|mg)"],
    "energy_kj": [rf"{_NUM}\s*kJ", rf"能量\D{{0,10}}{_NUM}\s*(?:kJ|千焦)"],
}


def _find(text: str, keys: list[str]) -> float | None:
    for pat in keys:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                continue
    return None


def extract_from_hit(hit: SearchHit) -> Evidence:
    text = f"{hit.title}\n{hit.snippet}"
    fields = {k: _find(text, pats) for k, pats in PATTERNS.items()}
    filled = sum(1 for v in fields.values() if v is not None)
    return Evidence(
        source="web",
        url=hit.url,
        title=hit.title,
        snippet=hit.snippet[:500],
        confidence=min(0.95, 0.25 + 0.15 * filled),
        **fields,
    )


def extract(hits: list[SearchHit]) -> list[Evidence]:
    evs = [extract_from_hit(h) for h in hits]
    # 只留至少抽到一个营养字段的；全空的证据对裁决没有价值
    return [e for e in evs if any(
        getattr(e, f) is not None for f in PATTERNS
    )] or evs[:1]


def has_conflict(evidence: list[Evidence], field: str = "sugar_g", tol: float = 0.35) -> bool:
    """证据冲突检测：同一字段跨来源相对偏差超过 tol 视为冲突 → 转人工。"""
    vals = [getattr(e, field) for e in evidence if getattr(e, field) is not None]
    if len(vals) < 2:
        return False
    lo, hi = min(vals), max(vals)
    return hi > 0 and (hi - lo) / hi > tol


def summarize(evidence: list[Evidence]) -> str:
    """给 SSE / trace 用的一行人话摘要。"""
    parts: list[str] = []
    for f, label, unit in [
        ("sugar_g", "糖", "g"),
        ("fat_g", "脂肪", "g"),
        ("fibre_g", "纤维", "g"),
        ("salt_g", "盐", "g"),
    ]:
        vals = [getattr(e, f) for e in evidence if getattr(e, f) is not None]
        if vals:
            parts.append(f"{label} {vals[0]}{unit}/100g")
    return "、".join(parts) or "未抽取到结构化营养数据"
