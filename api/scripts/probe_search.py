"""找出：哪个**文本模型**在 DashScope 原生协议下能真的带回 search_info。

## 走到这一步的经过

1. 兼容协议 `/compatible-mode/v1/chat/completions` 5 种参数 × 2 模型 → 永远 0 条。
   官方能力对比表：兼容协议**不支持返回搜索来源**，只有原生协议支持。
2. 改走原生 `/api/v1/services/aigc/text-generation/generation` → 400
   `url error, please check url`。含义是**模型与端点不匹配**：
   `qwen3.7-plus` 是多模态模型（我们也拿它认图），不在 text-generation 这条路上。

而搜索是纯文本活，不需要视觉。`config.llm_model` 本来就是为"分别覆盖能力"留的：
`vlm_model` 管看图，`llm_model` 管搜索与重裁决。所以正解是给 `llm_model`
配一个支持联网的文本模型，看图仍然用 qwen3.7-plus。

这个脚本就是来定"配哪个"的 —— 逐个试，谁带回 search_info 就用谁。

## 用法

    cd api
    python3 scripts/probe_search.py
    python3 scripts/probe_search.py --models qwen3.5-plus,qwen3-max

每个模型一次调用。走 usage.guard / record，账本照记。
产物：`data/day6/search_probe.json`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings                                    # noqa: E402
from services import search as S, usage, vlm                   # noqa: E402

OUT = Path(settings.db_path).parent / "day6"
Q = "Kotmale Drinking Yoghurt nutrition facts"

# 文档里列过支持联网的文本模型，从新到旧试。都不行再谈换端点/外部搜索。
CANDIDATES = ["qwen3.5-plus", "qwen3-max", "qwen-plus", "qwen3.5-flash", "qwen-max"]

OPTS = {"enable_source": True, "forced_search": True, "search_strategy": "turbo"}


def find_keys(obj, needle: str, path: str = "$") -> list[str]:
    """递归报出所有含 `needle` 的键在哪儿 —— 不猜字段名，直接看响应长什么样。"""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}"
            if needle in k.lower():
                shape = (f"list[{len(v)}]" if isinstance(v, list)
                         else f"dict({list(v)[:6]})" if isinstance(v, dict) else type(v).__name__)
                hits.append(f"{here} → {shape}")
            hits += find_keys(v, needle, here)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            hits += find_keys(v, needle, f"{path}[{i}]")
    return hits


async def try_model(model: str, query: str) -> dict:
    msgs = [
        {"role": "system", "content": S.SEARCH_SYSTEM_PROMPT},
        {"role": "user", "content": f"Search query: {query}"},
    ]
    try:
        _, raw = await vlm.dashscope_generate(
            msgs, model=model, enable_search=True, search_options=OPTS, timeout=120
        )
    except Exception as exc:                                   # noqa: BLE001
        msg = str(exc)
        short = ("模型/端点不匹配（url error）" if "url error" in msg
                 else "模型不存在或无权限" if "Model not exist" in msg or "InvalidApiKey" in msg
                 else msg[:150])
        print(f"    ❌ {short}")
        return {"model": model, "error": msg[:400], "short": short}

    keys = find_keys(raw, "search")
    urls = [h.url for h in S._hits_from_search_info(raw)]
    print(f"    search 键 : {keys[:2] if keys else '（无）'}")
    print(f"    抽到 URL  : {len(urls)} 条 {urls[:2]}")
    return {"model": model, "search_keys": keys, "urls": urls, "raw": raw}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=Q)
    ap.add_argument("--models", default=",".join(CANDIDATES))
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    print("=" * 74)
    print(f"原生端点 : {vlm._native_generation_url()}")
    print(f"看图模型 : {settings.vlm_model or settings.qwen_model}（不动它）")
    print(f"待试文本模型 : {models}")
    print("=" * 74)

    results = []
    for m in models:
        print(f"\n--- {m}")
        r = await try_model(m, args.query)
        results.append(r)
        if r.get("urls"):
            print("    ✅ 拿到来源了，后面的不用试了")
            break

    winner = next((r for r in results if r.get("urls")), None)

    # 找到可用模型就当场把端到端也跑一遍 —— 端点通不等于链路通
    e2e = None
    if winner:
        print(f"\n--- 端到端 search_product（临时用 {winner['model']}）")
        old = settings.llm_model
        settings.llm_model = winner["model"]
        try:
            o = await S.search_product("Kotmale", "Drinking Yoghurt",
                                       ad_language="en", country="LK")
            e2e = {"status": o.status, "detail": o.detail, "queries_used": o.queries_used,
                   "hits": [{"url": h.url, "title": h.title,
                             "snippet": (h.snippet or "")[:150]} for h in (o.hits or [])]}
            print(f"    status={o.status}  查询 {o.queries_used} 次  命中 {len(o.hits or [])} 条")
            for h in (o.hits or [])[:3]:
                print(f"      {h.url}")
        except Exception as exc:                               # noqa: BLE001
            e2e = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"    ❌ {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            settings.llm_model = old                            # 探针不留副作用

    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "search_probe.json"
    p.write_text(json.dumps({"endpoint": vlm._native_generation_url(), "query": args.query,
                             "results": results, "end_to_end": e2e},
                            ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 74)
    if winner and (e2e or {}).get("hits"):
        print(f"✅ 找到了：{winner['model']}，端到端也通了。")
        print(f"\n   在 .env 里加这一行，然后重启后端：\n\n       LLM_MODEL={winner['model']}\n")
        print("   看图仍然用 qwen3.7-plus（VLM_MODEL 不用动）。")
    elif winner:
        print(f"⚠️  {winner['model']} 能拿到来源，但端到端没命中 ——")
        print("    问题在查询构造或候选筛选，不在协议。把 search_probe.json 给 Claude。")
    else:
        print("❌ 所有 文本模型都拿不到来源。抌 search_probe.json 给 Claude，一起定：")
        print("   换成中国内地竘，还是接外部搜索 API（Serper / Brave / Tavily）。")
    snap = usage.snapshot()
    print(f"\n→ {p}")
    print(f"当日账本：{snap['calls']} 次 / {snap['total_tokens']} tokens / "
          f"≈{snap['cost']:.4f} {snap['currency']}")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
