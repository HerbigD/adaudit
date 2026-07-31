#!/usr/bin/env python3
"""Day 6 真实跑批 —— 在**有外网的机器**上执行（云端沙箱与 device VM 都没网）。

用法（在 api/ 目录下，venv 已激活）：

    python scripts/day6_real_run.py --probe                     # 只探测：模型可用性 + JSON mode
    python scripts/day6_real_run.py --ad /path/to/ad.jpg        # 单张走完整链路（验收主项）
    python scripts/day6_real_run.py --ocr-smoke /path/to/dir    # 五语种 OCR 冒烟
    python scripts/day6_real_run.py --confusion /path/to/dir    # 混淆对改判方向冒烟
    python scripts/day6_real_run.py --fuse-test                 # 熔断实调验证（把预算压到 1）

产物统一写到 `data/day6/`：
    probe.json / real_run_<audit>.json / evidence_<audit>.json / trace_<audit>.json
    ocr_smoke.json / confusion.json / usage_summary.json

脚本**自己不改 .env**：provider/model/预算全部从 config 读，
需要切换就改 .env 或用环境变量前缀覆盖（如 `DAILY_TOKEN_BUDGET=1 python ...`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from config import settings  # noqa: E402
from graph import builder  # noqa: E402
from services import usage, vlm  # noqa: E402

OUT = Path(settings.db_path).parent / "day6"
OUT.mkdir(parents=True, exist_ok=True)


def dump(name: str, payload) -> Path:
    p = OUT / name
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  → {p}")
    return p


def banner() -> None:
    print("=" * 72)
    print(f"provider : vlm={settings.vlm_provider} llm={settings.llm_provider} "
          f"search={settings.search_provider}")
    print(f"model    : {settings.vlm_model or settings.qwen_model} "
          f"(thinking={settings.qwen_enable_thinking}, json_mode={settings.qwen_json_mode})")
    print(f"base_url : {settings.dashscope_base_url}")
    snap = usage.snapshot()
    print(f"budget   : {snap['total_tokens']}/{snap['budget']} tokens 已用，"
          f"估算 {snap['cost']:.4f} {snap['currency']}")
    print("=" * 72)


def guard_real() -> None:
    """真跑前的自检：还在 mock 上就别浪费时间了。"""
    bad = []
    if settings.vlm_provider == "mock":
        bad.append("VLM_PROVIDER=mock")
    if settings.llm_provider == "mock":
        bad.append("LLM_PROVIDER=mock")
    if settings.app_env == "mock":
        bad.append("APP_ENV=mock（搜索会走假数据）")
    if settings.search_provider == "mock":
        bad.append("SEARCH_PROVIDER=mock")
    if not settings.dashscope_api_key:
        bad.append("DASHSCOPE_API_KEY 未配置")
    if bad:
        sys.exit("[day6] 还没切到真实配置：" + "；".join(bad) +
                 "\n改 api/.env：APP_ENV=dev VLM_PROVIDER=qwen LLM_PROVIDER=qwen SEARCH_PROVIDER=dashscope")


# --------------------------------------------------------------------------- #
async def probe() -> dict:
    print("\n[1/1] 探测模型可用性…")
    result = await vlm.probe_model()
    result["json_mode_state"] = vlm.json_mode_state()
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
    dump("probe.json", result)
    if not result.get("reachable"):
        print("\n⚠️  模型不可达。常见原因：模型 ID 写错、key 无该模型权限、"
              "中国站/国际站 base_url 不匹配。")
    return result


async def run_one(image: str, tag: str = "real") -> dict:
    """单张走完整链路，导出 evidence / trace / 成本。"""
    db.init_db()
    app = await builder.get_app()
    audit_id = db.create_audit(image)
    cfg = {"configurable": {"thread_id": audit_id}}

    before = usage.snapshot()
    t0 = time.perf_counter()
    with usage.collect() as u:
        await app.ainvoke(
            {"audit_id": audit_id, "ad_image": image, "evidence": [], "trace": []}, config=cfg
        )
    elapsed = time.perf_counter() - t0
    st = (await app.aget_state(cfg)).values or {}
    after = usage.snapshot()

    initial, revised, final = st.get("initial"), st.get("revised"), st.get("final")
    evidence = [e.model_dump(mode="json") for e in st.get("evidence", [])]
    trace = [s.model_dump(mode="json") for s in st.get("trace", [])]

    summary = {
        "audit_id": audit_id,
        "image": image,
        "elapsed_s": round(elapsed, 2),
        "route_1": st.get("route_1"),
        "route_2": st.get("route_2"),
        "search_status": st.get("search_status"),
        "initial": initial.model_dump(mode="json") if initial else None,
        "revised": revised.model_dump(mode="json") if revised else None,
        "final": final.model_dump(mode="json") if final else None,
        "evidence_count": len(evidence),
        "real_source_urls": [e["source_url"] for e in evidence if e.get("provenance") == "web"],
        "cost": {
            "calls": u.calls,
            "tokens_in": u.tokens_in,
            "tokens_out": u.tokens_out,
            "tokens_total": u.tokens_in + u.tokens_out,
            "estimated": round(u.cost, 6),
            "currency": settings.cost_currency,
            "budget_before": before["total_tokens"],
            "budget_after": after["total_tokens"],
        },
        "adapters": sorted({s["adapter"] for s in trace if s.get("adapter")}),
    }
    # 账本前后差作为交叉校验：收集器要是漏了，这里能看出来
    summary["cost"]["ledger_delta"] = after["total_tokens"] - before["total_tokens"]

    print(f"\n  route: {summary['route_1']} → {summary['route_2']} "
          f"| search={summary['search_status']} | {elapsed:.1f}s")
    print(f"  final: {(final.label() if final else None)}")
    print(f"  evidence: {len(evidence)} 条，真实 URL {len(summary['real_source_urls'])} 个")
    print(f"  cost: {summary['cost']['tokens_total']} tokens ≈ "
          f"{summary['cost']['estimated']} {settings.cost_currency}（{u.calls} 次调用）"
          f"｜账本增量 {summary['cost']['ledger_delta']}")

    dump(f"real_run_{tag}_{audit_id[:8]}.json", summary)
    dump(f"evidence_{tag}_{audit_id[:8]}.json", evidence)
    dump(f"trace_{tag}_{audit_id[:8]}.json", trace)
    return summary


async def ocr_smoke(folder: str) -> dict:
    """五语种 OCR 冒烟：只跑 classify_initial，看品牌/品名抽没抽出来。"""
    rows = []
    for img in sorted(Path(folder).glob("*")):
        if img.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        try:
            with usage.collect() as u:
                c = await vlm.classify(str(img))
            rows.append({
                "file": img.name, "ok": True,
                "ad_language": c.ad_language, "country": c.country,
                "brand": c.brand, "product_name": c.product_name,
                "name_brand_identifiable": c.name_brand_identifiable,
                "leaf_vs_parent": c.leaf_vs_parent, "specific_code": c.specific_code,
                "specific_confidence": c.specific_confidence,
                "tokens": u.tokens_in + u.tokens_out, "cost": round(u.cost, 6),
            })
            print(f"  {img.name:<34} {c.ad_language} brand={c.brand!r} name={c.product_name!r}")
        except Exception as exc:  # noqa: BLE001
            rows.append({"file": img.name, "ok": False, "error": str(exc)[:300]})
            print(f"  {img.name:<34} FAILED {exc}")
    got = [r for r in rows if r.get("ok") and r.get("brand")]
    out = {
        "n": len(rows),
        "brand_extracted": len(got),
        "brand_extraction_rate": round(len(got) / len(rows), 3) if rows else 0.0,
        "by_language": {r.get("ad_language"): 0 for r in rows if r.get("ok")},
        "rows": rows,
    }
    for r in rows:
        if r.get("ok"):
            out["by_language"][r["ad_language"]] += 1
    dump("ocr_smoke.json", out)
    return out


async def confusion_smoke(folder: str) -> dict:
    """混淆对冒烟：看取证后改判方向对不对（初判 → 重裁决）。"""
    rows = []
    for img in sorted(Path(folder).glob("*")):
        if img.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        s = await run_one(str(img), tag="confusion")
        rows.append({
            "file": img.name,
            "initial_code": (s["initial"] or {}).get("specific_code"),
            "initial_candidates": (s["initial"] or {}).get("candidate_codes"),
            "revised_code": (s["revised"] or {}).get("specific_code"),
            "final_code": (s["final"] or {}).get("specific_code"),
            "search_status": s["search_status"],
            "route_2": s["route_2"],
            "evidence_urls": s["real_source_urls"],
            "tokens": s["cost"]["tokens_total"],
        })
    out = {"n": len(rows), "rows": rows}
    dump("confusion.json", out)
    return out


async def fuse_test() -> dict:
    """熔断实调验证：把预算压到 1 token，确认真实调用在**发出前**就被拒。

    用**独立账本**（临时文件），不污染当日真实计数 —— 否则为了验证熔断反而
    把真实预算记花了，本末倒置。
    """
    import tempfile

    print("\n把 daily_token_budget 临时压到 1（独立账本），再发一次真实调用…")
    orig_budget, orig_path = settings.daily_token_budget, settings.usage_path
    tmp = Path(tempfile.mkdtemp()) / "usage.json"
    settings.daily_token_budget, settings.usage_path = 1, str(tmp)
    try:
        usage.record("qwen3.7-plus", 1, 1)          # 先烧穿
        before = usage.snapshot()
        refused, err, wrong = False, None, None
        try:
            await vlm.complete("say ok", "say ok")
        except usage.BudgetExceeded as exc:
            refused, err = True, str(exc)
        except Exception as exc:  # noqa: BLE001
            wrong = f"{type(exc).__name__}: {exc}"
        out = {
            "budget_forced_to": 1,
            "ledger": before,
            "refused": refused,
            "refusal_message": err,
            "unexpected_error": wrong,
            "verdict": "PASS 熔断生效：真实调用在发出前被拒" if refused
                       else f"FAIL 未被熔断拦住（{wrong or '调用居然成功了'}）",
        }
    finally:
        settings.daily_token_budget, settings.usage_path = orig_budget, orig_path
    print(json.dumps(out, ensure_ascii=False, indent=2)[:900])
    dump("fuse_test.json", out)
    return out


# --------------------------------------------------------------------------- #
async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--ad", help="单张真实广告图路径")
    ap.add_argument("--ocr-smoke", help="五语种图片所在目录")
    ap.add_argument("--confusion", help="混淆对图片所在目录")
    ap.add_argument("--fuse-test", action="store_true")
    ap.add_argument("--skip-guard", action="store_true", help="跳过真实配置自检")
    ap.add_argument(
        "--force-search",
        action="store_true",
        help="把 DIRECT_THRESHOLD 临时抬到 1.01，逼任何图都走慢路径 —— "
             "用来在高置信样本上也能拿到取证链路的验收证据",
    )
    args = ap.parse_args()

    if args.force_search:
        settings.direct_threshold = 1.01
        print("⚠️  --force-search：DIRECT_THRESHOLD 临时抬到 1.01，本次所有图都会走取证路径\n"
              "   （只影响本次进程，不写回 .env）")

    if not args.skip_guard:
        guard_real()          # 熔断实调也要在真实配置下做，否则拦住的只是 mock
    banner()

    if args.probe:
        r = await probe()
        if not r.get("reachable"):
            return
    if args.ad:
        await run_one(args.ad)
    if args.ocr_smoke:
        print("\nOCR 冒烟：")
        await ocr_smoke(args.ocr_smoke)
    if args.confusion:
        print("\n混淆对冒烟：")
        await confusion_smoke(args.confusion)
    if args.fuse_test:
        await fuse_test()

    snap = usage.snapshot()
    dump("usage_summary.json", snap)
    print(f"\n当日累计：{snap['calls']} 次调用 / {snap['total_tokens']} tokens / "
          f"≈{snap['cost']:.4f} {snap['currency']}（预算 {snap['budget']}）")
    await builder.close_app()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    asyncio.run(main())
