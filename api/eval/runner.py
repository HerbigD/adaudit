"""基线 / 对照跑批。

用法（W7）：
    python -m eval.runner --arm full --limit 300

arm:
  vlm_only  —— 只跑 classify_initial（基线，不搜索不裁决）
  full      —— 全链路（快路径 + 慢路径），人工复核样本按"未裁定"计入 human_review_rate

## Day 3 加固 3：mock 断言位

跑批默认**拒绝** mock / rule-fallback 产出的结果 —— 一份 adapter=mock 的指标看起来
和真实指标长得一模一样，一旦流进简历或论文就是学术不端级别的事故。
两道闸：
  ① 开跑前查 config（app_env / vlm_provider / llm_provider）
  ② 跑完后查每条 Prediction 的 adapters（防止运行中被切到兜底）
`--allow-mock` 只用于自测链路，输出会被强制打上 `MOCK_RESULT_DO_NOT_REPORT` 标记。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Literal

import db
from config import settings
from eval import dataset, metrics
from graph import builder
from services import vlm

Arm = Literal["vlm_only", "full"]

MOCK_MARKER = "MOCK_RESULT_DO_NOT_REPORT"


class MockResultRefused(RuntimeError):
    """跑批产出里混入了 mock / 规则兜底结果。"""


def _preflight(allow_mock: bool) -> list[str]:
    """开跑前的配置检查。返回问题清单（allow_mock 时降级为警告）。"""
    problems: list[str] = []
    if settings.app_env == "mock":
        problems.append("APP_ENV=mock（搜索走假数据）")
    if settings.vlm_provider == "mock":
        problems.append("VLM_PROVIDER=mock")
    if settings.llm_provider == "mock":
        problems.append("LLM_PROVIDER=mock（重裁决会走 rule-fallback）")
    if problems and not allow_mock:
        raise MockResultRefused(
            "拒绝跑批：" + "；".join(problems) + "。\n"
            "指标只能来自真实 provider。自测链路请显式加 --allow-mock。"
        )
    return problems


def _postflight(preds: list[metrics.Prediction], allow_mock: bool) -> None:
    """跑完后的断言：任何一条结果带 mock/rule-fallback adapter 就拒绝输出指标。"""
    tainted = [p for p in preds if p.is_mock]
    if tainted and not allow_mock:
        adapters = sorted({a for p in tainted for a in p.adapters})
        raise MockResultRefused(
            f"拒绝出指标：{len(tainted)}/{len(preds)} 条结果由 {adapters} 产出。"
        )


async def _run_vlm_only(sample: dataset.Sample) -> metrics.Prediction:
    t0 = time.perf_counter()
    adapters: tuple[str, ...] = ()
    level, lang, country = "leaf", "en", None
    try:
        c = await vlm.classify(sample.image_path)
        code, conf = c.specific_code, c.specific_confidence
        adapters, level = (c.adapter or "unknown",), c.leaf_vs_parent
        lang, country = c.ad_language, c.country
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
        adapters=adapters,
        leaf_vs_parent=level,
        language=lang,
        country=country,
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
    trace = st.get("trace", [])
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
        adapters=tuple(sorted({s.adapter for s in trace if s.adapter})),
        leaf_vs_parent=final.leaf_vs_parent if final else "leaf",
        language=(initial.ad_language if initial else "en"),
        country=(initial.country if initial else None),
        search_status=st.get("search_status"),
        trace=[s.model_dump(mode="json") for s in trace],
    )


async def run(
    arm: Arm = "full",
    limit: int | None = None,
    only_confusing: bool = False,
    allow_mock: bool = False,
) -> dict:
    warnings = _preflight(allow_mock)
    db.init_db()
    samples = dataset.load(limit=limit, only_confusing=only_confusing)
    if not samples:
        raise SystemExit("评测集为空：先用 eval.dataset.import_csv 导入金标")

    fn = _run_full if arm == "full" else _run_vlm_only
    preds = [await fn(s) for s in samples]     # TODO(W7): 换成受限并发 gather
    _postflight(preds, allow_mock)

    result = {"arm": arm, **metrics.summarize(preds)}
    if warnings or result.get("contains_mock"):
        result["marker"] = MOCK_MARKER
        result["warnings"] = warnings
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="full", choices=["vlm_only", "full"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-confusing", action="store_true")
    ap.add_argument(
        "--allow-mock",
        action="store_true",
        help="仅用于自测链路：允许 mock/rule-fallback 结果，输出会打 MOCK_RESULT_DO_NOT_REPORT",
    )
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        result = asyncio.run(run(args.arm, args.limit, args.only_confusing, args.allow_mock))
    except MockResultRefused as exc:
        raise SystemExit(f"[eval.runner] {exc}") from exc

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
