# Day 6（续四）· A10 配额 + 单对上限 · 抽样清单待过目

> 214 tests passed（此前 207）。抽样清单已按批准的配额与上限重出。
> **§4 是给你过目的那张表**，确认后就可以跑（约 $1–2）。

---

## §1 配额：60 / 30

```bash
python -m eval.runner --ablation --dry-run \
  --tier-quota "definitional=60,definitional_compositional=30"
```

| 对比 | 由哪一档驱动 | n | 可解读 |
|---|---|---|---|
| **B − A** | Tier 1 definitional | **60** | ✅ ~15pp |
| **B2 − B** | Tier 2 compositional | **30** | ✅ ~20pp |
| **C − B2** | Tier 3 dev 经验对 | **0** | ❌ 预期内 |

`C−B2` 的 0 已写进 manifest 的 `methods_wording`，报告口径固定为：

> Tier 3 经验对尚未积累，该对比留待后续。

---

## §2 单对上限：从 72% 奶降到 20%

### 不设约束会怎样

Tier 1 候选池 489 条，形状严重偏斜：

| 对 | 池中条数 | 占比 |
|---|---|---|
| **5/19**（奶与酸奶） | **350** | **72%** |
| 3/18（果汁） | 57 | 12% |
| 7/24（咸味酱） | 44 | 9% |
| 8/23（餐食） | 21 | 4% |
| 2/12（谷物） | 17 | 3% |

按旧的"取前 60"抽出来 5/19 会占一半以上 —— B−A 实际测的是"模型在奶类上的表现"，
而结论会被写成"阈值型混淆对上置信度先验有效"。那是两回事。
（这条已用反证用例钉住：`test_the_old_top_n_draw_would_have_been_milk_heavy`。）

### 抽完的结果

**Tier 1 · 60 条**（上限 30，实际每对 12）

| 对 | n | 占本档 |
|---|---|---|
| 2/12 | 12 | 20% |
| 3/18 | 12 | 20% |
| 5/19 | 12 | 20% |
| 7/24 | 12 | 20% |
| 8/23 | 12 | 20% |

**5/19 从 72% 降到 20%，五对全覆盖。**

**Tier 2 · 30 条**（上限 15，实际 3–4）

| 对 | n | | 对 | n |
|---|---|---|---|---|
| 1/13 | 4 | | 7/27 | 4 |
| 4/17 | 4 | | 11/25 | 4 |
| 6/15 | 4 | | 31/32 | 3 |
| 14/16 | 4 | | 33/34 | 3 |

八对全覆盖。

### 一处我按理由执行而非按字面执行，需要你知道

裁决原文是"单一对不超过 30 条，不足的从其他对补"。
**按字面贪心执行**：5/19 取满 30 → 再从 3/18 取 30 → 60 条只覆盖 **2 对**。
上限满足了，B−A 仍然是个窄结论。

**我改成按对轮流取**（round-robin，触到上限就跳过）：同样满足 ≤30，
但覆盖全部 5 对 —— 它服务的正是你设这条上限的理由。
per-pair 分布进 manifest 和这份日报，你可以直接核。

### 还有一点必须说清楚：**均衡来自轮流取，不来自上限**

当前池形状下每对 12 条，12 远低于上限 30 —— **上限一次都没触发**。
它是兜底，只在"某些对提前取空、剩下的对被迫多担"时才起作用。
把 `PAIR_CAP_RATIO` 从 0.5 改到 1.0，结果一模一样（有用例断言这件事）。

之所以特意写下来：日后有人想调分布时，会很自然地去改那个帽 —— 改了不会有任何效果，
真正的旋钮是抽法。

---

## §3 Methods 措辞约束已进 manifest

不靠人记得，跟着清单走：

> B−A 的结论仅覆盖本次实际抽到的混淆对组合（见 `confusing.by_pair`），
> 不得表述为「在所有阈值型混淆对上成立」。
> Tier 3 经验对尚未积累，C−B2 该对比留待后续。

有用例断言这段文字必须在 manifest 里（`test_manifest_carries_the_methods_wording`）。

---

## §4 ⚠️ 抽样清单 —— 请过目这张表

```
api/data/splits/ablation_manifest.json       口径 + 分布 + 支撑量
api/data/splits/ablation_manifest_ids.csv    180 行，含 group / tier / pair / id / 图片路径 / gold / 国家 / 语言
```

| | n | LK | BD | IN | PK | en_only | mixed | local_only | na |
|---|---|---|---|---|---|---|---|---|---|
| 混淆组 | 90 | 63 | 4 | 14 | 9 | 20 | 45 | 9 | 16 |
| 对照组 | 90 | 63 | 4 | 14 | 9 | — | — | — | — |

对照组按混淆组的国家×语言分布配对抽取，**这次凑满了 90**。

核对项：

- `overlap_with_held_out: 0` —— 与 eval / smoke 零重叠（代码断言，不通过会抛异常）
- `seed: 20260731`
- Tier 1 / Tier 2 的 per-pair 分布见 §2
- 27 张 parked 金标（35/36/38）不在清单里

清单里的一行长这样：

```
confusing,definitional_compositional,11/25,1682830324380,Bangladesh/1682830324380.jpg,25,BD,na
```

**确认无误后**：

```bash
python -m eval.runner --ablation \
  --tier-quota "definitional=60,definitional_compositional=30"
```

180 条 × 4 臂 = 720 次真实调用，约 $1–2。
熔断上限 500k token/日仍然生效，跑之前可以先 `--fuse-test` 确认。

---

## §5 变更清单

**修改**

- `api/eval/ablation.py` —— `pair_of()`、`_draw_with_pair_cap()`、`PAIR_CAP_RATIO`、
  manifest 加 `by_pair` / `pair_cap_ratio` / `methods_wording`，id 清单加 `pair` 列
- `api/eval/runner.py` —— `--pair-cap`
- `api/tests/test_ablation_set.py` —— 新增 7 例（上限、覆盖面、反证、兜底语义、措辞）
- `api/data/splits/ablation_manifest.json` / `_ids.csv` —— 按新配额与上限重出

**测试**：214 passed（此前 207）

---

## §6 剩下的

1. **你过目 §4 的清单** → 跑 `--ablation`
2. A5（五处定义补全）仍等你比对 codebook
3. Day 12：D1 的非对称覆盖只是半个方案，Amul Toned → Double Toned 仍会误命中
