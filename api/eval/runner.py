"""基线 / 对照跑批。

用法：
    python -m eval.runner --arm full --split dev       # 调参、看误差，随便跑
    python -m eval.runner --arm full --split eval      # 出终版指标，**只跑一次**
    python -m eval.runner --ablation --split dev       # A3 四臂消融

arm:
  vlm_only  —— 只跑 classify_initial（基线，不搜索不裁决）
  full      —— 全链路（快路径 + 慢路径），人工复核样本按"未裁定"计入 human_review_rate

## A2 切分纪律

`--split` 缺省时读 `eval_samples` 全表，并打一条醒目警告：
那条路径**没有切分概念**，用它出的数字等于承认在测试集上调过参。
留着它只因为 W1–W6 的自测脚本还在用。

## A3 消融臂（`--pairs-arm`）

prompt 里放哪些混淆对（裁决①的三档制）：
  A  无 / B 仅 Tier1 definitional / B2 加 Tier2 compositional（线上默认）/ C 再加 Tier3 dev 经验对
缺省取 `config.PAIRS_ARM`（默认 B2）。arm 会写进每条 Prediction 和最终 summary，
所以任何一份指标都答得出"这个数字是在哪个 prompt 条件下得到的"。

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
import collections
import json
import time
from typing import Literal

import db
from config import settings
from eval import ablation, dataset, metrics
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
        gold_language=sample.language,
        gold_country=sample.country,
        split=sample.split,
        pairs_arm=settings.pairs_arm,
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
        gold_language=sample.language,
        gold_country=sample.country,
        split=sample.split,
        pairs_arm=settings.pairs_arm,
        search_status=st.get("search_status"),
        trace=[s.model_dump(mode="json") for s in trace],
    )


def _load_samples(
    split: str | None, limit: int | None, only_confusing: bool
) -> tuple[list[dataset.Sample], list[str]]:
    if split:
        return dataset.from_split(split, limit=limit, only_confusing=only_confusing), []
    warn = (
        "未指定 --split：本次读的是 eval_samples 全表，**没有 dev/held-out 隔离**。"
        "这样得到的数字不能作为终版指标（A2 决议）。"
        "正确用法：python -m eval.split --pool <金标池.csv> 之后 --split dev/eval。"
    )
    print(f"[eval.runner] ⚠ {warn}")
    return dataset.load(limit=limit, only_confusing=only_confusing), [warn]


async def _run_samples(
    samples: list[dataset.Sample], arm: Arm
) -> list[metrics.Prediction]:
    fn = _run_full if arm == "full" else _run_vlm_only
    return [await fn(s) for s in samples]      # TODO(W7): 换成受限并发 gather


async def run(
    arm: Arm = "full",
    limit: int | None = None,
    only_confusing: bool = False,
    allow_mock: bool = False,
    split: str | None = None,
    pairs_arm: str | None = None,
) -> dict:
    warnings = _preflight(allow_mock)
    if pairs_arm:
        settings.pairs_arm = pairs_arm         # prompt 构造读的是这个值
    db.init_db()
    samples, split_warnings = _load_samples(split, limit, only_confusing)
    warnings += split_warnings
    if not samples:
        raise SystemExit("评测集为空：先跑 eval.split，或用 eval.dataset.import_csv 导入金标")

    preds = await _run_samples(samples, arm)
    _postflight(preds, allow_mock)

    result = {"arm": arm, **metrics.summarize(preds)}
    if warnings or result.get("contains_mock"):
        result["marker"] = MOCK_MARKER
        result["warnings"] = warnings
    return result


async def run_ablation(
    split: str = "pool_minus_heldout",
    arm: Arm = "full",
    allow_mock: bool = False,
    n_confusing: int = ablation.CONFUSION_N,
    tier_quota: dict[str, int] | None = None,
    pair_cap: int | None = None,
    dry_run: bool = False,
    manifest_path: str | None = None,
) -> dict:
    """A3 四臂消融：同一批样本，只改 prompt 里放哪些混淆对。

    混淆子集与对照组**跑四遍**（A/B/B2/C），样本完全相同 ——
    唯一的自变量就是 prompt 里的那一行混淆对清单。
    """
    # dry-run 先于 preflight：出抽样清单不需要真实 provider，
    # 而"跑批前先给人看一眼"正是在 provider 还没接好的时候要做的事
    confusing, control = ablation.build_subset(split, n_confusing, tier_quota, pair_cap)
    if manifest_path or dry_run:
        ablation.write_manifest(
            manifest_path or f"{settings.split_dir}/ablation_manifest.json",
            confusing, control, source=split,
        )
    if dry_run:
        return {
            "dry_run": True,
            **ablation.manifest(confusing, control, source=split),
            "next": "清单确认无误后去掉 --dry-run 再跑（真实调用会产生费用）",
        }

    warnings = _preflight(allow_mock)
    db.init_db()
    composition = collections.Counter(ablation.tier_of(s) or "unknown" for s in confusing)
    print(f"[ablation] 混淆子集 {len(confusing)} 条 / 对照组 {len(control)} 条 "
          f"× {len(ablation.ARMS)} 臂")

    original = settings.pairs_arm
    results: dict[str, dict[str, list[metrics.Prediction]]] = {}
    try:
        for a in ablation.ARMS:
            settings.pairs_arm = a
            results[a] = {
                "confusing": await _run_samples(confusing, arm),
                "control": await _run_samples(control, arm),
            }
            for group in results[a].values():
                _postflight(group, allow_mock)
    finally:
        settings.pairs_arm = original

    out = ablation.summarize(results, split=split, composition=dict(composition))
    out["run_arm"] = arm
    if warnings or any(p.is_mock for g in results.values() for v in g.values() for p in v):
        out["marker"] = MOCK_MARKER
        out["warnings"] = warnings
    if split != "eval":
        out.setdefault("warnings", []).append(
            f"本次消融跑在 {split} 上。arm C 的结果**不可作为最终结论**："
            "它含的经验对正是从 dev 误差分析来的，在 dev 上评它是自评。"
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="full", choices=["vlm_only", "full"])
    ap.add_argument(
        "--split",
        default=None,
        choices=["dev", "eval", "smoke"],
        help="从 A2 切分 csv 取样本。不给则读 eval_samples 全表（会告警，不可出终版指标）",
    )
    ap.add_argument(
        "--pairs-arm",
        default=None,
        choices=list(ablation.ARMS),
        help="prompt 里放哪些混淆对：A 无 / B 仅 definitional / C 含 dev 经验对",
    )
    ap.add_argument("--ablation", action="store_true", help="跑 A3 三臂消融")
    ap.add_argument("--ablation-n", type=int, default=ablation.CONFUSION_N)
    ap.add_argument(
        "--tier-quota", default=None,
        help='按档配额，如 "definitional=60,definitional_compositional=30"。'
             "不给则按裁决原文先取满 Tier 1（此时 B2−B 无支撑）",
    )
    ap.add_argument(
        "--pair-cap", type=int, default=None,
        help=f"单一混淆对在本档配额里的条数上限。默认按 {ablation.PAIR_CAP_RATIO:.0%} 算",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="只出抽样清单不跑批 —— 真实调用前请先用它给人过目（A9 纪律）",
    )
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
        if args.ablation:
            result = asyncio.run(
                run_ablation(
                    args.split or "pool_minus_heldout", args.arm, args.allow_mock,
                    args.ablation_n, ablation.parse_tier_quota(args.tier_quota),
                    args.pair_cap, args.dry_run,
                )
            )
        else:
            result = asyncio.run(
                run(
                    args.arm, args.limit, args.only_confusing, args.allow_mock,
                    args.split, args.pairs_arm,
                )
            )
    except MockResultRefused as exc:
        raise SystemExit(f"[eval.runner] {exc}") from exc

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
