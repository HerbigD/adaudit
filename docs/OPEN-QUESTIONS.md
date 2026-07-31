# 待你决策的问题清单

> 截至 Day 6（续，六项决策已执行）。按"不解决会造成什么后果"排序。
> 每条给出：问题 / 为什么必须你来定 / 我的建议 / 拖着的代价。
> 已决策的条目移到文末「已关闭」。

---

## A · 阻塞级：不先定，后面的工作会白做

### ~~A4. taxonomy 覆盖不了 GT 的 35/36/38~~ —— 已裁决：维持 33 类，27 张 parked

**现状**（数据侧核对已查实，不用再猜）：

```
Annex 4 定义:  1–37
我们 taxonomy: 1–34（22 并入 32）= 33 类
数据集 GT:     1–38（38 号 Other 是数据集自加，Annex 4 里没有）
单标签池中 gold ∈ {35, 36, 38}: 27 张 = 0.55%   ← 预测空间无法表达
（37 号 0 张；33 号 1 张，在范围内）
```

这 27 张进 eval 必然全错，且错得没有信息量 —— 模型根本没有这些选项。

**四个选项**：

1. **排出抽样池**（4,942 → 4,915），落 `unrepresentable_gold.csv` 存档。
   数据侧的默认处理，但**偏离 A1 字面**（"只从 4,942 张抽样"），要你点头
2. **把 35/36/37 补进 taxonomy** —— 变 36 类，所有"33 类"断言、token 基线、
   `HFSS_VERDICTS` 覆盖校验跟改，约半天。38 号 `Other` 仍无解
3. **补进去但标 `out_of_scope=true`** —— 结构完整、不进 prompt、不参与指标
4. **留在池里按错算** —— 诚实但没信息量，exact_match 白低 0.55%

**我的建议**：选 1。27 张、0.55%，排出去 + Methods 写明白，
比为它们改动整个预测空间划算。**但这偏离你 A1 的字面表述，必须你说了算。**

---

### ~~A7. 22→32 合并会改动 80 张图的 gold~~ —— 已裁决：按同义处理，均按 32 计

**现状**：标注员把 22 与 32 当两个码在用（全表 22 有 284 行、32 有 533 行）。
单标签池中 gold=22 **80 张**（1.6%）、gold=32 18 张。
Annex 4 里 22 与 32 文本完全重复（均为 "Fast food (not only healthier options advertised)"），
所以项目的 22→32 合并依据成立。

**风险**：GT 的 22 文本比 32 少 "soft drinks" 几个字。
若标注实践中存在隐含区分，合并会把它抹掉。

**必须做的**：指标一律用 `gold_code_taxonomy`（已应用合并）而不是 `gold_code_raw`，
否则这 80 张全判错。我在切分脚本里会取前者。

**需要你确认**：标注时 22 和 32 是不是当同一件事在用。

---

### A5. 五处定义细节我补进了 `description_zh`，需要你比对 codebook

对照 Annex 4 原文发现 taxonomy 丢了五处限定语，**我已补上**（只动中文描述，
不动名称与编号）。这五处直接影响分类边界：

| code | 补的内容 | Annex 4 原文依据 |
|---|---|---|
| 1 | 面条**不含油炸** | "noodles (exclude fried)" |
| 3 | 果汁含量 **≥98%** 才算 3 | "include fruit juices containing ≥98% fruit" |
| 5 | 含**益生菌饮品** | "(include probiotic drinks)" |
| 21 | **排除**无糖口香糖 | 28 的括号注释 |
| 28 | 含**无糖口香糖** | "include sugar-free chewing gum" |

**需要你做**：确认这五条与你手上的 codebook 一致。不一致的我改回去。

**拖着的代价**：中。无糖口香糖到底算 21 还是 28 会直接改变那批样本的对错。

---

### ~~A6. 需要金标池文件~~ —— 已解锁（见「已关闭」）

A2 的切分脚本已写好（`python -m eval.split --pool <csv>`），
**但我手上没有金标数据**，所以 `data/splits/` 是空的。
dev/eval 切不出来 → A3 消融跑不了 → D3 切片没数据。

**最省事的路**：数据侧记录提到已生成 `adaudit_decisions_bundle.zip`，
里面有 `pool_4942.csv`（带 `gold_code_raw` / `gold_code_taxonomy` 两列）。
**给我这个 zip 的路径就行**，你什么都不用另外导。

拿不到就导一份 csv，列名不敏感：
`id` / `image_path` / `gold_specific` / `country` / `ad_language` 的常见别名都认。
多标签行（`gold_specific` 里含 `;` `,` `|` `/`）会被显式跳过并打印条数。

⚠️ 用 `pool_4942.csv` 时我取 **`gold_code_taxonomy`** 列（见 A7）。

---

### ~~A8. eval 300 条下 India / Pakistan 两层太小~~ —— 已由裁决②关闭（只报描述性切片）

按数据侧核对的池分布 —— **Sri Lanka 74.2% / Bangladesh 10.0% /
India 8.5% / Pakistan 7.3%** —— 分层切出的 300 条 eval 里
India ≈ 25、Pakistan ≈ 22，刚过我设的 `MIN_SLICE_N = 20`。
`reliable` 会是真，但置信区间宽到不该拿去做跨国比较。

另有一条同源问题：**eval 300 只覆盖 33 类中的 22 类**（dev 200 覆盖 26 类），
per-class accuracy 在 eval 上不可报。

**三个选项**：

1. **eval 扩到 500** —— India ≈ 42 / Pakistan ≈ 37 勉强能用，跑批成本 +67%
2. **D3 只报 Sri Lanka + Bangladesh**，其余两国标"n 过小，仅供参考"
3. **对小国过采样**（India/Pakistan 保底 40 条）—— 但 eval 就不再是总体的无偏估计，
   整体 exact_match 得单独加权还原

**我的建议**：选 2 最诚实且不动已定的 300 条规模。
但如果"跨国泛化"是论文主要卖点，1 更值。

另：A3 的 90 张混淆对样本**必须定向抽样** —— 随机 300 张里混淆对几乎不成对出现
（`ablation.build_subset` 已按 `is_confusing_pair` 定向筛，但它只能从 dev 200 里筛，
如果 dev 里落在混淆对上的样本不足 90 条，我会打印实际条数而不是静默少跑）。

---

### A9. 消融子集能否超出 dev 的范围（两件事，一个决定）

`§4` 实测暴露的两个数：

| | 需要 | dev 实际有 | 后果 |
|---|---|---|---|
| Tier 1 混淆样本 | 越多越好 | **22** | B−A 只有 20pp 以上的效应才看得出来 |
| 对照组 | 90 | **48** | DiD 置信区间明显变宽 |

Tier 2 进来后 dev 200 里 141 条算"混淆样本"，非混淆只剩 59 条，
配对后只能凑 48 条对照。

**同一个决定**：消融集是否可以从 `池 − eval − smoke`（4,615 条）里抽，
而不是限死在 dev 200 内。

1. **可以** —— Tier 1 取满 90（全池有 523 条）、对照也取满 90，与 eval 严格互斥。
   Methods 里把它单列为 "ablation set"。**我倾向这个**
2. **不可以，就用 dev** —— B−A 大概率报"未检出显著差异"，那一问答不上
3. **只做 B2−B 与 C−B2** —— 放弃 B−A。不推荐，那是最关键的对比

**拖着的代价**：高。消融跑批要真实 provider、要花钱，跑之前得先定，
否则跑完发现样本量不够只能重跑。

---

### ~~A10. 消融集按档配额~~ —— 已批准 60/30 + 单对上限（见「已关闭」）

裁决原文"先取满 Tier 1 再用 Tier 2 补"。池里 Tier 1 有 523 条，
所以 90 条**全部**来自 Tier 1：

```
B−A   n=90  ✅ 可解读
B2−B  n=0   ❌ 做不出来 —— 没有 Tier 2 样本，B 与 B2 两臂 prompt 无实际差别
C−B2  n=0   ❌ 预期内（Tier 3 还没有经验对）
```

`C−B2` 为 0 正常。但 `B2−B` 为 0 是配额的直接后果 ——
而裁决①刚把 Tier 2 单独拆成一臂就是为了单独看它的贡献，按原文抽等于那一臂白设，
这轮 720 次调用里 B2 臂的 180 次产出不了可解读的对比。

**a. 就按原文** —— B−A 满额 90，Tier 2 留到以后单独跑
**b. 分配额** `--tier-quota "definitional=60,definitional_compositional=30"`
   → B−A n=60、B2−B n=30，两个都 ≥30 可解读；代价是 B−A 从 90 降到 60

**我倾向 b。** 60 条对 B−A 仍能看出 ~15pp 以上的效应。

另需知晓：混淆组 gold 分布里 **19（全脂奶/酸奶）占 62%**（全池 Tier 1 里它就有 334 条）。
所以 B−A 的结论主要是关于 5/19 这一对的，Methods 不能写成"在所有阈值型混淆对上都成立"。

---

## B · 需要你提供事实，我查不到也不能猜

### B4. Parle Smooth 那张图的 GT 里 21 是不是错标

我认为是错的：`21. Chocolate and candy` 列的全是固体糖果，
而 "Hazelnut Chocolate" 在这张图里是**口味名**不是品类。

A1 已定为单标签（"被广告的产品"原则），所以这张图的 GT 应该只留一个。
按该原则，被广告的产品是奶饮，GT 应为 19 —— **请确认**。

---

## C · 需要你在本机跑（我这边永远没外网）

云端沙箱与 `device_bash` 都不通外网，**所有真实调用只能你跑**。
这不是临时状况，是这个协作模式的固定约束。

Day 6 还欠的真实产物：

- [ ] 一张真实广告走完**慢路径**出 `direct_verified`（带真实 URL + 单条成本）
      —— 用 `--force-search` 或换一张牛奶/麦片图
- [ ] 混淆对冒烟 ≥3 张，看改判方向
- [ ] OCR 五语种冒烟（en/hi/bn/ur/si 各 1 张）
- [ ] `--fuse-test` 熔断实调
- [ ] SSE 在真实秒级延迟下的 broker 重放复验
- [ ] **新增**：`python -m eval.runner --ablation --split dev`（A3 三臂，需先有切分）

**另外需要你验证一件我没法验的**：百炼内置联网返回的 `search_info.search_results`
字段形状。我是按文档写的解析，**没跑过真的**。字段名对不上的话
`evidence` 会全空但不报错 —— 第一条真实 Evidence 出来后，
请务必确认 `source_url` 不是空字符串。

---

## D · 已登记的风险，等你决定何时处置

### D1. OPEN-RISK-01 · 缓存近名误命中 —— 已排期 Day 12

处置方案已定（你的补充决议）：**非对称 token 覆盖** +
`Amul Toned / Double Toned` 回归测试。Day 12 执行。

⚠️ **但非对称覆盖只修了一半**（数据侧核对指出，我复核同意）：

| 档案 | 查询 | `tokens(archive) ⊆ tokens(query)` | 结果 |
|---|---|---|---|
| `Amul Toned` | `Amul Double Toned Milk` | 成立 | **误命中，没挡住** ❌ |
| `Amul Double Toned` | `Amul Toned Milk` | 不成立 | 正确不命中 ✅ |

Toned（≤3g 脂肪 → 5）与 Double Toned 恰好跨 5/19 分界，这正是最该挡住的方向。

**Day 12 要加的第二条规则**：token 差集若含关键维度词
（`double` / `toned` / `full cream` / `skimmed` / `low fat` / `zero` …）则判不命中。
**回归用例必须把上表第一行写成"期望不命中"** —— 否则照着半个方案写出来的会是绿灯。

在此之前跑批，缓存命中率这个指标本身是虚的 —— 报数时要带这句。

### D2. 域名表三个国家是空的

`sources_by_country.json` 里 BD / PK / LK 的 `nutrition_db` 是空数组，
只能靠 official + ecommerce 兜底。按设计不阻塞运行，
但这三国的取证成功率会低于印度。等真实跑批的失败案例出来后再补。

### D4. 混淆对自动推导后，饮料类冲突判定覆盖收窄（新登记）

A3 的新推导规则下，`(18,25)` 与 `(25,29)` 不再是 definitional 对
（Annex 4 对它们没给数值切分点，判据在配料表不在营养表）。
后果：Day 5 的**跨源冲突判定**对饮料类失去了目标维度。

我认为收窄是对的 —— 原来那两对的"判定维度=糖"本来就不是 Annex 4 的判据，
拿它做冲突判定是在一个非判据维度上比数字。但如果真实跑批里饮料类
冲突漏检明显变多，来源就是这里。

**可选处置**：从 dev split 的误差分析把它们作为经验对注入
（`register_empirical_pair`，标 `source=dev_error_analysis`）——
这正是 A3 制度设计的用途。**但必须先有 dev split**（见 A6）。


### D6. 缓存命中结构性依赖语义分 —— `chromadb` 装不装该定了（Day7 实测发现）

**事实**：

```
W_EXACT_BRAND (0.55) + W_NAME_OVERLAP (0.20) = 0.75
CACHE_HIT_THRESHOLD                          = 0.82
```

**品牌精确匹配 + 名称 100% 重叠也只有 0.75，够不着阈值。**
也就是说**任何一次缓存命中都依赖语义分**那 0.25。

而 `chromadb` 是 `pyproject.toml` 里的 optional extra，**当前环境没装**，
跑的是 `_FallbackClient` —— 用 `difflib` 字符相似度冒充语义，
它自己的注释写着"够 demo，不够 6000 张评测"。

**后果**：向量库一挂或没装，缓存命中率直接归零，而 SQLite 档案还好端端躺着。
看板上表现为"记忆机制失效"，排查方向却会指向缓存写入 —— 很难查。

**三个选项**：

1. **装 chromadb**（`pip install -e ".[vector]"`）—— 按原计划 W5 就该装。
   代价：依赖变重，Docker 镜像变大
2. **调权重让精确匹配自己就够阈值**（如 `W_EXACT_BRAND` 0.55→0.62）——
   一行改动，但**改变缓存命中行为**，与"保持原始行为继续观察"冲突
3. **维持现状** —— 但要接受"缓存命中率"这个指标实际测的是 difflib 的表现

**我的建议**：跑 300 张 eval 之前必须选 1 或 2，否则命中率数字没有解释力。
今日按决议**没动**，只写了用例 `test_score_ceiling_without_semantic_is_below_the_hit_threshold`
把这个耦合钉住 —— 哪天有人调权重或阈值，那里会红。

**顺带**：Day7 修了 fallback 客户端跨进程不刷新的缺陷（构造后再不读盘），
不修则"手动插档案验证二次免搜索"永远过不了。**只改存储层，未动任何权重阈值。**

### D7. strict 模式挡不住"非维度形容词"类误命中

`Mock Crunchy Cereal 500g` → 档案 `Mock Cereal 500g` 在 strict 下**仍然放行**，
因为 `crunchy` 不是营养维度词。

strict 的两道规则覆盖的是"跨营养维度边界"那一类（Amul Toned/Double Toned、
full cream、instant、zero sugar）。"纯口味/形态形容词"那一类要另立规则
（比如"差集里出现任何未知形容词就降权而非直接命中"）。

不是实现漏掉，是规则的能力边界。要不要补第三条规则，等真实误命中分布出来再说。

---
### ~~D5. 混淆对推导判据：5 对 vs 17 对~~ —— 已由裁决①关闭（三档制）

我的推导给出 5 对；数据侧的独立推导给出 17 对（其中 `nutrient_threshold` 8 对）。
**不是谁抄错了，是判据取舍不同。**

| | 我的判据 | 数据侧判据 |
|---|---|---|
| 规则 | 同一营养素、同一 basis、方向相反、**切分点相同** | 同为某个营养/组成维度 |
| 结果 | `(2,12) (3,18) (5,19) (7,24) (8,23)` | 上述 + `(8,24) (9,16) (9,17)` + 9 对 `compositional_criterion` |

分歧集中在 `(8,24)`：8 按 **per serve** 的饱和脂肪+钠判，24 按 **per 100g** 的总脂肪判 ——
**没有共享的判定线**。我认为它推不出"视觉不可区分"，因为维度名相同但分母不同。

**我倾向坚持自己的判据**：A3 的整个论证依赖"混淆性是 Annex 4 定义的推论"，
判据放宽一分，这句话就弱一分。但把 `compositional_criterion` 那 9 对
（如 1/13 面条炸不炸、31/32 快餐健康与否）作为**第二类 definitional 对**
单独引入也说得通 —— 它们确实是定义决定的，只是判据不是数字。

**需要你定**：混淆对只收"共享数值切分线"的，还是把"定义级组成判据"也收进来？
收的话建议在 `source` 里分成 `definitional_threshold` / `definitional_compositional` 两档，
消融时可以再拆一个 arm。

---

## E · 协作机制上的固定约束（不用你解决，但要知道）

1. **我不能删文件**。沙箱对你的仓库只有读写，没有删除权限。
   所以每次同步完会剩一个 `_to_delete/` 目录要你手动 `rm -rf`。
2. **git 命令我不在你机器上跑**。git 每次刷新索引会留下一个我清不掉的
   `.git/index.lock`，上次就卡住过。所以 commit / push 始终留给你。
3. **真实 API 调用同上**，见 C 节。

---

## 已关闭

| 问题 | 决议 | 日期 |
|---|---|---|
| taxonomy 名称与 codebook 冲突（13 条语义级） | 外部重写为 v1.0-codebook，33 条全 confirmed | Day3 |
| HFSS 归属怎么定 | 从名称正则改为显式判定表 `HFSS_VERDICTS` | Day3 |
| 搜索后端选型 | 百炼内置联网（复用同一把 key） | Day6 |
| `ad_language` / `country` 进 Classification | 批准，两个字段一起加 | Day5 |
| 模型选型 | qwen3.7-plus 单一模型全链路 | Day6 |
| 中国站 / 国际站 | 国际站，`COST_CURRENCY=USD` | Day6 |
| **A1** 金标单标签还是多标签 | **单标签**，"被广告的产品"原则；4,942 条单标池，1,372 条多标封存 | Day6 |
| **A2** 评测集切分 | dev 200 / eval 300，按 country×language 分层，种子 `20260731` 写进 config，csv 进 git；smoke 12 条互斥 | Day6 |
| **A3** `confusing_pairs` 算不算泄漏 | 改为**从 Annex 4 阈值自动推导**（`source=definitional`）；经验对只能来自 dev 且标 `dev_error_analysis`；三臂消融 A/B/C + 匹配对照组已实现 | Day6 |
| **B1** taxonomy 丢了数值阈值 | 以协议 **Annex 4** 为权威来源，11 个 code 逐字回填 `thresholds` | Day6 |
| **B2** `_rule_based` 阈值是占位值 | 全部替换为 Annex 4 切分点，判定移入 `services/nutrient_rules.py`，代码里不写数字 | Day6 |
| **B3** 钠 → 盐 ×2.5 换算 | **删除**。一律在钠空间比较；标签只给盐时 `salt_g × 400 = sodium_mg` | Day6 |
| **B5** 成本估算只能填一档阶梯价 | 保持最低档，config 注明估算值是下限 | Day6 |
| **A6** 金标池 | 从 `~/imperial_foodad/human label/combined_file_allcountries_final.xlsx` 独立重建，4,942/1,372/27/80 四个数与数据侧记录逐条吻合；三集已落 `api/data/splits/` | Day6 |
| **裁决①** 混淆对判据分歧（5 对 vs 17 对） | 三档制：Tier1 definitional(5) / Tier2 definitional_compositional(8) / Tier3 dev_error_analysis；消融拆成四臂 A/B/B2/C，Tier2 单独占一臂 | Day6 |
| **裁决②** eval 跨国切片 | 只报描述性切片（每层带 n），删除 `slice_gap`，eval 维持 300 不扩量；报告与 README 各写一句 limitation | Day6 |
| **OPEN-RISK-02** 测试打真实 API | 实测一轮 28 次真实 POST。conftest 强制四个 provider 为 mock + 抹 key + httpx 出站熔断 + `realapi` marker；`.env=dev+qwen` 下整轮全绿零请求 | Day6 |
| **A10** 消融集按档配额 | 批准 `definitional=60, definitional_compositional=30`；追加单对上限 50%，抽法改按对轮流取 → Tier 1 五对各 12 条（5/19 从 72% 降到 20%）；Methods 措辞约束进 manifest；跑批批准（720 次调用 ≈ $1–2） | Day6 |
| **A9** 消融集能否超出 dev | 批准从「池−eval−smoke」抽，三条件全落码：互斥断言 / Methods 单列 ablation set / Tier3 仍只来自 dev | Day6 |
| **A4** 35/36/37 补不补 | **维持 33 类**，不补；27 张 parked 到 unrepresentable_gold.csv，Day 12 前不进任何指标 | Day6 |
| **A7** 22 与 32 是否同义 | **按同义处理**，维持 22→32；Methods 写「22/32 在 codebook 中定义一致，标注与评测均按 32 计」 | Day6 |
| splits/manifest.json 的 seed 记成 null | `dict.get` 被 `seed=None` 遮蔽；抽出 `resolve_params()` 单点解析，切分与 manifest 共用 | Day6 |
| **D3** 按语言/国家切片指标 | 批准并实现；优先用金标语言而非模型判读，每层带 `reliable`(n≥20) 标记 | Day6 |
| Annex 4 边界值（糖恰好 20 / 纤维恰好 5） | 原文有定义缝隙；**本项目补充规则：边界归非健康类(12)**，理由串里明写来源 | Day6 |
| Annex 4 的 23 用逗号并列两条件 | **本项目读作 OR**，记入 `taxonomy.json` 的 `project_note` | Day6 |
