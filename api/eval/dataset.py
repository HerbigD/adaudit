"""评测集读写：CSV/Excel 金标 ↔ eval_samples 表。

评测集规模：6,314 张广告 + gt（方案 §7）。W7 跑批时按 sample 抽子集。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import db
from services import taxonomy


@dataclass
class Sample:
    id: str
    image_path: str
    gold_specific: int
    gold_general: str
    source: str = "manual_label"
    is_confusing_pair: bool = False
    # A2 切分带来的字段：金标侧的国家/语言（D3 切片要用金标而不是模型判读），
    # 以及这条属于哪一份切分 —— 指标输出里必须能看到它是 dev 还是 eval。
    country: str | None = None
    language: str | None = None
    split: str | None = None


def from_split(
    name: str, limit: int | None = None, only_confusing: bool = False
) -> list[Sample]:
    """从 A2 的切分 csv 读样本（`data/splits/{dev,eval,smoke}.csv`）。

    **这是跑批的正路**。`load()` 直读 `eval_samples` 表，那张表里没有切分概念，
    W7 出终版指标时用它就等于又在全集上跑，A2 的隔离白做。
    """
    from eval import split as split_mod

    rows = split_mod.load_split(name)
    out = [
        Sample(
            id=r.id,
            image_path=r.image_path,
            gold_specific=r.gold_specific,
            gold_general=taxonomy.general_of(r.gold_specific),
            source="split",
            is_confusing_pair=any(r.gold_specific in p for p in taxonomy.confusing_pairs()),
            country=r.country,
            language=r.language,
            split=name,
        )
        for r in rows
    ]
    if only_confusing:
        out = [s for s in out if s.is_confusing_pair]
    return out[:limit] if limit else out


def import_csv(path: str | Path, source: str = "manual_label") -> int:
    """CSV 列要求：image_path, gold_specific（细类编号）。gold_general 自动回填。"""
    n = 0
    with Path(path).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = taxonomy.normalize(row["gold_specific"])   # 历史 22 → 32
            if code is None:
                continue
            db.add_eval_sample(
                image_path=row["image_path"],
                gold_general=taxonomy.general_of(code),
                gold_specific=str(code),
                source=source,
                is_confusing_pair=any(code in p for p in taxonomy.confusing_pairs()),
            )
            n += 1
    return n


def load(limit: int | None = None, only_confusing: bool = False) -> list[Sample]:
    sql = "SELECT * FROM eval_samples"
    args: list = []
    if only_confusing:
        sql += " WHERE is_confusing_pair=1"
    sql += " ORDER BY created_at"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with db.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return [
        Sample(
            id=r["id"],
            image_path=r["image_path"],
            gold_specific=int(r["gold_specific"]),
            gold_general=r["gold_general"],
            source=r["source"],
            is_confusing_pair=bool(r["is_confusing_pair"]),
        )
        for r in rows
        if r["gold_specific"] and r["gold_specific"].isdigit()
    ]


def export_csv(path: str | Path) -> int:
    samples = load()
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "image_path", "gold_specific", "gold_general", "source"])
        for s in samples:
            w.writerow([s.id, s.image_path, s.gold_specific, s.gold_general, s.source])
    return len(samples)
