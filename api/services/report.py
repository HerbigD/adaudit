"""批次聚合统计 + LLM 品类结构分析报告（闭环最后一环）。

红线：**报告里所有数字必须来自 stats_json**（grounded），
LLM 只负责把统计表翻译成文字洞察，不允许自己算数。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import db
from services import cache_store, taxonomy, vlm


def aggregate(batch_id: str) -> dict[str, Any]:
    rows = db.list_audits(batch_id=batch_id, limit=100000)
    n = len(rows)
    general_dist: Counter[str] = Counter()
    specific_dist: Counter[str] = Counter()
    conf_hist = {"0.0-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
    routes: Counter[str] = Counter()
    choices: Counter[str] = Counter()
    done = searched = human = 0

    for r in rows:
        final = r.get("final")
        if r.get("route_1"):
            routes[r["route_1"]] += 1
        if r["route_1"] == "search":
            searched += 1
        if r.get("human_choice"):
            human += 1
            choices[r["human_choice"]] += 1
        if not final:
            continue
        done += 1
        general_dist[final["general_category"]] += 1
        specific_dist[str(final["specific_code"])] += 1
        c = final.get("specific_confidence", 0.0)
        key = (
            "0.85-1.0" if c >= 0.85 else "0.7-0.85" if c >= 0.7 else "0.5-0.7" if c >= 0.5 else "0.0-0.5"
        )
        conf_hist[key] += 1

    hfss_codes = {12, 13, 14, 16, 17, 19, 20, 21, 23, 24, 25, 32}
    hfss = sum(v for k, v in specific_dist.items() if int(k) in hfss_codes)

    return {
        "total": n,
        "completed": done,
        "general_distribution": dict(general_dist),
        "specific_distribution": dict(specific_dist),
        "confidence_histogram": conf_hist,
        "route_distribution": dict(routes),
        "search_trigger_rate": round(searched / n, 4) if n else 0.0,
        "human_review_rate": round(human / n, 4) if n else 0.0,
        "human_choice_distribution": dict(choices),
        "original_adopted_rate": round(choices["original"] / human, 4) if human else 0.0,
        "prediction_adopted_rate": round(choices["prediction"] / human, 4) if human else 0.0,
        "hfss_share": round(hfss / done, 4) if done else 0.0,
        "cache": cache_store.stats(),
    }


REPORT_SYSTEM = """You write short category-structure analysis reports for public-health
advertising surveillance. You are given a JSON statistics table.

HARD RULE: every number in your report must appear verbatim in the statistics table.
Never compute, estimate, or invent a figure. If something is not in the table, do not
mention it. Write in Chinese, 300–500 字, with sections:
一、品类结构概览 / 二、高糖高脂高盐（HFSS）风险 / 三、Agent 运行质量（搜索触发率、人工复核率、缓存）/ 四、建议。"""


async def generate_report(batch_id: str) -> str:
    stats = aggregate(batch_id)
    db.update_batch(batch_id, stats_json=json.dumps(stats, ensure_ascii=False))
    try:
        md = await vlm.complete(REPORT_SYSTEM, json.dumps(stats, ensure_ascii=False, indent=2))
    except Exception:  # noqa: BLE001 — mock / LLM 不可用 → 模板化报告，数字同样 grounded
        md = _template_report(stats)
    db.update_batch(batch_id, report_md=md, status="report_ready")
    return md


def _template_report(s: dict[str, Any]) -> str:
    top = sorted(s["general_distribution"].items(), key=lambda kv: -kv[1])[:3]
    top_txt = "、".join(f"{k}（{v} 条）" for k, v in top) or "无"
    top_specific = sorted(s["specific_distribution"].items(), key=lambda kv: -kv[1])[:3]
    spec_txt = (
        "、".join(
            f"[{c}] {taxonomy.BY_CODE[int(c)].name[:24]}（{v} 条）" for c, v in top_specific
        )
        or "无"
    )
    return f"""# 批次品类结构分析报告

## 一、品类结构概览
本批次共 {s['total']} 条广告，已完成裁定 {s['completed']} 条。
大类分布 Top3：{top_txt}。
细类分布 Top3：{spec_txt}。

## 二、HFSS 风险
高糖/高脂/高盐相关细类占已完成样本的 {s['hfss_share']:.1%}。
置信度分布：{s['confidence_histogram']}。

## 三、Agent 运行质量
- 搜索触发率：{s['search_trigger_rate']:.1%}
- 人工复核率：{s['human_review_rate']:.1%}
- 人工裁定中采纳 original {s['original_adopted_rate']:.1%}，采纳 prediction {s['prediction_adopted_rate']:.1%}
- 产品缓存库：{s['cache']['products']} 个档案，累计命中 {s['cache']['total_hits']} 次

## 四、建议
优先复核落在 0.5–0.85 置信区间的样本；对高频出现的品牌补充缓存档案可进一步压低人工复核率。

> 本报告全部数字来源于批次 stats_json，未经二次计算。
"""
