"""挑出「混淆对冒烟」与「OCR 语种冒烟」两组图，复制成两个小目录。

手册 §2 / §3 要求「≥3 张落在混淆对上的图」和「五语种各 1 张」，
但没说这些图从哪儿来 —— 这个脚本负责把它们挑出来并落盘。

## 一条必须遵守的约束：不能用消融清单里的图

跑一次冒烟就会往 `product_cache` 写一条该产品的档案。
如果冒烟用的是消融清单里的图，之后跑 720 次消融时 `cache_lookup` 会**命中缓存、
跳过联网搜索** —— 四臂比的就不再是「prompt 里放不放混淆对」，
而混进了「这张图之前被跑过没有」。那是把实验条件搞脏了。

所以这里显式排除 `ablation_manifest_ids.csv` ∪ `eval.csv` ∪ `smoke.csv` 的 id。
（实测 ablation ∩ dev = 11 —— 光从 dev 里挑并不安全，必须按 id 排除。）

## 语种那一组的实话

金标 CSV 的 `language_raw` 只有 sinhala / tamil / english / NA 这几种，
**没有 hi / bn / ur 的标注**。所以「五语种」只能按国家近似：
LK-sinhala / LK-tamil / IN / BD / PK，各挑一张，
实际图上是什么文字**要你看图确认** —— 这一项本来也是人工核对的。

## 用法

    cd api
    python3 scripts/make_smoke_sets.py                 # 用默认图片根
    python3 scripts/make_smoke_sets.py --dry-run       # 只看挑了哪些，不复制
    python3 scripts/make_smoke_sets.py --image-root /path/to/images
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings                       # noqa: E402

SPLIT_DIR = Path(settings.split_dir)
OUT_ROOT = Path(settings.db_path).parent
DEFAULT_IMAGE_ROOT = Path.home() / "imperial_foodad" / "# Images - food only"

# 每组各挑一张的「语种」桶。(标签, 判定函数)
LANG_BUCKETS = [
    ("si  斯里兰卡-僧伽罗", lambda r: r["country"] == "LK" and "sinhala" in r["language_raw"].lower()),
    ("ta  斯里兰卡-泰米尔", lambda r: r["country"] == "LK" and "tamil" in r["language_raw"].lower()),
    ("en  印度-英文",       lambda r: r["country"] == "IN" and "english" in r["language_raw"].lower()),
    ("bn  孟加拉",          lambda r: r["country"] == "BD"),
    ("ur  巴基斯坦",        lambda r: r["country"] == "PK"),
]


def _read(name: str) -> list[dict[str, str]]:
    path = SPLIT_DIR / name
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _held_out_ids() -> set[str]:
    """跑冒烟会污染缓存的那些 id —— 一个都不能用。"""
    out: set[str] = set()
    for name in ("ablation_manifest_ids.csv", "eval.csv", "smoke.csv"):
        out |= {r["id"] for r in _read(name)}
    return out


def _tier1_pairs() -> list[tuple[int, int]]:
    """Tier 1（definitional）混淆对，直接读消融 manifest，不依赖 taxonomy 内部结构。"""
    m = json.loads((SPLIT_DIR / "ablation_manifest.json").read_text(encoding="utf-8"))
    keys = m["confusing"]["by_pair"]["definitional"]
    return [tuple(int(x) for x in k.split("/")) for k in keys]


def pick(n_confusion: int) -> dict[str, list[dict]]:
    pool = _read("pool_4942.csv")
    banned = _held_out_ids()
    avail = [r for r in pool if r["id"] not in banned]
    print(f"金标池 {len(pool)} 张 → 排除消融/eval/smoke 后可用 {len(avail)} 张")

    # ---- 混淆对组：每对只取 1 张，取不同的对，覆盖面比数量重要 ----
    by_code: dict[int, list[dict]] = {}
    for r in avail:
        by_code.setdefault(int(r["gold_specific"]), []).append(r)

    confusion, used_pairs = [], []
    for a, b in _tier1_pairs():
        if len(confusion) >= n_confusion:
            break
        for code in (a, b):
            cands = by_code.get(code) or []
            if cands:
                r = dict(cands[0])
                r["_pair"] = f"{a}/{b}"
                r["_note"] = f"gold={code}，若改判应落到 {b if code == a else a}"
                confusion.append(r)
                used_pairs.append(f"{a}/{b}")
                break

    # ---- 语种组：每桶 1 张 ----
    ocr = []
    for label, ok in LANG_BUCKETS:
        hit = next((r for r in avail if ok(r)), None)
        if hit is None:
            print(f"  ⚠️  {label}：池子里没有符合的样本，跳过")
            continue
        r = dict(hit)
        r["_bucket"] = label
        ocr.append(r)

    return {"confusion": confusion, "ocr": ocr}


def materialise(rows: list[dict], out_dir: Path, image_root: Path, dry: bool) -> list[dict]:
    """把选中的图复制过去。缺图**报错而不是跳过** —— 悄悄少一张会让验收数对不上。"""
    manifest, missing = [], []
    if not dry:
        out_dir.mkdir(parents=True, exist_ok=True)
    for r in rows:
        src = image_root / r["image_path"]
        dst = out_dir / f"{r['id']}{src.suffix}"
        entry = {k: v for k, v in r.items() if not k.startswith("_") or True}
        entry["src"] = str(src)
        entry["dst"] = str(dst)
        if not src.exists():
            missing.append(str(src))
        elif not dry:
            shutil.copy2(src, dst)
        manifest.append(entry)
    if missing:
        print(f"  ❌ {len(missing)} 张图在图片根下找不到，例如：")
        for m in missing[:3]:
            print(f"     {m}")
        raise SystemExit("图片根路径不对，或数据集不完整 —— 用 --image-root 指定正确的目录")
    if not dry:
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-root", default=str(DEFAULT_IMAGE_ROOT))
    ap.add_argument("--n-confusion", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="只打印挑了哪些，不复制")
    args = ap.parse_args()

    image_root = Path(args.image_root).expanduser()
    print(f"图片根：{image_root}")
    if not image_root.exists():
        raise SystemExit(f"图片根不存在：{image_root}")

    sel = pick(args.n_confusion)

    print(f"\n混淆对冒烟（{len(sel['confusion'])} 张 → data/smoke_confusion/）")
    for r in sel["confusion"]:
        print(f"  {r['id']}  对={r['_pair']:<7} {r['_note']}  [{r['country']}] {r['image_path']}")

    print(f"\nOCR 语种冒烟（{len(sel['ocr'])} 张 → data/smoke_ocr/）")
    for r in sel["ocr"]:
        print(f"  {r['id']}  {r['_bucket']:<18} gold={r['gold_specific']:<3} {r['image_path']}")

    materialise(sel["confusion"], OUT_ROOT / "smoke_confusion", image_root, args.dry_run)
    materialise(sel["ocr"], OUT_ROOT / "smoke_ocr", image_root, args.dry_run)

    if args.dry_run:
        print("\n（--dry-run：没有复制任何文件）")
    else:
        print(f"\n✅ 已生成：\n  {OUT_ROOT / 'smoke_confusion'}\n  {OUT_ROOT / 'smoke_ocr'}")
        print("   每个目录下的 manifest.json 记着每张图的 id / gold / 该往哪边改判，"
              "验收时对答案用。")


if __name__ == "__main__":
    main()
