# Day 3 补记 · taxonomy 换成 v1.0-codebook 后的连带处理

测试 **66 passed**（新增 5 条）｜token 1038 / 2000 ✅｜HFSS 集合已人工逐条核对

---

## 1. 替换结果

| | v0.9-draft | v1.0-codebook |
|---|---|---|
| confirmed | 0 / 33 | **33 / 33** |
| `/api/health` confirmed_ratio | 0.0 | **1.0** |
| 混淆对 | (2,12) (5,19) (8,23) (16,17) (11,18) (11,25) | (2,12) (5,19) (8,23) (16,17) **(18,25) (25,29)** |

原 17 条差异项逐条复验，`name_en` 与 codebook 原文 **17/17 一致**。

---

## 2. HFSS 派生重验 —— 结论：**正则方案必须废掉**

按要求重跑了 `hfss_codes()` 的名称正则推导，逐条人工核对，抓到 3 处硬伤：

| code | 名称 | 正则结论 | 正确结论 | 错因 |
|---|---|---|---|---|
| **7** | 高不饱和脂肪油脂与低脂**咸味**酱 | ★HFSS | 非 HFSS | 被"咸味"误命中；codebook 里这是**健康向**油脂类 |
| **29** | 茶与咖啡（**不含甜味**粉剂冲调） | ★HFSS | 非 HFSS | 被"甜味"误命中，而名称里那是**否定**语义 |
| **13** | 调味/油炸即食米饭面条 | 非 HFSS | **HFSS** | 名称不含风险词，但 evidence_needed 就是钠+脂肪 |

`[29]` 这条最能说明问题：**正则读不懂"不含"**。一个把"不含甜味"判成高糖的规则，
放在一份要拿去做政策合规监测的系统里，是不能接受的。

### 改法

`hfss_codes()` 改为读**显式判定表** `taxonomy.HFSS_VERDICTS`：33 个 stable_code 一行结论 +
一句依据，可审、可 diff、答辩时能逐条解释。覆盖性由加载期校验强制 ——
taxonomy 里出现新编号而判定表没写，**启动即报错**，不会静默漏掉。

> 设计取向的调整：taxonomy.json 仍是**分类数据**的唯一来源，
> 但 HFSS 归属是叠加其上的一层**政策判断**，本来就不该从名称字符串里猜。
> 昨天写"从数据推导，名称一改自动跟着变"是过度自动化，今天这次替换正好把它证伪了。

### 最终 HFSS 集合（13 类）

```
12 高糖/低纤维谷物    13 调味油炸即食米面   14 甜味与高脂烘焙
15 盐腌加工肉         16 甜味零食           17 加盐加脂咸味零食
19 全脂奶酪奶制品     20 冰淇淋与甜点       21 巧克力与糖果
23 高脂/高盐餐食      24 高脂高盐酱料       25 含糖饮料
32 快餐（常规）
```

明确排除且有依据：`7` 健康油脂、`9` 核心食物健康零食、`11` 瓶装水、`29` 茶咖、`31` 快餐健康选项、
`10/30` 婴幼儿（单独监管口径）、`33/34` 无具体食品。

### 两条留给你拍板的边界

1. **`[26]` 酒精**：受广告监管，但不属于"高糖/高脂/高盐"口径。已**单列**
   （`taxonomy.alcohol_codes()`），报告里写成"酒精类另占 X%（不计入 HFSS）"。
   如果课题的监管框架把酒精并进同一口径，改 `HFSS_VERDICTS[26]` 一行即可。
2. **`[18]` 果汁/果汁饮料**：codebook 把它与含糖饮料 `[25]` 分列，因此按**非 HFSS** 计。
   但若课题采用的营养分级模型把果汁的糖计入游离糖（不少模型会），这条要翻。
   判定表里已写明这一行的可改性。

---

## 3. token 计量重跑

| | v0.9-draft | v1.0-codebook | 变化 |
|---|---|---|---|
| taxonomy 文本块 | 746 | **1038** | +39% |
| classify system prompt | 1239 | **1535** | +24% |
| adjudicate system prompt | 977 | 1272 | +30% |

预算 2000，**仍然够用**（文本块占 52%，整段 classify prompt 占 77%）。
涨幅来自 codebook 原文名称更长（如 `[14]` 单条就 90+ 字符）。

测试侧加了两道：`test_prompt_block_within_token_budget` 收紧为整段 prompt 也不得超 2000；
新增 `test_token_drift_against_recorded_baseline`，记录基线并在漂移 >25% 时报警 ——
下次再换 taxonomy 数据，"涨了多少"一眼可见，而不是等它悄悄顶到预算上限。

---

## 4. `_rule_based`：阈值一个没动，但分支重挂了

按要求**没有改任何阈值**（糖 15g / 脂 3g / 脂 10g·盐 1.2g / 糖 2.5g 全部原样），
adapter 仍是 `rule-fallback`，eval 双闸继续拦截。

但有一处必须动：旧代码有个 `{11, 18}` 分支（当时语义 = 含糖饮料 vs 无糖饮料）。
新 codebook 下 `11` = 瓶装水、`18` = 果汁，且 `(11,18)` 已不再是混淆对 ——
这个分支留着会得出 **"糖含量高 → 判为瓶装水"** 的反向结论。

已把它重挂到现行混淆对上，**沿用同一个 2.5 g/100ml 阈值**：

```python
elif {18, 25} & pool_set and sugar is not None:   # 果汁 vs 含糖饮料
    code = pick(18, 25, sugar >= 2.5, ...)
elif {25, 29} & pool_set and sugar is not None:   # 含糖饮料 vs 茶咖
    code = pick(29, 25, sugar >= 2.5, ...)
```

---

## 5. 测试变化

| 新增 | 内容 |
|---|---|
| `test_all_categories_confirmed` | confirmed_ratio == 1.0 且版本为 1.0-codebook |
| `test_token_drift_against_recorded_baseline` | token 基线漂移守卫 |
| `test_hfss_verdicts_cover_every_code` | 判定表覆盖全部编号，且每条都有依据 |
| `test_hfss_set_matches_codebook_semantics` | 重点盯 7 / 29 / 11 / 9 / 31 不得混入 |
| `test_alcohol_tracked_separately_from_hfss` | 酒精单列 |

`test_hfss_codes_derived_not_hardcoded` 已删除（它断言的正是被证伪的那个设计）。

---

## 6. 仍未变的遗留

- `_rule_based` 的阈值仍是占位值 —— 接真实营养分级模型时统一核。
- `tokens_in/out/cost_usd` 空位待接真实 provider。
