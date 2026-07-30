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


def import_csv(path: str | Path, source: str = "manual_label") -> int:
    """CSV 列要求：image_path, gold_specific（细类编号）。gold_general 自动回填。"""
    n = 0
    with Path(path).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = int(row["gold_specific"])
            if not taxonomy.is_valid(code):
                continue
            db.add_eval_sample(
                image_path=row["image_path"],
                gold_general=taxonomy.general_of(code),
                gold_specific=str(code),
                source=source,
                is_confusing_pair=any(code in p for p in taxonomy.CONFUSING_PAIRS),
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
