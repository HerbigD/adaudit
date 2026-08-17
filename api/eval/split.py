"""A2 · 数据切分：dev 200 / eval 300 / smoke 12，三者互斥、可复现。

## 为什么必须现在切（Day6 决议背景）

Day3 到 Day6 的每一次"改进"（阈值、混淆对、prompt、规则兜底）都是在**全集**上看效果的。
不切分就意味着：答辩时"我们没在测试集上调过参"这句话无法证明。
切分本身不能追溯地修复已经发生的调优，但它划下一条线 ——
**线之后的所有调参只准看 dev**，eval 集在出终版指标前只跑一次。

## 三条硬约定

1. **种子写进 config**（`SPLIT_SEED`），不写在命令行默认值里。
   命令行默认值改了没人会注意，config 改了 git diff 里跑不掉。
2. **分层维度是 country；有 language 时再叠上去**。
   四国样本量差异很大，随机切会让某国在 eval 里只剩几张，切片指标直接没意义。
3. **smoke 与 dev/eval 互斥**。冒烟集会被反复跑、反复看，它就是最脏的那部分数据，
   混进任何一边都算污染。

## 分层怎么做（`_allocate`）

按"每层应得配额 = 层占比 × 目标条数"算，再用**最大余数法**补齐到精确总数。
不用 `round()` 逐层取整 —— 那样总数会飘，飘出来的差额只能从最后一层硬砍，
等于让排序最末的那一层承担全部误差。

层内排序用 `(hash(seed, id), id)`，不是 `random.shuffle`：
同一个种子下，**新增样本不会打乱既有样本的相对顺序**，
所以金标池扩容后 dev/eval 的重叠部分是稳定的，指标可比。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import settings

SPLITS = ("dev", "eval", "smoke")


@dataclass
class Row:
    """金标池的一行。`extra` 保留 csv 里的其余列，切分不丢信息。"""

    id: str
    image_path: str
    gold_specific: int
    country: str = "UNK"
    language: str = "unk"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def stratum(self) -> str:
        return f"{self.country}|{self.language}"


# --------------------------------------------------------------------------- #
# 读入
# --------------------------------------------------------------------------- #
_ID_KEYS = ("id", "sample_id", "image_id", "ad_id")
_IMG_KEYS = ("image_path", "image", "path", "file", "filename")
_GOLD_KEYS = ("gold_specific", "gold", "specific_code", "code", "label")
_COUNTRY_KEYS = ("country", "country_code", "market")
_LANG_KEYS = ("language", "ad_language", "lang")


def _pick(row: dict[str, str], keys: Sequence[str]) -> str | None:
    for k in keys:
        for actual in row:
            if actual.strip().lower() == k:
                v = (row[actual] or "").strip()
                if v:
                    return v
    return None


def read_pool(path: str | Path) -> list[Row]:
    """读金标池 csv。列名大小写不敏感，常见别名都认（见 `_*_KEYS`）。

    **只收单标签行**：`gold_specific` 里出现分隔符（`;` `,` `|` `/`）
    说明这是 A1 封存的多标签样本，直接跳过并计数，不静默混进来。
    """
    from services import taxonomy

    rows: list[Row] = []
    skipped = Counter()
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        for i, raw in enumerate(csv.DictReader(f), start=1):
            gold = _pick(raw, _GOLD_KEYS)
            img = _pick(raw, _IMG_KEYS)
            if not gold or not img:
                skipped["缺 image_path 或 gold_specific"] += 1
                continue
            if any(sep in gold for sep in ";,|/"):
                skipped["多标签（A1 已封存，不进切分）"] += 1
                continue
            code = taxonomy.normalize(gold)
            if code is None:
                skipped[f"非法编号 {gold}"] += 1
                continue
            known = {
                k.strip().lower()
                for keys in (_ID_KEYS, _IMG_KEYS, _GOLD_KEYS, _COUNTRY_KEYS, _LANG_KEYS)
                for k in keys
            }
            rows.append(
                Row(
                    id=_pick(raw, _ID_KEYS) or f"row{i:05d}",
                    image_path=img,
                    gold_specific=code,
                    # 不截断：语言列可能是 ISO 码（en/hi/bn），也可能是粗桶
                    # （en_only / local_only / mixed / na）。截到 5 个字符会把
                    # `en_only` 和 `en_on...` 之类混成一团，分层直接错。
                    country=(_pick(raw, _COUNTRY_KEYS) or "UNK").strip().upper(),
                    language=(_pick(raw, _LANG_KEYS) or "unk").strip().lower(),
                    extra={k: v for k, v in raw.items() if k.strip().lower() not in known},
                )
            )
    if skipped:
        for reason, n in skipped.items():
            print(f"[split] 跳过 {n} 行：{reason}")
    return rows


# --------------------------------------------------------------------------- #
# 分层配额
# --------------------------------------------------------------------------- #
def _allocate(sizes: dict[str, int], target: int) -> dict[str, int]:
    """最大余数法：按层占比分配 target 个名额，总数精确等于 target（或全部可用量）。"""
    total = sum(sizes.values())
    if total == 0 or target <= 0:
        return {k: 0 for k in sizes}
    target = min(target, total)

    exact = {k: v * target / total for k, v in sizes.items()}
    quota = {k: min(int(v), sizes[k]) for k, v in exact.items()}
    short = target - sum(quota.values())

    # 余数大的先补；余数相同按层名排序，保证可复现
    order = sorted(sizes, key=lambda k: (-(exact[k] - int(exact[k])), k))
    while short > 0:
        progressed = False
        for k in order:
            if short == 0:
                break
            if quota[k] < sizes[k]:
                quota[k] += 1
                short -= 1
                progressed = True
        if not progressed:            # 所有层都取满了
            break
    return quota


def _sort_key(seed: int, row: Row) -> tuple[str, str]:
    """稳定伪随机序：同一种子下新增样本不会打乱既有样本的相对顺序。"""
    h = hashlib.sha256(f"{seed}:{row.id}:{row.image_path}".encode()).hexdigest()
    return (h, row.id)


def resolve_params(**kw) -> dict[str, int]:
    """把 `seed/dev/ev/smoke` 的 None 解析成 config 默认值。

    **单独抽出来是因为踩过一次**：`build()` 原先用 `kw.get("seed", settings.split_seed)`
    往 manifest 里写种子。CLI 不传 `--seed` 时传的是 `seed=None` —— key 存在、值是 None，
    `dict.get` 的默认值**不会生效**，于是 manifest 里的 seed 记成了 `null`。
    切分本身没错（`stratified_split` 内部另做了 None 解析），但 manifest 是
    "这份切分怎么来的"的唯一凭据，它记 null 等于这份切分不可复现 —— 比切错还糟，
    因为看起来一切正常。现在解析只有这一处，写 manifest 与做切分共用同一份结果。
    """
    return {
        "seed": settings.split_seed if kw.get("seed") is None else kw["seed"],
        "dev": settings.split_dev_size if kw.get("dev") is None else kw["dev"],
        "ev": settings.split_eval_size if kw.get("ev") is None else kw["ev"],
        "smoke": settings.split_smoke_size if kw.get("smoke") is None else kw["smoke"],
    }


def stratified_split(
    rows: Iterable[Row],
    *,
    seed: int | None = None,
    dev: int | None = None,
    ev: int | None = None,
    smoke: int | None = None,
) -> dict[str, list[Row]]:
    """按 country|language 分层切出 smoke → dev → eval，三者互斥。

    **顺序是有意的**：先取 smoke（它最小、最需要覆盖多样性），
    再取 dev，剩下的池子里取 eval。反过来会让 smoke 抢走 eval 的分层名额。
    """
    p = resolve_params(seed=seed, dev=dev, ev=ev, smoke=smoke)
    seed, dev, ev, smoke = p["seed"], p["dev"], p["ev"], p["smoke"]

    buckets: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        buckets[r.stratum].append(r)
    for k in buckets:
        buckets[k].sort(key=lambda r: _sort_key(seed, r))

    out: dict[str, list[Row]] = {}
    cursor = {k: 0 for k in buckets}

    # smoke 走**覆盖优先**而不是按比例：它只有十来条，按比例切会全给印度，
    # 而冒烟集的用途恰恰是"五语种 OCR 各来一张"。先每层轮一张，还有余额再按比例补。
    out["smoke"] = _take(buckets, cursor, _coverage_quota(buckets, cursor, smoke), seed)

    for name, target in (("dev", dev), ("eval", ev)):
        avail = {k: len(v) - cursor[k] for k, v in buckets.items()}
        out[name] = _take(buckets, cursor, _allocate(avail, target), seed)
    return out


def _take(
    buckets: dict[str, list[Row]], cursor: dict[str, int], quota: dict[str, int], seed: int
) -> list[Row]:
    picked: list[Row] = []
    for k in sorted(buckets):
        take = quota.get(k, 0)
        picked.extend(buckets[k][cursor[k]: cursor[k] + take])
        cursor[k] += take
    picked.sort(key=lambda r: _sort_key(seed, r))
    return picked


def _coverage_quota(
    buckets: dict[str, list[Row]], cursor: dict[str, int], target: int
) -> dict[str, int]:
    """覆盖优先配额：先保证每个语言出现一次、每个国家出现一次，再按层大小轮补。"""
    quota = {k: 0 for k in buckets}
    avail = {k: len(v) - cursor[k] for k, v in buckets.items()}
    if target <= 0:
        return quota

    def _add(k: str) -> bool:
        if quota[k] < avail[k] and sum(quota.values()) < target:
            quota[k] += 1
            return True
        return False

    # 层大的优先代表本语言/本国 —— 同一语言下挑样本最多的那一层，最不容易挑到怪样本
    by_lang: dict[str, list[str]] = defaultdict(list)
    by_country: dict[str, list[str]] = defaultdict(list)
    for k in buckets:
        country, _, lang = k.partition("|")
        by_lang[lang].append(k)
        by_country[country].append(k)
    for group in (by_lang, by_country):
        for key in sorted(group):
            for k in sorted(group[key], key=lambda x: (-avail[x], x)):
                if quota[k] or _add(k):
                    break

    for k in sorted(buckets, key=lambda x: (-avail[x], x)):
        while sum(quota.values()) < target and _add(k):
            break
    while sum(quota.values()) < target and any(quota[k] < avail[k] for k in buckets):
        for k in sorted(buckets, key=lambda x: (-avail[x], x)):
            if sum(quota.values()) >= target:
                break
            _add(k)
    return quota


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
_COLUMNS = ["id", "image_path", "gold_specific", "country", "language", "split"]


def write_csv(path: Path, name: str, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_COLUMNS)
        for r in rows:
            w.writerow([r.id, r.image_path, r.gold_specific, r.country, r.language, name])


def _distribution(rows: list[Row]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "country": dict(sorted(Counter(r.country for r in rows).items())),
        "language": dict(sorted(Counter(r.language for r in rows).items())),
        "gold_specific": dict(sorted(Counter(r.gold_specific for r in rows).items())),
    }


def build(pool_csv: str | Path, out_dir: str | Path | None = None, **kw) -> dict[str, Any]:
    """读池 → 切分 → 写三份 csv + manifest。返回 manifest（也落盘）。"""
    rows = read_pool(pool_csv)
    if not rows:
        raise SystemExit(f"[split] 金标池为空或列名对不上：{pool_csv}")

    params = resolve_params(**kw)          # 解析一次，切分与 manifest 共用
    parts = stratified_split(
        rows, seed=params["seed"], dev=params["dev"],
        ev=params["ev"], smoke=params["smoke"],
    )
    out = Path(out_dir or settings.split_dir)

    ids = [r.id for name in SPLITS for r in parts[name]]
    assert len(ids) == len(set(ids)), "切分结果有重叠 —— 这是 bug，不是数据问题"

    manifest = {
        "pool_csv": str(pool_csv),
        "pool_size": len(rows),
        "seed": params["seed"],
        "sizes_requested": {k: params[k] for k in ("dev", "ev", "smoke")},
        "stratify_by": ["country", "language"],
        "sizes": {name: len(parts[name]) for name in SPLITS},
        "mutually_exclusive": True,
        "distribution": {name: _distribution(parts[name]) for name in SPLITS},
        "pool_distribution": _distribution(rows),
        "discipline": (
            "dev 用于调参与误差分析；eval 在出终版指标前只准跑一次；"
            "smoke 只用于链路自测，其结果永远不进指标。"
            "A3 的经验混淆对只能来自 dev。"
        ),
    }
    for name in SPLITS:
        write_csv(out / f"{name}.csv", name, parts[name])
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def load_split(name: str, out_dir: str | Path | None = None) -> list[Row]:
    """读回某一份切分。文件不存在时给出可执行的下一步，而不是抛 FileNotFoundError。"""
    path = Path(out_dir or settings.split_dir) / f"{name}.csv"
    if not path.exists():
        raise SystemExit(
            f"[split] 找不到 {path}。先跑：\n"
            f"    python -m eval.split --pool <金标池.csv>"
        )
    return read_pool(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="A2 数据切分：dev/eval/smoke，分层且可复现")
    ap.add_argument("--pool", required=True, help="单标签金标池 csv")
    ap.add_argument("--out", default=None, help=f"输出目录，默认 {settings.split_dir}")
    ap.add_argument("--seed", type=int, default=None, help="默认取 config.SPLIT_SEED")
    ap.add_argument("--dev", type=int, default=None)
    ap.add_argument("--eval", dest="ev", type=int, default=None)
    ap.add_argument("--smoke", type=int, default=None)
    args = ap.parse_args()

    m = build(args.pool, args.out, seed=args.seed, dev=args.dev, ev=args.ev, smoke=args.smoke)
    print(json.dumps(m, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
