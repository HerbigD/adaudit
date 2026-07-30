# Day 5 日报 · 搜索取证链路（南亚多语言版）

测试 **118 passed**（新增 44 条）｜前端 build 通过｜六条路径 E2E 全通

---

## 0. 申报事项与结论

设计文档 §9 标注 `Classification` 新增 `ad_language` / `country` 属 state schema 变更，需申报。
**已申报并获批，两个字段一起加。** 连带完成的软约定：taxonomy prompt 改英文为主、写 CLAUDE.md。
eval 按语言切片的**指标**本次未做，但 `Prediction` 已留 `language` / `country` / `search_status` 字段。

---

## 1. 交付清单

| 文件 | 内容 |
|---|---|
| `services/search.py` | 查询构造（多语言）+ SearchBudget + 执行与重试 |
| `services/nutrition.py` | 候选筛选 → 单位换算 → LLM/规则抽取 → 降级 → 冲突判定 |
| `graph/nodes/web_search.py` | 编排全链路 + `search_status` 状态机 + trace |
| `graph/state.py` | `NutrientValue` / 新 `Evidence` / `Classification` 两个新字段 |
| `graph/edges.py` | route_2 支持 `degraded`（阈值上调）与 `conflict`（强制人工） |
| `api/data/sources_by_country.json` | 域名黑名单 + 四国电商/营养库域名表 |
| `api/data/category_terms.json` | 本土品类词 + 英文营销停用词 |
| `tests/test_search_chain.py` | §8 七组验收测试 + 状态机 |
| `CLAUDE.md` | 仓库工作约定（含 §9 三条软约定） |

---

## 2. 查询构造（§3）

```
Q1  "{brand} {product_name} nutrition facts"     tier=1
Q2  "{brand} {product_name} official site"       tier=2
Q3  "{product_name} nutrition {本土文字}"          tier=3（裁决降权）
```

实测五种语言的产出：

| ad_language | 输入 | Q1 | Q3 |
|---|---|---|---|
| hi | `Maggi मैगी` / `New Limited Edition Masala Instant Noodles 70g` | `Maggi Masala Instant Noodles nutrition facts` | `Masala Instant Noodles nutrition मैगी` |
| bn | `Pran প্রাণ` / `Special Offer Chanachur 150g` | `Pran Chanachur 150g nutrition facts` | `… nutrition প্রাণ` |
| ur | `Olpers اولپرز` / `Full Cream Milk 1L` | `Olpers Full Cream Milk 1L nutrition facts` | `… nutrition اولپرز` |
| si | `Anchor ඇන්කර්` / `Toned Milk Powder 400g` | `Anchor Toned Milk Powder nutrition facts` | `… nutrition ඇන්කර්` |
| en | `Amul` / `Double Toned Milk 500ml` | `Amul Double Toned Milk nutrition facts` | `Double Toned Milk nutrition` |

三条规则的实现细节值得记一笔：

1. **截断优先保住本土品类词**。`Masala Instant Noodles` 有 22 字符、超过 20 的上限，
   但 `instant noodles` 在 `category_terms.json` 里，整体保住 —— 只截到 `Masala Instant`
   等于把 13 类的判据扔了。`toned milk` / `double toned milk` 同理（直接决定 5/19）。
2. **本土文字只出现在 Q3**。`split_script()` 按 Unicode 区段把品牌拆成拉丁/本土两半，
   Q1/Q2 只用拉丁转写，Q3 才带本土原文（Daraz 类本土电商常有本土文字标题）。
3. **泛类目查询直接拒发**。`build_queries(None, "yoghurt")` 返回空列表 ——
   召回的是品类平均值，会污染 Evidence。

**禁止中文**由 `assert_no_cjk()` 在构造出口处硬拦，测试里五种语言各断言一次。

## 3. 筛选与抽取（§5）

**阶段一（零成本）**：黑名单 → 标题重叠 → source_type → 排序取前 3。

- 黑名单覆盖视频/社交/问答/新闻站。`ndtv.com` 既判 `other` 也进黑名单（§8 测试 7）。
- **本土文字标题不被误杀**：标题全是孟加拉文、与英文查询词零重叠时不判"无重叠"，
  放行交给能读多语言的阶段二。这条不写，Daraz 页面会被整批筛掉。
- 国家推不出时**合并全部国家域名表**，宁可判宽 —— `daraz.lk` 仍判 ecommerce 而非 other。

**阶段二**：一次 LLM 调用批量处理 3 个候选，全英文 prompt。三条降级路径：

| 情况 | 行为 | adapter |
|---|---|---|
| LLM 正常返回 | 结构化 Evidence | provider 名 |
| mock / 未配 provider | 规则正则抽取 | `mock-extract`（被 eval 双闸拦截） |
| 坏 JSON / 全部 match=false | 降级 Evidence（snippet + conclusion_hint） | `degraded` |

降级**不调第二次 LLM**，且不抛异常。裁决 prompt 会被显式追加
"the evidence is DEGRADED … you must state that the call is based on unstructured evidence"，
规则兜底路径也会在 reasoning 前缀加"【基于非结构化证据】"并把置信度压到 ≤0.60。

## 4. 单位换算（全项目唯一一处）

`nutrition.normalize()`。实测：

| 输入 | 输出 |
|---|---|
| `168 mg/100g` | `0.168` |
| `9 g/serving`（缺份量） | `None` |
| `9 g/serving` + serving=30g | `30.0` |
| `11 g/floz` | `37.196` |
| `9 g/30g` | `30.0` |
| `20 %RDA` | `None` |

裁决节点只看 `normalized`，不做任何换算。

## 5. 冲突判定（§6）

三条全满足才判冲突。实测 mock 冲突样本的 trace：

```
fat 跨来源相对偏差 88%（1.2–9.8），且该维度正是判定依据
```

混淆对 → 判定维度用**显式表** `taxonomy.PAIR_NUTRIENTS`（不从中文 key_dimensions 猜，
教训见 Day3 补记的 HFSS 正则事故），覆盖性由加载期校验强制。
`protein` 差 800% 但不在 5/19 的判定维度上 → **不判冲突**，测试钉死了这条。

## 6. `search_status` 状态机（§7）

六条 mock 路径 E2E 实测：

| 广告 | search_status | extract_mode | 终局 |
|---|---|---|---|
| highconf | —（快路径不取证） | — | `direct` |
| low-cereal | `ok` | rule | `direct_verified` |
| serving-cereal | `degraded` | rule | `pending_human` |
| degraded-snack | `degraded` | degraded | `pending_human` |
| conflict-yoghurt | `conflict` | rule | `pending_human` |
| nobrand | —（无锚点直接人工） | — | `pending_human` |

**一处对文档的收紧**：文档把 `ok` 定义为"≥1 条 Evidence 含目标维度 normalized 值"。
按字面实现时 serving 样本抽到了读数（`9 g/serving`）却换算不出，仍被判 `ok`。
这不对 —— 一个卡不了阈值的数字不该享受直出门槛。已把 `decide_status` 改成
**检查 normalized 是否存在**，serving 样本因此正确落到 `degraded`。

`degraded` 档的价值在 E2E 里看得很清楚：它不是"扔人工"，是把 `VERIFIED_THRESHOLD`
从 0.75 抬到 0.80 再机审一次；只是本次 mock 的降级样本置信度被压到 0.60，没过线。

## 7. trace（§8）

`web_search` 的 `StepTrace.extra` 实测包含：`queries`（每条查询词/tier/耗时/结果数/状态/尝试次数）、
`candidates_screened`（in/blacklisted/no_overlap/out）、`extract_mode`、`evidence_ids`、
`conflict_check`（codes/verdict/why）、`ad_language`、`country`、`search_status`。
失败案例归因时"死在哪条查询"一眼可见。

## 8. Evidence 是契约（§1 原则 2）

`Classification.evidence_refs` 从 `list[int]` 改成 `list[str]`，存的是 `Evidence.id`
（`ev_001`…）。裁决 prompt 里给模型看的是**结构化读数 + id**，不是原文段落。
前端新增 `EvidenceList` 组件，按 id / source_type / tier / 降级标记 / 缓存核验状态渲染，
`normalized` 为 null 时显示原值并标"未换算"。

## 9. 连带改动

- **taxonomy prompt 改英文为主**：文本块 token 1038 → **637**（-39%），
  classify prompt 1535 → 1286。中文名保留在 `cascade()` 供 UI。
  `_DIM_ABBR` 缺映射会在加载期报错，防止中文悄悄漏进 prompt；测试 `test_prompt_block_is_english_only` 守着。
- **CLAUDE.md**：三条硬约定（模型侧英文 / 国家语言数据驱动 / mock 不得成为指标）+
  架构要点 + "显式表 vs 字符串推断"的教训 + 纪律（schema 变更需申报）。
- **缓存护栏适配**：`degraded` / `conflict` / `cache` 一律不沉淀 auto 档案；
  联网证据还必须带结构化读数才算数。

## 10. 遗留

- `search._search_once` 仍是 mock，MCP 联网工具待接（`TODO(W4/Day6)`）。
- `_rule_based` 阈值仍是占位值；钠→盐用了 ×2.5 的通用换算，接真实营养分级模型时统一核。
- eval 按语言/国家的**切片指标**未做，字段已留档。
- `sources_by_country.json` 里 BD/PK/LK 的 `nutrition_db` 是空表 —— 按设计不阻塞运行，
  待 eval 失败案例反馈后补。
