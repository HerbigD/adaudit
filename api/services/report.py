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
    adapters: Counter[str] = Counter()
    done = searched = human = parent_level = 0

    for r in rows:
        final = r.get("final")
        if r.get("route_1"):
            routes[r["route_1"]] += 1
        if r["route_1"] == "search":
            searched += 1
        if r.get("human_choice"):
            human += 1
            choices[r["human_choice"]] += 1
        for step in r.get("trace") or []:
            if step.get("adapter"):
                adapters[step["adapter"]] += 1
        if not final:
            continue
        done += 1
        general_dist[final.get("general_category") or "(unknown)"] += 1
        code = final.get("specific_code")
        if code is None:
            parent_level += 1                      # 粒度自适应：只定到父类
        else:
            specific_dist[str(code)] += 1
        c = final.get("specific_confidence", 0.0)
        key = (
            "0.85-1.0" if c >= 0.85 else "0.7-0.85" if c >= 0.7 else "0.5-0.7" if c >= 0.5 else "0.0-0.5"
        )
        conf_hist[key] += 1

    # Day7 · OPEN-RISK-01 观察指标：缓存命中率 与 命中后被人工改判率
    hit_rows = db.cache_hit_rows([r["id"] for r in rows])
    hits = len(hit_rows)
    reviewed = [h for h in hit_rows if h["overturned"] is not None]
    overturned = [h for h in reviewed if h["overturned"] == 1]

    # HFSS 归属来自 taxonomy.HFSS_VERDICTS 判定表（逐条可审），不是名称正则猜的
    risky = taxonomy.hfss_codes()
    hfss = sum(v for k, v in specific_dist.items() if int(k) in risky)
    alcohol = sum(v for k, v in specific_dist.items() if int(k) in taxonomy.alcohol_codes())

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
        "alcohol_share": round(alcohol / done, 4) if done else 0.0,
        "parent_level_count": parent_level,
        "parent_level_share": round(parent_level / done, 4) if done else 0.0,
        "adapters": dict(adapters),
        "taxonomy_version": taxonomy.load().version,
        "cache": cache_store.stats(),

        # ---- Day7 新增两数（日报 §0 已申报） ----
        # 分母是**整批**，不是"走了搜索路径的"：命中率要回答"这批广告有多少免了搜索"。
        "cache_hit_rate": round(hits / n, 4) if n else 0.0,
        # 分母按决议是"缓存命中总数"。⚠️ 它会被"命中但没走到人工"的样本稀释 ——
        # 人工复核率越低，这个数越接近 0，与缓存质量无关。
        # 所以同时给出只在**经人工裁定过的命中**里算的那一版，两个一起看才有意义。
        "cache_overturn_rate": round(len(overturned) / hits, 4) if hits else 0.0,
        "cache_overturn_detail": {
            "hits": hits,
            "reviewed": len(reviewed),
            "overturned": len(overturned),
            "overturn_rate_among_reviewed": (
                round(len(overturned) / len(reviewed), 4) if reviewed else None
            ),
            "unreviewed": hits - len(reviewed),
            "note": (
                "overturned=NULL（未走到人工）不计入分子，但按决议计入 cache_overturn_rate 的分母。"
                "判断缓存质量请看 overturn_rate_among_reviewed；"
                "reviewed 太小时该值不可解读。"
            ),
            "by_match_mode": dict(Counter(h["match_mode"] or "unknown" for h in hit_rows)),
            "by_provenance": dict(Counter(h["provenance"] or "unknown" for h in hit_rows)),
        },
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
            f"[{c}] {(taxonomy.get(int(c)).name_zh if taxonomy.get(int(c)) else '?')}（{v} 条）"
            for c, v in top_specific
        )
        or "无"
    )
    return f"""# 批次品类结构分析报告

## 一、品类结构概览
本批次共 {s['total']} 条广告，已完成裁定 {s['completed']} 条。
大类分布 Top3：{top_txt}。
细类分布 Top3：{spec_txt}。

## 二、HFSS 风险
高糖/高脂/高盐相关细类占已完成样本的 {s['hfss_share']:.1%}；酒精类另占 {s['alcohol_share']:.1%}（不计入 HFSS）。
置信度分布：{s['confidence_histogram']}。
仅定到父类（细类待定）的样本：{s['parent_level_count']} 条（{s['parent_level_share']:.1%}）。

## 三、Agent 运行质量
- 搜索触发率：{s['search_trigger_rate']:.1%}
- 人工复核率：{s['human_review_rate']:.1%}
- 人工裁定中采纳 original {s['original_adopted_rate']:.1%}，采纳 prediction {s['prediction_adopted_rate']:.1%}
- 产品缓存库：{s['cache']['products']} 个档案（人工核验 {s['cache']['human_verified']} 个，被人工覆盖 {s['cache']['superseded']} 次），累计命中 {s['cache']['total_hits']} 次
- 结果产出方（adapter）：{s['adapters']}｜taxonomy {s['taxonomy_version']}

## 四、建议
优先复核落在 0.5–0.85 置信区间的样本；对高频出现的品牌补充缓存档案可进一步压低人工复核率。

> 本报告全部数字来源于批次 stats_json，未经二次计算。
"""
