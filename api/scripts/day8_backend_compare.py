"""Day8 · 两种向量后端下的缓存命中率对比。

## 为什么必须对比

缓存命中得分 = `0.55 品牌 + 0.20 名称重叠 + 0.25 语义`，阈值 `0.82`。
**语义分是每一次命中的必要条件**（0.55+0.20=0.75 够不着 0.82）。
所以换后端不是"检索好一点差一点"，是**命中率会整体位移** ——
两种后端下的命中率数字放在一起比没有意义，除非注明 backend。

从今天起，任何一份带缓存指标的结果都必须带 `cache_backend`。

## 用法

在仓库的 `api/` 目录下跑（路径都是相对 `api/` 的，产物落在仓库根 `data/day8/`）：

```bash
# difflib 一侧（不需要网络）
CHROMA_DISABLE=1 python3 scripts/day8_backend_compare.py --out ../data/day8/backend_difflib.json

# chroma 一侧（首次会联网下载 ~80MB embedding 模型）
python3 scripts/day8_backend_compare.py --out ../data/day8/backend_chroma.json

# 出对比表
python3 scripts/day8_backend_compare.py --report ../data/day8/backend_difflib.json ../data/day8/backend_chroma.json
```

## 它不碰你的真实库

`_seed()` 会 `DELETE FROM product_cache` 并写 12 条**编造**的档案
（`fat=3.0`、`specific_code=5`、`provenance=auto`）。在真实库上跑一次，
这些假档案就永久留在 `data/adaudit.db` 和 `data/chroma/` 里，
之后真实审计查 `Amul Toned Milk` 之类会命中它们 ——
单向棘轮只挡「auto 盖 human_verified」，挡不住 auto 档案被当成真档案命中。

所以 `main()` 一进来就 `_isolate()`，把 `db_path` / `chroma_path` 切到一次性
scratch 目录；`_seed()` 里另有一条 assert 兜底。隔离不是"更干净一点"，
是这个脚本能不能跑的前提。

跑的是 **mock 链路**（`APP_ENV=mock`）——本脚本不产生任何真实 API 调用，
它测的是"同一批查询在两种向量后端下命中率差多少"，与模型无关。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                                                     # noqa: E402
from config import settings                                   # noqa: E402
from graph.state import Classification, Evidence, NutrientValue  # noqa: E402
from services import cache_store, vectorstore                 # noqa: E402

# 12 条冒烟档案 + 12 条查询。查询刻意分三类，好看清后端差在哪：
#   exact    完全同名 —— 只靠品牌+名称重叠是 0.75，够不着 0.82，**必须靠语义分**
#   variant  多一个规格词 —— 语义上仍是同一产品
#   decoy    跨类别边界的近名（Toned vs Double Toned）—— **不该命中**
FIXTURES = [
    ("Amul", "Amul Toned Milk 1L"),
    ("Nestle", "Nestle Everyday Dairy Whitener 400g"),
    ("Kelloggs", "Kelloggs Corn Flakes 475g"),
    ("Britannia", "Britannia Marie Gold Biscuits 250g"),
    ("Maggi", "Maggi Masala Instant Noodles 70g"),
    ("Parle", "Parle G Glucose Biscuits 800g"),
    ("Coca Cola", "Coca Cola Original 750ml"),
    ("Lays", "Lays Classic Salted Chips 52g"),
    ("Dabur", "Dabur Real Mixed Fruit Juice 1L"),
    ("Amul", "Amul Butter 500g"),
    ("MTR", "MTR Rava Idli Mix 500g"),
    ("Haldiram", "Haldiram Aloo Bhujia 200g"),
]

QUERIES = (
    [("exact", b, n, True) for b, n in FIXTURES[:6]]
    + [("variant", b, f"{n} Pack", True) for b, n in FIXTURES[6:10]]
    + [
        ("decoy", "Amul", "Amul Double Toned Milk 1L", False),
        ("decoy", "Maggi", "Maggi Atta Instant Noodles 70g", False),
    ]
)


def _isolate() -> Path:
    """把存储切到一次性 scratch 目录。**必须在任何 db / 向量库访问之前调用。**

    见模块 docstring「它不碰你的真实库」。这里同时切 `db_path` 与 `chroma_path`：
    只切前者的话，12 条假档案照样会写进真实向量库，而向量库那一侧没有 SQL 可以回滚。
    """
    scratch = Path(tempfile.mkdtemp(prefix="adaudit-backend-cmp-"))
    settings.db_path = str(scratch / "cmp.db")
    settings.chroma_path = str(scratch / "chroma")
    Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
    # 单例可能已经按旧路径建好了（import config 时就 mkdir 过），必须丢掉重建
    vectorstore.reset()
    print(f"scratch: {scratch}")
    return scratch


def _seed() -> None:
    # 兜底：真删之前再确认一次打的不是真实库。断言比注释可靠。
    assert "adaudit-backend-cmp-" in settings.db_path, (
        f"拒绝在非 scratch 库上 seed：{settings.db_path}。先调 _isolate()。"
    )
    with db.cursor() as cur:
        cur.execute("DELETE FROM product_cache")
    for brand, name in FIXTURES:
        ev = [
            Evidence(
                id="ev_seed", source_url=f"https://example.com/{brand}",
                nutrients=[NutrientValue(nutrient="fat", value=3.0,
                                         unit="g/100g", normalized=3.0)],
            )
        ]
        verdict = Classification(
            general_id=3, specific_code=5, brand=brand, product_name=name,
            specific_confidence=0.9, general_confidence=0.95,
        )
        cache_store.upsert(brand, name, ev, verdict)


def run(out: str) -> dict:
    db.init_db()
    _seed()
    backend = vectorstore.backend()

    rows = []
    for kind, brand, name, should_hit in QUERIES:
        rec, score = cache_store.lookup(brand, name)
        hit = bool(rec) and score >= settings.cache_hit_threshold \
            and not rec.get("strict_reject_reason")
        rows.append({
            "kind": kind, "brand": brand, "query": name,
            "score": round(score, 4), "hit": hit,
            "should_hit": should_hit, "correct": hit == should_hit,
        })

    hits = sum(r["hit"] for r in rows)
    wanted = [r for r in rows if r["should_hit"]]
    decoys = [r for r in rows if not r["should_hit"]]
    result = {
        "cache_backend": backend,
        "degrade_reason": vectorstore.degrade_reason(),
        # 口径跟着数字走（纪律 #9）：这份命中率测的是"12 条档案的一次性空库"，
        # 不是生产库的命中率；本脚本不调模型，所以没有 adapters 可报。
        "isolated_db": True,
        "adapters": "n/a — 本脚本不调模型",
        "n_archives": len(FIXTURES),
        "n_queries": len(rows),
        "hit_rate": round(hits / len(rows), 4),
        "recall_on_same_product": round(
            sum(r["hit"] for r in wanted) / len(wanted), 4),
        "false_hit_rate_on_decoys": round(
            sum(r["hit"] for r in decoys) / len(decoys), 4),
        "mean_score": round(sum(r["score"] for r in rows) / len(rows), 4),
        "rows": rows,
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"backend={backend}  命中 {hits}/{len(rows)}  "
          f"同产品召回 {result['recall_on_same_product']}  "
          f"近名误命中 {result['false_hit_rate_on_decoys']}")
    print(f"  → {out}")
    return result


def report(paths: list[str]) -> None:
    data = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    cols = ["cache_backend", "hit_rate", "recall_on_same_product",
            "false_hit_rate_on_decoys", "mean_score"]
    print("\n| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for d in data:
        print("| " + " | ".join(str(d[c]) for c in cols) + " |")

    print("\n逐条对比（score）：\n")
    print("| kind | query | " + " | ".join(d["cache_backend"] for d in data) + " | 判定 |")
    print("|" + "---|" * (3 + len(data)))
    for i, r in enumerate(data[0]["rows"]):
        scores = " | ".join(
            f"{d['rows'][i]['score']}{'✅' if d['rows'][i]['hit'] else '❌'}" for d in data
        )
        print(f"| {r['kind']} | {r['query'][:34]} | {scores} | "
              f"{'应命中' if r['should_hit'] else '不应命中'} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/day8/backend_run.json")
    ap.add_argument("--report", nargs="+", help="给两个结果文件，出对比表")
    args = ap.parse_args()

    if args.report:
        report(args.report)
        return

    _isolate()          # 必须在 force_fallback / run 之前：两者都会碰存储
    if os.environ.get("CHROMA_DISABLE"):
        # 强制走 fallback：对比实验需要能主动选后端，不能只看环境碰巧是什么
        vectorstore.force_fallback("CHROMA_DISABLE=1（对比实验显式指定）")
    run(args.out)


if __name__ == "__main__":
    main()
