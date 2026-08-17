# Day 6（续）· 人类六项决策执行报告

> 输入：A1 单标签金标 / A2 dev 200+eval 300 分层切分 / A3 消融批准 /
> B1+B2+B3 Annex 4 权威阈值 / B5 成本估算保持下限 / D3 语言国家切片，
> 外加 D1 的 Day 12 补充决议。
>
> 本文只写**做了什么、为什么这么做、哪里和你的预期不一样**。
> 需要你拍板的两件事在 §7，请先看那一节。

---

## §0 一句话总结

六项全部落地，测试 184 全绿（新增 51 个用例）。
过程中**推翻了我自己昨天写的一条推导规则**（§2），
**修掉了两个我自己刚写进去的 Annex 4 适用范围错误**（§1.5，是数据侧的独立核对逼出来的），
并修掉了一个会让测试"整轮绿、单跑红"的隔离缺陷（§6）。
需要你决策的事在 §7。

---

## §1 B1+B2+B3 · Annex 4 权威阈值接线

### 做了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `data/taxonomy.json` | 只有中文品类描述，数值判据丢失 | 11 个 code 带 `thresholds`（Annex 4 逐字英文 + 机器可判规则），版本 `1.1-annex4` |
| `services/nutrient_rules.py` | 不存在 | 新建。判定引擎，**代码里不写任何数字**，全部从 taxonomy 读 |
| `adjudicate._rule_based` | 我猜的占位值（糖 15 / 脂 3 / 脂 10·盐 1.2 / 糖 2.5） | 调 `nutrient_rules.decide()` |
| 钠→盐 `× 2.5` | 有 | **已删除**。有个测试直接对裁决节点源码断言 `"2.5" not in src` |
| per-serve 口径 | 不存在，全链路只有 per-100g | `NutrientValue` 新增 `basis` / `per_serve` / `serving_size_g` |
| 抽取的营养素 | 5 个 | 7 个（补 `saturated_fat`、`energy_kj`） |

### 三条值得单独说的执行细节

**① 份量不明就转人工，不许拿 per-100g 顶替。**
Annex 4 判 8/23（餐食）和 9（健康零食）用的是 **per serve**。
拿 per-100g 去比 "900mg /serve" 是把两个不同分母的数字放在一起比 ——
链路会一路绿灯跑完，产出的却是无意义的判定。
所以 `_meals` 拿不到份量就返回 `uncertain`，由裁决节点导向人工。
这条最容易被"顺手兜底"破坏，我给它单独立了一个用例。

**② 判不了就交出去，不回落到 `pool[0]`。**
旧的 `_rule_based` 在证据不够时会拿候选里的第一个当答案。
那是把"判不了"伪装成"判出来了"，而且伪装得没有痕迹 ——
下游看到一个正常的叶子编号，没人会再去问它是怎么来的。
现在这种情况 `specific_code=None`、按父类输出、置信 0.40、转人工。

**③ 抽饱和脂肪必须排掉总脂肪的正则。**
`fat` 的模式加了 `(?<!saturated\s)(?<!trans\s)` 前瞻，
否则 "saturated fat 6g" 会被当成总脂肪读走，8/23 直接判错。

### 边界值：这是本项目补充规则，不是 Annex 4 原文

Annex 4 的 2 是 `<20g 糖 且 >5g 纤维`，12 是 `>20g 糖 或 <5g 纤维`。
**恰好等于 20 或 5 时两边都不成立** —— 原文留下的定义缝隙。

统一规则：**边界值归非健康类（12）**，对 HFSS 监管口径保守。
判定理由字符串里会明写"按本项目补充规则"，
`taxonomy.json` 的 23 号也带 `project_note` 记录 OR 读法的来源。
读结果的人不会误以为 Annex 4 就是这么写的。

---

## §1.5 与数据侧核对记录的对账 —— 抓到我两个真 bug

项目知识库里有一份**独立完成的** Annex 4 逐字核对
（`claude/AdAudit_六项决策执行_Annex4核对.md`，数据侧执行、代码侧未执行）。
我把它和我刚写完的代码逐条对了一遍，**它抓到我两个已经写进代码的错误**。
两个都已修复并补了回归用例。

### Bug 1（严重）· code 7 的 10g 脂肪线不适用于食用油

Annex 4 原文：

> **7.** Oils high in mono- or polyunsaturated fats, (olive oil, …),
> **and** low fat savoury sauces (**<10g fat /100g**)

`<10g/100g` **只修饰 savoury sauces 那一支**。
我把它当成整个 code 7 的判据写进了 `_sauces`，于是：

> **橄榄油脂肪 ~91–100g/100g → 我的规则把它判进 24（other high fat/salt products）。**

而 Annex 4 明确把植物油放在 7。油脂进 7、butter/animal fats 进 24
是**定义之分，不是阈值之分**。

这是"数值抄对了、适用范围抄丢了"——比抄错数字更难发现，因为所有阈值测试都是绿的。

**修法**：`decide()` 增加 `is_sauce` 形态信号。判不出形态（`None`）或明确不是酱（`False`）
一律 `uncertain` 转人工，**不套阈值**。`sauce_form()` 从品名判断，
**判不出时返回 `None` 而不是 `True`** —— 默认成"是酱"等于没修。
适用范围约束同时写进 `taxonomy.json` 的 `threshold_scope` / `scope_warning`，
有测试断言它必须留在数据源里。

### Bug 2 · 边界缝隙有 3 处，我只处理了 1 处

我只做了 2/12 的糖 20 / 纤维 5。另外两处同样是缝隙：

| 缝隙 | 我原来的行为 | 修正后 |
|---|---|---|
| savoury sauce fat **= 10** | → 7（健康侧）❌ | → 24 ✅ |
| soup fat **= 2** | 完全没实现汤规则 | → 24 ✅ |

原来的 `_sauces` 写的是 `fat > 10 → 24`，所以恰好 10 时落到 7 ——
**和我自己在 2/12 上定的"边界归非健康类"规则自相矛盾**。
5/19（≤3/>3、≤15/>15）与 3/18（≥98/<98）确实无缝隙，不用处理。

顺带补上了汤的判据：Annex 4 的 8 含 `soups (<2g fat/100g, exclude dehydrated)`、
24 含 `soups (>2g fat/100g and all dehydrated)`。汤必须在 8/23 的餐食分支**之前**拦下 ——
餐食按 per-serve 判，汤按 per-100g 判，走错分支会让一条本来可用的判据变成"份量不明转人工"。

### 已采纳的其余几条

- 5/19 补 `definitional_includes`（`alternatives e.g. Soy` / `probiotic drinks`）——
  南亚场景下豆奶、Yakult 型益生菌饮料据此进 5/19 而非 25
- `meta.known_gaps` 改写：Annex 4 是 **1–37**，数据集 GT 的 38 号 `Other` 是数据集自加
- `meta.annex4_transcription_notes` 记下四处转录修正，可查
- 钠换算系数 ×400 已互相印证正确（= ×1000 ÷ 2.5）

### 需要你裁决的三处口径分歧

我和那份记录在三件事上结论不同。都不是谁抄错了，是**判据的取舍不同**：

**① 混淆对：我推出 5 对，那边推出 17 对（其中 nutrient_threshold 8 对）。**
分歧点是 `(8,24)` 和 `(9,16)`/`(9,17)`。
我把 (8,24) 判为假对，理由是 8 按 **per serve** 的饱和脂肪+钠判、24 按 **per 100g**
的总脂肪判，**没有共享的判定线**；那边按"同为脂/盐维度"收进来。
我倾向坚持自己的判据（共享切分线），因为 A3 的整个论证依赖"混淆性是定义的推论"——
维度名相同但分母不同，推不出"视觉不可区分"。
**但这是可以争的，你定。**

**② 语言分层粒度**：那边用粗桶（`en_only` / `local_only` / `mixed` / `na`），
我的 `split.py` 原来假设是 ISO 码并截断到 5 字符 —— 会把 `en_only` 截成 `en_on`。
**已改为不截断**，两种粒度都能用。

**③ 池子分布**：那份记录里是 **Sri Lanka 74.2% / Bangladesh 10.0% / India 8.5% /
Pakistan 7.3%**，和我此前假设的"印度占大头"完全相反。
这直接影响 D3 切片的可用性：按这个分布，300 条 eval 里
India ≈ 25 张、Pakistan ≈ 22 张 —— **低于我设的 `MIN_SLICE_N = 20` 边缘**，
`reliable` 会勉强为真但置信区间很宽。
建议：要么把 eval 扩到 500，要么 D3 只报 Sri Lanka + Bangladesh 两层、
其余两国明确标"n 过小，仅供参考"。**这条需要你选。**

### 那份记录里我还没执行的部分

它提到一个已生成的产物 `adaudit_decisions_bundle.zip`
（含 `splits/`、`annex4_thresholds.json`、`confusing_pairs_derived.json`、
`split_config_snippet.py`、`pool_4942.csv`、`unrepresentable_gold.csv`）。
**我这边拿不到这个 zip**。如果你手上有，给我路径，我用里面的 `pool_4942.csv`
直接跑切分 —— 那样就不用你另外导一份（见 §7.3）。

它还指出两件我尚未处理的事，已登记进 `OPEN-QUESTIONS.md`：

- **22→32 合并会改动 80 张图的 gold**（占单标签池 1.6%）。
  指标必须用 `gold_code_taxonomy` 而不是 `gold_code_raw`，否则这 80 张全判错。
- **OPEN-RISK-01 的方案 2 只修了一半**：非对称覆盖对
  `archive="Amul Toned"` / `query="Amul Double Toned Milk"` **仍会误命中**
  （archive 的 token 是 query 的子集）。Day 12 修的时候要按那份记录的建议加一条：
  token 差集若含 `double / toned / full cream / skimmed` 这类关键维度词则判不命中。
  **回归用例要把这一条写成"期望不命中"**，否则 Day 12 会照着半个方案写出绿灯。

---

## §2 A3 · 混淆对自动推导 —— 我推翻了自己昨天的规则

### 昨天的规则错了

昨天我写的判据是：**同父类 + `key_dimensions` 相同 + 双方都有 `thresholds`**。
今天把 Annex 4 阈值填进去以后，它当场推出了两个假对、漏掉一个真对：

| 对 | 旧规则 | 事实 |
|---|---|---|
| (8, 24) | ✅ 推出 | ❌ 假对。8 按 **per serve** 的饱和脂肪+钠判，24 按 **per 100g** 的总脂肪判，没有共享判定线 |
| (23, 24) | ✅ 推出 | ❌ 假对。同上。23 与 24 之分是产品形态（餐食 vs 酱料/汤），不是阈值 |
| (7, 24) | ❌ 漏掉 | ✅ 真对。低脂咸味酱 `<10g fat/100g` vs 高脂咸味酱 `>10g fat/100g`，**共用同一条 10g 线** |

(7,24) 被漏掉的原因是它们分属父类 8 与 6。
Annex 4 的父类分组是按"健康/不健康"编排的，**不是按视觉相似度**，
拿它当混淆对的判据从一开始就是错的。

### 新规则

> 两类在**同一营养素、同一 basis 上给出方向相反、切分点相同**的阈值 → definitional 混淆对。

推导结果：`(2,12) (3,18) (5,19) (7,24) (8,23)` ——
和 `nutrient_rules._PAIR_RULES` 天然对齐，一条不多一条不少。

### 副作用：三对旧的手工对消失了，这是对的

`(16,17)` `(18,25)` `(25,29)` 不再出现在 prompt 里。
原因不是规则太严，是 **Annex 4 对它们根本没给数值切分点**：

- (16,17) 甜零食 vs 咸零食 —— 靠品类形态，不靠营养表
- (18,25) 果汁饮料 vs 含糖饮料 —— 靠果汁百分比（在**配料表**，不在营养表）
- (25,29) 含糖饮料 vs 茶咖 —— 靠"是否加糖"，同上

它们要进 prompt 必须走 `register_empirical_pair()`，
标 `source=dev_error_analysis`，且**只准来自 dev split**。这正是 A3 想要的制度。

⚠️ **对搜索取证的影响**：(18,25)/(25,29) 退出后，饮料类的跨源冲突判定失去了目标维度。
这是 Day 5 冲突判定覆盖面的一次实质收窄。我认为收窄是对的（原来那两对的
"判定维度=糖"本来就不是 Annex 4 的判据），但你如果在真实跑批里看到饮料类
冲突漏检变多，来源就是这里。

### 顺带修掉一个静默失效

旧的手抄表 `PAIR_NUTRIENTS` 里 (8,23) 写的是 `("fat", "sodium")`，
而 Annex 4 用的是 **saturated fat**。
于是 Day 5 的冲突判定一直盯着一个 Annex 4 根本没用到的维度看 ——
表面有覆盖，实际静默失效。现在维度由阈值自动推出，手抄不了。

### 三臂消融已就位

```bash
python -m eval.runner --ablation --split dev
```

- **arm A** 不给任何混淆对 · **arm B** 只给 definitional · **arm C** 再加 dev 经验对
- 配套**匹配对照组**：按混淆子集的国家×语言分布抽等量的非混淆样本
- 输出核心是 `difference_in_differences = Δ混淆 − Δ对照`

对照组不是可选项。只看混淆样本会得出一个假结论：
加提示 → 混淆样本准确率上升 → 有效。但提示也可能只是把模型整体推向"多报低置信"，
于是它在非混淆样本上也开始犹豫 —— 那不是判别力变强，是阈值整体漂移。
差分把这一层剥掉。我给这个逻辑写了两个用例（同涨 10pp → 差分为 0；
只有混淆组涨 → 差分 0.15）。

`--ablation` 的输出永远带一句 caveat：**90 条上小于约 10 个百分点的差异不可解读**。

---

## §3 A2 · 数据切分

`api/eval/split.py`，`python -m eval.split --pool <金标池.csv>`。

| 项 | 值 |
|---|---|
| 种子 | `config.SPLIT_SEED = 20260731`（写在 config，不是命令行默认值） |
| 分层 | country × language |
| dev / eval / smoke | 200 / 300 / 12，三者互斥（有断言） |
| 落盘 | `api/data/splits/{dev,eval,smoke}.csv` + `manifest.json`，进 git |

### 三个设计决定

**① 排序用 `sha256(seed, id)`，不用 `random.shuffle`。**
shuffle 的输出取决于列表长度，金标池加一条样本就会把整个序列重排，
上一轮的指标全部作废。哈希排序下扩容是稳定的 ——
实测 IN 加 400 条后，dev 重叠 93%、eval 重叠 88%。

**② 配额用最大余数法。**
逐层 `round()` 会让总数飘，飘出来的差额只能从最后一层硬砍，
等于让排序最末的那一层承担全部误差。

**③ smoke 走覆盖优先，不按比例。**
按比例切 12 条会几乎全给印度（占 65%），
而冒烟集的用途恰恰是"五语种 OCR 各来一张"。
现在它保证四国全覆盖、语言 ≥5 种。

### 跑批纪律已进代码

`eval.runner` 不给 `--split` 时会打一条醒目警告并写进结果的 `warnings`：
那条路径读 `eval_samples` 全表，**没有 dev/held-out 隔离**。
每条 `Prediction` 与最终 summary 都带 `split` 与 `pairs_arm` 字段 ——
任何一份指标都答得出"这是在哪份切分、哪个 prompt 条件下跑的"。

**你需要做的**：把单标签金标池导出成 csv 给我路径（或直接跑 `--pool`）。
列名不敏感，`id/image_path/gold_specific/country/ad_language` 这几个词的常见别名都认。
多标签行（`gold_specific` 里有 `;` `,` `|` `/`）会被显式跳过并打印条数，不静默丢。

---

## §4 D3 · 语言 / 国家切片

`metrics.summarize()` 新增 `by_language` / `by_country` / `by_search_status`
以及 `language_gap` / `country_gap`。

两个不显然的处理：

**① 优先用金标语言，不用模型判读的语言。**
用模型自己判的语言分层，等于让模型给自己划考区：
它把孟加拉语广告判成英语的那些样本会被算进 en 层 ——
en 的准确率被这条错误拉低、bn 层则凭空少了一条，两个层同时失真。
所以 `Prediction` 分开存 `gold_language`/`gold_country` 与模型判读的
`language`/`country`，切片优先用金标。金标没有语言列时才退回，
且 summary 里 `slice_key` 会如实写成 `model_predicted`。

**② 每层带 `reliable` 标记（n ≥ 20）。**
5 条样本上的 0.80 是 4/5，不是能写进表格的数。
`slice_gap` 只在可靠层之间算差距 —— 否则 5 条样本能造出任意大的"差距"。

---

## §5 B5 · 成本估算

按你的决定保持在最低档（`$0.32/$1.28 per Mtok`），config 里已注明**估算值是下限**。
token 熔断不受影响 —— 那个用的是 provider 返回的真实数。

---

## §6 顺手修掉的一个测试隔离缺陷

跑测试时发现 `test_conflict_path_goes_human_and_supersedes_cache`
**整轮跑绿、单独跑红**。

查下来是测试直接写生产库 `data/adaudit.db`：
该用例自己会 upsert 一条 `ConflictBrand` 档案，跑完留在库里；
再单跑时 `cache_lookup` 命中了上一轮的残留，`search_status` 从 `conflict`
变成 `cache`，`route_2` 就成了 `direct_verified`。

**测试失败，但代码没错。** 而反过来更危险：
一个真实的缓存逻辑回归，可能因为库里恰好有条旧档案而被掩盖成绿色。

新增 `tests/conftest.py`：库文件、checkpoint、chroma、usage ledger
全部按 pytest 会话建在 tmp 目录，跑完即弃。
单文件连跑三次已稳定通过。

---

## §7 ⚠️ 需要你拍板的两件事（都动数据源）

### 7.1 taxonomy 覆盖不了 GT 的 35/36/38 —— 27 张图无法表达

数据侧记录已经把这件事查清了，不用再猜：

```
Annex 4 定义:        1–37
我们 taxonomy:       1–34（22 并入 32）= 33 类
数据集 GT:           1–38（38 号 Other 是数据集自加，Annex 4 里没有）
单标签池中 gold ∈ {35, 36, 38}: 27 张   ← 预测空间无法表达
（37 号 0 张；33 号 1 张，在范围内）
```

**27 张 = 单标签池的 0.55%。** 它们进 eval 的话必然全错，而且错得没有意义
（模型根本没有这些选项）。

四个选项：

1. **排出抽样池**（池 4,942 → 4,915），落 `unrepresentable_gold.csv` 存档 ——
   数据侧记录的默认处理。但**偏离 A1 字面**（"只从 4,942 张抽样"），所以要你点头
2. **把 35/36/37 补进 taxonomy** —— 变 36 类，所有"33 类"的断言、token 基线、
   `HFSS_VERDICTS` 覆盖校验跟改，约半天。38 号 `Other` 仍然无解
3. **补进去但标 `out_of_scope=true`** —— 结构完整、不进 prompt、不参与指标
4. **留在池里，按错算** —— 诚实但没信息量，还会让 exact_match 平白低 0.55%

**我倾向 1**：27 张、0.55%，排出去并在 Methods 里写明白，比为它们改动整个
预测空间划算。但**这偏离你 A1 的字面表述，所以必须你说了算。**

### 7.2 五处定义细节我补进了 `description_zh`，需要你确认

对照 Annex 4 原文发现 taxonomy 丢了五处限定语，我已补上（只动中文描述，不动名称与编号）：

| code | 补的内容 | Annex 4 原文依据 |
|---|---|---|
| 1 | 面条**不含油炸**（fried noodles 归别处） | "noodles (exclude fried)" |
| 3 | 果汁含量 **≥98%** 才算 3 | "include fruit juices containing ≥98% fruit" |
| 5 | 含**益生菌饮品** | "(include probiotic drinks)" |
| 21 | **排除**无糖口香糖 | 28 的括号注释 |
| 28 | 含**无糖口香糖** | "include sugar-free chewing gum" |

这五处直接影响分类边界（比如无糖口香糖到底算 21 还是 28）。
**请确认这五条与你手上的 codebook 一致**，不一致的我改回去。

### 7.3 我需要的输入：单标签金标池 csv

切分脚本已就位但 `data/splits/` 是空的 —— **我手上没有金标数据**。
这是当前唯一卡住整条评测链的东西（A3 消融、D3 切片都等它）。

数据侧记录提到已有 `pool_4942.csv`（在 `adaudit_decisions_bundle.zip` 里，
且带 `gold_code_raw` / `gold_code_taxonomy` 两列）。
**如果你手上有这个 zip，给我路径最省事** —— 我直接用它跑，你什么都不用导。

拿不到的话，导一份 csv 也行，列名不敏感：
`id` / `image_path` / `gold_specific` / `country` / `ad_language` 的常见别名都认。

⚠️ 用 `pool_4942.csv` 时我会取 **`gold_code_taxonomy`** 那一列 ——
用 `gold_code_raw` 会让 80 张 gold=22 的图全判错（22 已并入 32）。

### 7.4 D3 切片：按真实分布，印度和巴基斯坦层都太小

按数据侧记录的池分布（Sri Lanka 74.2% / Bangladesh 10.0% / India 8.5% / Pakistan 7.3%），
300 条 eval 里 India ≈ 25、Pakistan ≈ 22 —— 刚过我设的 `MIN_SLICE_N = 20`，
`reliable` 会是真，但置信区间宽到不该拿去做跨国对比。

三个选项：

1. **eval 扩到 500** —— India ≈ 42 / Pakistan ≈ 37，勉强能用。代价是跑批成本 +67%
2. **D3 只报 Sri Lanka + Bangladesh 两层**，其余两国标"n 过小，仅供参考"
3. **对小国过采样**（分层时给 India/Pakistan 保底 40 条）——
   但这样 eval 就不再是总体的无偏估计，整体 exact_match 要单独加权还原

**我倾向 2**：最诚实，且不改动已定的 300 条规模。但如果"跨国泛化"是你论文的主要卖点，
那 1 更值。**这条要你选。**

---

## §8 变更清单

**新增**

- `api/services/nutrient_rules.py` —— Annex 4 判定引擎
- `api/eval/split.py` —— A2 分层切分
- `api/eval/ablation.py` —— A3 三臂消融 + 匹配对照
- `api/tests/conftest.py` —— 测试存储隔离
- `api/tests/test_annex4_rules.py`（33 例）
- `api/tests/test_split.py`（10 例）
- `api/tests/test_eval_slices_and_ablation.py`（8 例）

**修改**

- `api/data/taxonomy.json` —— `thresholds` ×11、5 处定义补全、7/24 加 `threshold_scope` + `boundary_rule`、5/19 加 `definitional_includes`、**删除人工 `confusing_pairs`**、`known_gaps` 与 `annex4_transcription_notes` 重写、版本 `1.1-annex4`
- `api/services/taxonomy.py` —— 推导规则重写、`PAIR_DIMS` 自动填充、`pairs_for_arm()`、cascade 输出带 `source`
- `api/services/nutrient_rules.py` —— 7/24 加适用范围守卫、边界改归 24、新增汤规则、`sauce_form()`/`soup_form()`
- `api/services/nutrition.py` —— 份量解析、per-serve 换算、能量 kJ/kcal、饱和脂肪正则、`_nutrient_value()` 统一构造
- `api/services/vlm.py` —— mock 品名与混淆对绑定（假数据自相矛盾会让测试红在错的地方）
- `api/graph/state.py` —— `Nutrient` 补两项、`Basis`、`NutrientValue.per_serve`、`Evidence.per_serve()`
- `api/graph/nodes/adjudicate_with_evidence.py` —— 接 `nutrient_rules`、删 ×2.5、传形态信号、判不了转人工
- `api/eval/{metrics,dataset,runner,split}.py` —— 切片指标、切分读取、消融臂、语言列不截断
- `api/config.py` —— `pairs_arm`、`split_*`
- `api/tests/test_taxonomy.py` —— token 基线 1286→1648 / 871→1225（涨的全是 Annex 4 阈值原文，仍远低于 2000 预算）

**测试**：184 passed（此前 133）。单文件、整轮、连跑三次均稳定。

---

## §9 下一步

1. **你给金标池**（`pool_4942.csv` 路径，或另导一份 csv）→ 我跑切分 → 三份进 git
2. §7 的四件事定了我再动 taxonomy 与切分参数
3. 你在本机跑 `--ablation --split dev`（需真实 provider；我这边永远没外网）
4. Day 12 处理 D1 —— **注意非对称覆盖只是半个方案**，见 §1.5 末尾
