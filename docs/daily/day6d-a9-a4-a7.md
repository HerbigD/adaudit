# Day 6（续三）· A9 / A4 / A7 执行 + manifest 种子修复

> 207 tests passed（此前 194）｜抽样清单已出，**等你过目再跑**（A9 纪律）。
> 三条裁决全部落码；一处需要你选一下配额，见 §1.3。

---

## §1 A9 · 消融集从「池 − eval − smoke」抽取

### 1.1 三个条件都落进代码，不是落进文档

| 条件 | 落点 | 怎么保证 |
|---|---|---|
| ① 与 eval / smoke 严格互斥 | `ablation._assert_disjoint()` | **返回前断言，抛异常不打日志** |
| ② Methods 单列 "ablation set" | `ablation.manifest()` | 产出 provenance / 来源 / 规模，与 dev·eval 并列写进 README |
| ③ Tier 3 仍只准来自 dev | `manifest.tier3_constraint` + 用例 | 消融集扩大**不**放宽这条 |

条件①**故意抛异常而不是打日志**：一条 eval 样本混进消融集，产出的数字看起来
完全正常，没有任何迹象提示它被污染过，而日志会被滚掉。异常不会。
`test_contamination_raises_instead_of_logging` 正面验证了这个熔断本身。

实测：`∩eval = 0`、`∩smoke = 0`、混淆组与对照组互不重叠。
（与 dev 有 9 条重叠 —— 这是允许的，dev 不是 held-out。）

### 1.2 抽样清单已生成，等你过目

```
api/data/splits/ablation_manifest.json      分布 + 口径 + 支撑量
api/data/splits/ablation_manifest_ids.csv   180 行逐条 id / 图片路径 / gold / 国家 / 语言
```

按裁决原文（先取满 Tier 1）的结果：

| | n | 按档 | LK | BD | IN | PK |
|---|---|---|---|---|---|---|
| 混淆组 | 90 | Tier 1 × 90 | 66 | 5 | 12 | 7 |
| 对照组 | 90 | 非混淆 | 66 | 5 | 12 | 7 |

混淆组 gold 分布：`19×56, 18×10, 5×5, 7×5, 23×5, 3×4, 24×4, 12×1`
——19（全脂奶/酸奶）占 62%，这是池子本身的形状（全池 Tier 1 样本里 19 就有 334 条），
不是抽样偏差。但它意味着 **B−A 的结论主要是关于 5/19 这一对的**，
写 Methods 时得说清楚，不能讲成"在所有阈值型混淆对上都成立"。

跑批命令（确认清单后去掉 `--dry-run`）：

```bash
python -m eval.runner --ablation --dry-run    # 只出清单，不花钱
python -m eval.runner --ablation              # 真跑，180 条 × 4 臂 = 720 次调用
```

`--dry-run` 走在 `_preflight` **之前** —— 出清单不需要真实 provider，
而"跑批前先给人看一眼"恰恰是在 provider 还没接好的时候要做的事。

### 1.3 ⚠️ 需要你选一下：按原文抽，B2−B 就做不出来

裁决原文是"先取满 Tier 1 再用 Tier 2 补"。池里 Tier 1 有 523 条，
所以 90 条**全部**来自 Tier 1 —— B−A 拿到满额支撑，但：

```
B−A   n=90  ✅ 可解读
B2−B  n=0   ❌ 做不出来（没有 Tier 2 样本，B 与 B2 两臂在这批样本上 prompt 无实际差别）
C−B2  n=0   ❌ 做不出来（Tier 3 目前一对都没有，属预期）
```

`C−B2` 为 0 是正常的（Tier 3 要等 dev 误差分析产出经验对）。
但 `B2−B` 为 0 是**配额的直接后果** —— 而裁决①刚把 Tier 2 单独拆成一臂，
就是为了单独看它的贡献。按原文抽等于那一臂白设。

两个选择：

**a. 就按原文**（当前清单）—— B−A 满额 90，B2−B 留到以后单独跑一轮。
   好处：最关键的那一问答得最实；坏处：Tier 2 的价值这轮说不出话。

**b. 分配额**，一条命令的事：

```bash
python -m eval.runner --ablation --dry-run --tier-quota "definitional=60,definitional_compositional=30"
```

→ `B−A n=60`、`B2−B n=30`，两个对比都可解读（都 ≥30）。
   代价：B−A 的支撑从 90 降到 60。

**我倾向 b。** 60 条对 B−A 仍够用（能看出 ~15pp 以上的效应），
而 30 条至少让 Tier 2 那一臂有话说 —— 否则这轮 720 次调用里有一半
（B2 臂的 180 次）产出不了任何可解读的对比。

代码不替你选：不给 `--tier-quota` 就按原文，给了就按你的。
支撑量随结果一起报（`contrast_support`），每档的 n 写在纸上，
读 DiD 时不会拿 90 的分母去解释一个 30 条支撑的差值。

---

## §2 A4 · 维持 33 类

- `taxonomy.json` 的 `meta.known_gaps` 改写为裁决原文：35/36/37 不补进 taxonomy
- 27 张 parked 金标（`unrepresentable_gold.csv`）**不进任何抽样**——
  `ablation._representable()` 在候选池构造时就滤掉，有用例钉死
- Tier 2 的 `(35,36)` 维持 `compositional_pairs._dropped` 留痕现状
- README 的评测集口径里写明"Day 12 前不进任何指标，eval 之后再决定去留"

parked ≠ 删掉：清单原样留在 `data/splits/`，27 行可查。

---

## §3 A7 · 22 → 32 按同义处理

`taxonomy.json` 新增 `meta.merge_22_to_32`，含 Methods 原句：

> 22/32 在 codebook 中定义一致，标注与评测均按 32 计。

回归用例 `test_pool_uses_merged_gold_not_raw` 钉死三件事：
池里 `gold_code_raw == 22` 的正好 80 行、这 80 行的 `gold_specific` 全是 32、
全池不存在 `gold_specific == 22`。用错列会让这 80 张（1.6%）全判错。

---

## §4 小修复 · manifest 的 seed 记成了 null

### 成因

`build()` 原先这样写：

```python
"seed": kw.get("seed", settings.split_seed)
```

CLI 不传 `--seed` 时传的是 `seed=None` —— **key 存在、值是 None**，
`dict.get` 的默认值不会生效，于是 manifest 里记了 `null`。

### 为什么这条值得单独修

切分本身没错（`stratified_split` 内部另做了一次 None 解析，实际用的是 20260731）。
但 manifest 是"这份切分怎么来的"的**唯一凭据**，它记 null 等于这份切分不可复现 ——
**比切错还糟**，因为一切看起来都正常。

### 改法

解析抽成 `resolve_params()`，**只有这一处**，切分与 manifest 共用同一份结果。
顺带把请求参数也记进 `sizes_requested`，实际规模与请求规模能对上。

```json
"seed": 20260731,
"sizes_requested": {"dev": 200, "ev": 300, "smoke": 12},
"sizes": {"dev": 200, "eval": 300, "smoke": 12}
```

同类问题顺手修了一处：`manifest` 的 `by_gold` 键原是 int，
落盘再读回来会变成 str，两者不相等 —— 一致性校验形同虚设。已统一成 str，用例断言往返相等。

---

## §5 变更清单

**新增**

- `api/tests/test_ablation_set.py`（13 例）—— A9 三条件 + A4 + A7 + 种子回归
- `api/data/splits/ablation_manifest.json` / `_ids.csv` —— 抽样清单，**等你过目**

**修改**

- `api/eval/ablation.py` —— `_candidate_pool()` 池减 held-out、`_assert_disjoint()`、
  `manifest()` / `write_manifest()`、`contrast_support()`、`parse_tier_quota()`、按档配额
- `api/eval/runner.py` —— `--dry-run`、`--tier-quota`，默认来源改 `pool_minus_heldout`
- `api/eval/split.py` —— `resolve_params()`，manifest 记解析后的种子
- `api/data/taxonomy.json` —— `meta.known_gaps`（A4）、`meta.merge_22_to_32`（A7）
- `README.md` —— 评测集口径表（四份子集并列）+ 三条 Methods 原句 + 测试表补 5 行

**测试**：207 passed（此前 194）

---

## §6 下一步

1. **§1.3 选 a 还是 b** → 确认清单 → 你在本机跑 `--ablation`
2. A5（五处定义补全）仍等你比对 codebook
3. Day 12：D1 的非对称覆盖只是半个方案，Amul Toned → Double Toned 仍会误命中
