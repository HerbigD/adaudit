"""基线 / 对照跑批。

用法（W7）：
    python -m eval.runner --limit 300 --arm full
arm:
  vlm_only  —— 只跑 classify_initial（基线，不搜索不裁决）
  full      —— 全链路（快路径 + 慢路径），人工复核样本按"未裁定"计入 human_review_rate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Literal

import db
from eval import dataset, metrics
from graph import builder
from services import vlm

Arm = Literal["vlm_only", "full"]


async def _run_vlm_only(sample: dataset.Sample) -> metrics.Prediction:
    t0 = time.perf_counter()
    try:
        c = await vlm.classify(sample.image_path)
        code, conf = c.specific_code, c.specific_confidence
    except Exception:  # noqa: BLE001
        code, conf = None, 0.0
    return metrics.Prediction(
        audit_id=sample.id,
        gold_specific=sample.gold_specific,
        initial_specific=code,
        final_specific=code,
        initial_confidence=conf,
        final_confidence=conf,
        route_1=None,
        route_2=None,
        used_evidence=False,
        cache_hit=False,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )


async def _run_full(sample: dataset.Sample) -> metrics.Prediction:
    app = await builder.get_app()
    audit_id = db.create_audit(sample.image_path)
    cfg = {"configurable": {"thread_id": audit_id}}
    t0 = time.perf_counter()
    await app.ainvoke(
        {"audit_id": audit_id, "ad_image": sample.image_path, "evidence": [], "trace": []},
        config=cfg,
    )
    snap = await app.aget_state(cfg)
    st = snap.values or {}
    initial, final = st.get("initial"), st.get("final")
    return metrics.Prediction(
        audit_id=audit_id,
        gold_specific=sample.gold_specific,
        initial_specific=initial.specific_code if initial else None,
        final_specific=final.specific_code if final else None,
        initial_confidence=initial.specific_confidence if initial else 0.0,
        final_confidence=final.specific_confidence if final else 0.0,
        route_1=st.get("route_1"),
        route_2=st.get("route_2"),
        used_evidence=bool(st.get("evidence")),
        cache_hit=bool(st.get("cache_hit")),
        latency_ms=int((time.perf_counter() - t0) * 1000),
        trace=[t.model_dump(mode="json") for t in st.get("trace", [])],
    )


async def run(arm: Arm = "full", limit: int | None = None, only_confusing: bool = False) -> dict:
    db.init_db()
    samples = dataset.load(limit=limit, only_confusing=only_confusing)
    if not samples:
        raise SystemExit("评测集为空：先用 eval.dataset.import_csv 导入金标")
    fn = _run_full if arm == "full" else _run_vlm_only
    preds = [await fn(s) for s in samples]     # TODO(W7): 换成受限并发 gather
    return {"arm": arm, **metrics.summarize(preds)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="full", choices=["vlm_only", "full"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-confusing", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    result = asyncio.run(run(args.arm, args.limit, args.only_confusing))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
