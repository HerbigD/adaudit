"""从切分 CSV 导入 `eval_samples`，并**逐行对账**。

## 为什么要对账而不是只看条数

条数对上了不代表内容对上了。金标池里有两列容易搞混：

- `gold_code_raw` —— 标注员原始填的码
- `gold_specific` —— **已应用 22→32 合并**（A7 裁决）

用错列会让 80 张 gold=22 的图全判错，而**行数完全一致**，看不出来。
所以这里导完再回读一遍，与 CSV 逐行比对 `image_path` 与 `gold_specific`，
不一致就把差异打出来并以非零码退出。

## 用法

```bash
python3 scripts/import_eval_samples.py --split eval          # 导入 300 张
python3 scripts/import_eval_samples.py --split eval --check  # 只对账，不写库
```
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                                   # noqa: E402
from config import settings                 # noqa: E402
from services import taxonomy               # noqa: E402

GOLD_COLUMN = "gold_specific"               # **不是** gold_code_raw，见模块 docstring


def read_csv(split: str) -> list[dict[str, str]]:
    path = Path(settings.split_dir) / f"{split}.csv"
    if not path.exists():
        raise SystemExit(f"找不到 {path}。先跑 python3 -m eval.split --pool <金标池.csv>")
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if rows and GOLD_COLUMN not in rows[0]:
        raise SystemExit(
            f"{path} 缺列 {GOLD_COLUMN!r}（实际列：{list(rows[0])}）。"
            f"金标必须用已合并 22→32 的那一列，用 gold_code_raw 会让 80 张 gold=22 全判错。"
        )
    return rows


def import_split(split: str) -> int:
    """导入。**先清掉上一次同 split 的导入**，否则重跑会翻倍。

    `source` 用 `split:<name>` 而不是笼统的 `manual_label` ——
    这样人工回流（`human_feedback`）与不同切分的导入在库里分得开，
    清理时也不会误删回流数据。
    """
    rows = read_csv(split)
    src = f"split:{split}"
    with db.cursor() as cur:
        cur.execute("DELETE FROM eval_samples WHERE source=?", (src,))

    n = 0
    for r in rows:
        code = taxonomy.normalize(r[GOLD_COLUMN])
        if code is None:
            raise SystemExit(f"非法编号 {r[GOLD_COLUMN]!r}（id={r.get('id')}）——"
                             f"切分 CSV 里不该出现 33 类之外的码")
        db.add_eval_sample(
            image_path=r["image_path"],
            gold_general=taxonomy.general_of(code),
            gold_specific=str(code),
            source=src,
            is_confusing_pair=any(code in p for p in taxonomy.confusing_pairs()),
        )
        n += 1
    return n


def reconcile(split: str) -> dict:
    """回读库里的行，与 CSV 逐行比对。返回对账结果。"""
    rows = read_csv(split)
    src = f"split:{split}"
    with db.cursor() as cur:
        cur.execute("SELECT * FROM eval_samples WHERE source=?", (src,))
        in_db = {r["image_path"]: dict(r) for r in cur.fetchall()}

    missing, mismatched = [], []
    for r in rows:
        got = in_db.get(r["image_path"])
        if got is None:
            missing.append(r["image_path"])
            continue
        want = str(taxonomy.normalize(r[GOLD_COLUMN]))
        if got["gold_specific"] != want:
            mismatched.append({"image_path": r["image_path"],
                               "csv": want, "db": got["gold_specific"]})

    extra = sorted(set(in_db) - {r["image_path"] for r in rows})
    ok = not missing and not mismatched and not extra and len(in_db) == len(rows)
    return {
        "split": split,
        "csv_rows": len(rows),
        "db_rows": len(in_db),
        "missing_in_db": missing,
        "mismatched_gold": mismatched,
        "extra_in_db": extra,
        "gold_distribution": dict(sorted(Counter(
            str(taxonomy.normalize(r[GOLD_COLUMN])) for r in rows
        ).items(), key=lambda kv: -kv[1])),
        "confusing_pair_rows": sum(
            1 for r in rows
            if any(taxonomy.normalize(r[GOLD_COLUMN]) in p for p in taxonomy.confusing_pairs())
        ),
        "ok": ok,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="eval", choices=["dev", "eval", "smoke"])
    ap.add_argument("--check", action="store_true", help="只对账，不写库")
    args = ap.parse_args()

    db.init_db()
    if not args.check:
        n = import_split(args.split)
        print(f"导入 {n} 行（source=split:{args.split}）")

    rep = reconcile(args.split)
    print(f"对账：CSV {rep['csv_rows']} 行 / 库 {rep['db_rows']} 行")
    print(f"  缺失 {len(rep['missing_in_db'])}｜金标不一致 {len(rep['mismatched_gold'])}"
          f"｜库里多出 {len(rep['extra_in_db'])}")
    print(f"  落在混淆对上的：{rep['confusing_pair_rows']} 行")
    print(f"  gold 分布 top5：{list(rep['gold_distribution'].items())[:5]}")

    if not rep["ok"]:
        for m in rep["mismatched_gold"][:10]:
            print(f"    ✗ {m['image_path']}: CSV={m['csv']} 库={m['db']}")
        raise SystemExit("❌ 对账不一致")
    print("✅ 逐行对账一致")


if __name__ == "__main__":
    main()
