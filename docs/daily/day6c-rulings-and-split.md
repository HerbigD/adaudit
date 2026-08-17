# Day 6（续二）· 三条裁决执行 + OPEN-RISK-02 修复 + 分层切分跑数

> 输入：裁决① 混淆对两档制 / 裁决② eval 只报描述性切片 / OPEN-RISK-02 必修 / 解锁分层切分。
> 194 tests passed（此前 184，新增 10）｜切分三集已落盘。
> **先看 §1** —— 那是今天唯一一件会花你钱的事。

---

## §1 OPEN-RISK-02 · 测试在打真实 API（已修，实测比评审说的更严重）

### 复现结果

评审说"10 个测试会打真实 API"。我按 `.env = APP_ENV=dev + VLM_PROVIDER=qwen`
复现并在 `httpx` 上挂了探针，实际数字是：

```
一轮 pytest → 28 次真实 POST https://dashscope.aliyuncs.com/.../chat/completions
```

**你本机的 `.env` 现在正是这个配置**（我核对过，时间戳 12:15，我没动过它）：

```
APP_ENV=dev
VLM_PROVIDER=qwen
LLM_PROVIDER=qwen
SEARCH_PROVIDER=dashscope
```

也就是说，在修掉之前，你在本机跑一次 `pytest` 就会烧掉 28 次调用的 token。

### 危害不止烧钱

- 测试红绿开始取决于**网络和账户余额** —— 结果不再可复现
- 熔断 ledger（`data/usage.json`）被测试数据污染，累计数不再可信，
  而它是成本熔断的**唯一事实来源**
- "CI 没 key 就红、本地有 key 就绿"是最难查的一类不可复现

### 修法：两层，不是一层

**第一层 · settings 按死。**
`tests/conftest.py` 在任何测试模块 import 之前（conftest 由 pytest 最先加载）
把 `app_env / vlm_provider / llm_provider / search_provider` 四个全部设成 mock，
和存储隔离同一位置、同一理由。三把 API key 一并抹成 `None` ——
provider 万一被某个用例改回 qwen，应当**立刻失败**而不是安静地把请求发出去。

**第二层 · 出站熔断。**
只改 settings 是"约定"不是"保证"：新写的代码只要绕过 `settings` 直接建客户端，
洞就又开了。所以再补一层 `httpx` 补丁 —— 测试期间任何真实出站请求**直接抛异常**，
异常信息里直接告诉你该怎么办。要真打 API 的用例显式标 `@pytest.mark.realapi`，
用 `pytest --realapi` 单独开门。

我特意把第二层做成正面用例（`test_real_outbound_request_is_blocked`）：
熔断本身也会失效，得有东西盯着它。

### 验收（按你给的口径）

`.env` 保持 `dev + qwen`，跑整轮：

```
194 passed
出站请求：0 次
usage.json：{'calls': 0, 'tokens_in': 0, 'tokens_out': 0}
```

`tests/test_provider_isolation.py` 把这条钉死了，其中一条用例专门断言
"`.env` 是危险配置时隔离确实起作用了"——`.env` 本来就是 mock 时它自动跳过，
所以它只在真正有风险的环境里说话。

---

## §2 裁决① · 混淆对三档制

### 落地

| 档 | source | 来源 | 对数 | 进 prompt | 进消融 B 臂 |
|---|---|---|---|---|---|
| Tier 1 | `definitional` | `_derive_pairs()` 自动推导，共享数值切分线 | **5** | ✅ | ✅ |
| Tier 2 | `definitional_compositional` | `taxonomy.json` 显式登记，判据是组成/形态 | **8** | ✅ | ❌ |
| Tier 3 | `dev_error_analysis` | 只能经 `register_empirical_pair()` 从 dev 注入 | 0 | 仅 C 臂 | ❌ |

Tier 1 锁定不变：`(2,12) (3,18) (5,19) (7,24) (8,23)`，`(8,24)` 维持排除。

### 数据侧的 9 对里，(35,36) 无法登记

```
✅ (1,13) (6,15) (4,17) (7,27) (14,16) (31,32) (33,34) (11,25)
❌ (35,36)  ← 两个编号在本 taxonomy 里都不存在（我们只到 34）
```

跳过时打 warning 并在 `taxonomy.json` 的 `compositional_pairs._dropped` 里留痕，
**不静默丢**。它的去留取决于 A4（35/36/37 补不补），还没定。

`(7,27)` 我加了一条备注：Annex 4 的 27（Recipe additions，含 oils）与 7（Oils）
在 "oils" 上定义重叠 —— 这是**来源文件自身的冲突**，阈值层面无解。

### 消融从三臂拆成四臂

| arm | 内容 | 对比 | 回答 |
|---|---|---|---|
| A | 无 | | |
| B | Tier 1 | **B − A** | 阈值级先验值多少 |
| B2 | + Tier 2 | **B2 − B** | 组成级先验再加多少 |
| C | + Tier 3 | **C − B2** | 经验先验再加多少 |

**Tier 2 必须单独占一臂**，不能并进 B：并进去的话 B→C 的差值会把
"组成级先验"和"经验先验"搅在一起，谁也说不清是哪个在起作用。

指标输出里 `confusing_pairs_by_tier` 按档分开，不混成一张表 ——
"混淆性是定义的推论"这句话在两档上的强度并不一样，混着报会把这个差别抹掉。

### token 代价

`classify_prompt` 1648 → **1727**、`adjudicate_prompt` 1225 → **1304**（每对约 10 token）。
仍远低于 2000 预算。基线已更新。

---

## §3 裁决② · 切片只描述，不对比

`metrics.summarize()` 里**删掉了 `slice_gap`**（原本会报 best/worst/gap）。
理由就是你给的口径：n≈22–25 的层上比较准确率，差异几乎全落在抽样噪声里，
报出来只会被读成"模型在印度更差"这种因果结论。

改动：

- 每层字段从 `reliable`（会被读成"这层可以拿去比"）改成 `small_sample`（只陈述样本量）
- summary 固定带 `slice_interpretation: "descriptive_only"` 与一句 `slice_limitation`
- 新增用例 `test_summary_never_emits_a_cross_slice_comparison` ——
  即使造一个"en 100% / bn 50%"的极端场景，summary 里也不许出现 gap/best/worst 字段

limitation 原文（已进代码，报告与 README 直接引用）：

> 切片指标为描述性，不构成跨组对比结论。四国样本量不均衡（Sri Lanka 约 74%，
> 其余三国合计约 26%）反映的是广告投放现实；India / Pakistan 层 n 在 20–30 量级，
> 仅供描述，跨国显著性检验留作未来工作。

eval 维持 300，不过采样、不扩量。

---

## §4 解锁 · 金标池与分层切分

### 你要的那个 zip 不存在，但不需要它

`adaudit_decisions_bundle.zip` 我在项目附件、你的 `adaudit` 文件夹、`Downloads`
里都找过 —— 都没有。它是另一个会话产出的，只在那个对话里。

**但源头在你机器上**：`~/imperial_foodad/human label/combined_file_allcountries_final.xlsx`。
我直接从它重建，结果与数据侧记录**逐个数字吻合**：

| | 数据侧记录 | 我独立重建 |
|---|---|---|
| Products 唯一 image_number | — | 7,001 |
| 与图库交集 | 6,314 | **6,314** ✅ |
| 单标签 / 多标签 | 4,942 / 1,372 | **4,942 / 1,372** ✅ |
| gold ∈ {35,36,38} | 27 | **27**（38 号 16、36 号 7、35 号 4）✅ |
| gold = 22（需 22→32 合并） | 80 | **80** ✅ |
| 池国家分布 | LK 74.2 / BD 10.0 / IN 8.5 / PK 7.3 | **LK 3645 / BD 492 / IN 417 / PK 361** ✅ |

两条独立路径得到同一组数字，这份池子可以信。

产物落 `api/data/splits/`（进 git）：
`pool_4942.csv`（带 `gold_code_raw` + `gold_specific` 两列）、`unrepresentable_gold.csv`、
`dev.csv` / `eval.csv` / `smoke.csv` / `manifest.json`。

⚠️ 指标一律用 **`gold_specific`** 列（已应用 22→32）。用 `gold_code_raw` 会让那 80 张全判错。

### 切分摘要（seed = 20260731，按 country × 粗语言桶分层）

| | n | LK | BD | IN | PK | en_only | mixed | local_only | na | 覆盖类别 |
|---|---|---|---|---|---|---|---|---|---|---|
| **smoke** | 12 | 6 | 3 | 2 | 1 | 6 | 2 | 2 | 2 | 7 |
| **dev** | 200 | 148 | 20 | 17 | 15 | 52 | 110 | 12 | 26 | 19 |
| **eval** | 300 | 223 | 30 | 25 | 22 | 79 | 165 | 18 | 38 | 26 |
| 池（4,915） | 4915 | 3645 | 492 | 417 | 361 | 1284 | 2701 | 303 | 627 | 33 |

三集互斥（有断言），同种子可复现（有用例），扩容稳定（dev 重叠 93% / eval 88%）。

**语言用粗桶**（`en_only` / `local_only` / `mixed` / `na`）：标注里的语言是自由文本，
`"sinhala english"` / `"Sinhala English"` / `"Not Applicable (NA)"` 大小写与组合都不统一，
只能归桶，不能当 ISO 码用。我把 `split.py` 原来按 5 字符截断的逻辑删了 ——
那会把 `en_only` 截成 `en_on`，分层直接错。

**eval 只覆盖 33 类中的 26 类**（dev 19 类）。per-class accuracy 在 eval 上不可报，
这条和裁决② 的口径一致，已写进 limitation。

### 消融子集：能凑够 90，但分布很偏

加入 Tier 2 后 dev 里落在混淆对上的样本从 22 涨到 **141**，90 条凑得出来。
但分档看：

```
Tier 1（definitional）           22 条
Tier 2（compositional）         119 条
```

按比例随机抽 90 条，Tier 1 只会占约 14 条 —— 而 **B−A 恰恰只由 Tier 1 驱动**
（Tier 2 样本在 A 与 B 之间 prompt 完全相同，纯粹是噪声）。
整段实验里最值钱的那个数会被稀释到看不见。

所以 `build_subset` 改成**按档配额**：先取满 Tier 1（22），再用 Tier 2 补到 90。
每档实际条数写进 summary 的 `subset_composition`，caveats 里明写
"每个对比只由它的自变量真正变化的那一档驱动，读 DiD 要用那一档的 n，不是 90"。

**两个遗留问题见 §5。**

---

## §5 需要你决定的两件事（都由 §4 的实测暴露出来）

### 5.1 Tier 1 只有 22 条 —— B−A 这个对比几乎做不出结论

22 条的样本量下，只有 20 个百分点以上的效应才看得出来。
而 B−A 正是你论文里"置信度是模型内生的还是我们喂的"那一问的直接答案。

三个选项：

1. **接受，把 B−A 报成"未检出显著差异"** —— 诚实，但那一问就没答上
2. **定向扩充 Tier 1 子集** —— 全池有 523 条落在 Tier 1 对上，从
   `池 − eval − smoke` 里再抽到 90（Tier 1 独占），与 eval 严格互斥。
   代价：这批样本不属于 dev，需要在 Methods 里单列为"ablation set"
3. **只做 B2−B 与 C−B2，放弃 B−A** —— 不推荐，等于放弃最关键的那个对比

**我倾向 2。**

### 5.2 对照组只凑到 48 条（要 90）

Tier 2 进来后 dev 200 里有 141 条是"混淆样本"，非混淆的只剩 59 条，
按国家×语言配对后只能凑出 48 条对照。对照组是差分的分母之一，
48 vs 90 会让 DiD 的置信区间明显变宽。

同样的解法：对照组也从 `池 − eval − smoke` 里配对抽取。
**要不要这么做，和 5.1 是同一个决定** —— 都是"消融集是否可以超出 dev 的范围"。

---

## §6 变更清单

**新增**

- `api/tests/test_provider_isolation.py`（8 例）—— OPEN-RISK-02 回归

**修改**

- `api/tests/conftest.py` —— provider 强制 mock + key 抹除 + 出站熔断 + `realapi` marker
- `api/data/taxonomy.json` —— 新增 `compositional_pairs`（8 对 + `_dropped` 留痕）
- `api/services/taxonomy.py` —— `_load_compositional()`、`ARM_TIERS` 四臂、`pairs_by_tier()`、Tier 2 免除维度告警
- `api/eval/metrics.py` —— 删 `slice_gap`、`reliable`→`small_sample`、`slice_limitation`、`confusing_pairs_by_tier`
- `api/eval/ablation.py` —— 四臂、`tier_of()`、按档配额、`subset_composition`
- `api/eval/runner.py` —— 四臂、传 composition
- `api/eval/split.py` —— 语言列不再截断（粗桶会被截坏）
- `api/config.py` —— `pairs_arm` 默认 `B` → `B2`
- `api/tests/test_taxonomy.py` / `test_eval_slices_and_ablation.py` —— 分档与描述性口径

**数据**

- `api/data/splits/` —— `pool_4942.csv`、`unrepresentable_gold.csv`、`dev.csv`、`eval.csv`、`smoke.csv`、`manifest.json`

**测试**：194 passed（此前 184）。`.env = dev+qwen` 下整轮全绿、零真实请求、ledger 零累计。

---

## §7 下一步

1. §5 两件事定了 → 我调整消融子集来源，然后你在本机跑
   `python -m eval.runner --ablation --split dev`
2. A4（35/36/37 补不补）还没定 —— 它同时卡住 `(35,36)` 这对 Tier 2
3. Day 12 的 D1：**非对称覆盖只是半个方案**，Amul Toned → Double Toned 仍会误命中
