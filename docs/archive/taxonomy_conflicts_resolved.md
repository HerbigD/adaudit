# ✅ 已解决 · 归档：taxonomy.json 与原 codebook 的差异清单

> **状态：已解决（2026-07-30）。本文档仅作归档留痕，不再是待办项。**
>
> **解决方式**：`api/data/taxonomy.json` 已按原始 codebook（`General Category.txt`）逐条重写为
> **v1.0-codebook**，33 条全部 `confirmed=true`。下文 A/B/C/D 四类差异项已逐条复验，
> `name_en` 与 codebook 原文 **17/17 一致**。
>
> **替换后的连带处理**（详见 `docs/daily/day3-taxonomy-update.md`）：
> 1. HFSS 派生从"名称正则"改为**显式判定表** `taxonomy.HFSS_VERDICTS` —— 正则在新名称上
>    产生 2 个假阳性（`[7]` 被"咸味"误命中、`[29]` 被"甜味"误命中，而后者名称里那是**否定**语义）
>    与 1 个假阴性（`[13]` 油炸方便面被漏掉）。
> 2. prompt 文本块 token 从 746 涨到 **1038**（+39%），仍在 2000 预算内。
> 3. `_rule_based` 的饮料分支从已废弃的 `(11,18)` 对重挂到现行的 `(18,25)/(25,29)` 对；
>    **阈值一个没动**，只是旧分支在新语义下会得出"糖高 → 瓶装水"的反向结论。
>
> ---
>
> 以下为原始记录（生成于 Day 3，对照 v0.9-draft）。
> 当时的判断是：父子归属、混淆对、证据维度三项已一致，需核对的只有类别名称/语义。

## 结论先说（原文）

代码已按指令**以 taxonomy.json 为唯一事实来源**（`services/taxonomy.py` 直接加载，不另造数据）。
但下面 13 条是**语义级冲突**，不是措辞差异 —— 接真实 VLM 出指标前必须逐条核准，
否则分类器学到的是一套和金标不同的语义，eval 数字会全面失真。

## A 类 · 语义完全不同（7 条，最高优先级）

| code | taxonomy.json | 原 codebook | 后果 |
|---|---|---|---|
| **9** | 糖果与巧克力 | Healthy snacks based on core foods | 健康零食被判成糖果 |
| **11** | 含糖饮料 | Bottled water | **HFSS 判定整体翻转**：瓶装水会被计入高糖 |
| **21** | 坚果与籽类零食 | Chocolate and candy | 巧克力被判成坚果 |
| **26** | 乳基饮料与奶茶 | Alcohol | 酒精广告被判成奶茶 |
| **29** | 酒精饮料 | Tea and coffee | 茶咖广告被判成酒精 |
| **30** | 乳制品替代品 | Baby and toddler milk formulae | 婴配奶粉被判成植物奶 |
| **24** | 汤、沙拉与其他预制食品 | Other high fat/salt products（酱料、肉酱） | 高脂高盐酱料被判成沙拉 |

## B 类 · 语义相反（3 条）

| code | taxonomy.json | 原 codebook |
|---|---|---|
| **4** | 加工果蔬（含添加剂） | Vegetables **without** additives, plain seaweed |
| **18** | 无糖/低糖饮料 | Fruit juice/drinks |
| **25** | 100% 纯果汁 | **Sugar sweetened** drinks |

B 类最危险的地方在于：`confusable_with` 声称 11/18、11/25 是混淆对，
在 json 的语义下（含糖 vs 无糖、含糖 vs 纯果汁）成立；
在 codebook 的语义下（瓶装水 vs 果汁饮料、瓶装水 vs 含糖饮料）也成立 ——
**混淆对本身对不出错，所以这类冲突不会被现有校验发现**，只能人工核。

## C 类 · 编号互换（1 组）

| code | taxonomy.json | 原 codebook |
|---|---|---|
| **31** | 快餐（常规餐品） | Fast food (**healthier** options) |
| **32** | 快餐（健康选项/轻食） | Fast food (**general**, includes unhealthy) |

31/32 在两份定义里正好对调。json 的 32 保留了 "22 已并入本类" 的 merge_note，
而 codebook 里被合并进 32 的是 "general/unhealthy" —— **merge_note 支持 codebook 的版本**，
这条基本可以判定为 json 写反了。

## D 类 · 范围出入（3 条，影响较小但会拉低准确率）

| code | taxonomy.json | 原 codebook |
|---|---|---|
| 1 | 全谷物与原味淀粉制品 | 还包含原味面包/米/面条与素饼干 |
| 3 | 无添加果蔬（果+蔬合并） | 仅水果（蔬菜在 4） |
| 13 | 精制谷物零食化制品 | Flavoured/fried instant rice and noodle |
| 14 | 其他淀粉类主食 | 甜面包、蛋糕、甜饼干、派酥（属高糖高脂） |

14 尤其要注意：codebook 里它是**高糖高脂**类目，json 里却是中性的"其他淀粉主食"，
`hfss_codes()` 的推导结果会因此少算一类。

## 代码里已经做了的防护（原文）

1. `services/taxonomy.py` 加载时对 `confirmed=false` 发一次 warning，
   `/api/health` 暴露 `taxonomy.confirmed_ratio`（当时 0.0，**现为 1.0**）。
2. `hfss_codes()` 从 taxonomy.json 的名称语义推导而非硬编码列表。
   → **此条已被推翻**：v1.0 替换后人工核对发现名称正则读不懂否定与语义，
   已改为显式判定表，见本文顶部说明。
3. `adjudicate` 的规则兜底 `_rule_based` 只处理 json 声明的 `confusing_pairs`，
   且 adapter 打 `rule-fallback`，被 `eval.runner` 的断言位拦住，出不了指标。

## 核准动作（原计划 → 实际执行情况）

| 原计划 | 状态 |
|---|---|
| 用原 codebook 逐条核对 33 个名称，改 taxonomy.json | ✅ 已完成（外部重写为 v1.0-codebook） |
| 每核准一条把 `confirmed` 改 true，`confirmed_ratio` → 1.0 | ✅ 33/33，`/api/health` 已显示 1.0 |
| 重跑 pytest 盯住 HFSS 集合变化 | ✅ 变化被发现，且证明了正则方案不可靠 |
| `meta.version` 升到 1.0，进批次报告的 `taxonomy_version` | ✅ 报告已带 `taxonomy 1.0-codebook` |
