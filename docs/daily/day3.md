# Day 3 日报 · classify_initial + 条件边① + 三项骨架加固

taxonomy `v0.9-draft`｜测试 **61 passed**｜前端 build 通过｜mock 全链路 E2E 通过

---

## 一、骨架加固（评审意见 3 项）

### 1. 缓存写入护栏 + provenance

`adjudicate_with_evidence` 写 `product_cache` 前先过 `cache_store.should_cache()`，
六个条件全满足才写，任何一条不满足都**把拒写原因写进 trace**（`extra.cache_write`）：

| 条件 | 不满足时的 reason |
|---|---|
| `revised.specific_confidence >= DIRECT_THRESHOLD` | `置信度 0.62 < DIRECT_THRESHOLD 0.85` |
| `search_status == "ok"` | `search_status='cache' != ok` |
| evidence 含**联网**证据 | `无联网证据（缓存证据不重复沉淀）` |
| 无证据冲突 | `证据冲突` |
| 叶子已定 | `叶子未定（parent 级结果不入库）` |
| brand + product_name 齐备 | `缺少 brand/product_name 唯一键` |

档案新增 `provenance` 字段：`auto`（搜索自动沉淀）/ `human_verified`（人工裁定确认）。
`cache_lookup` 命中时把 `provenance` / `revision` / `hit_count` / `score` 一起写进
`StepTrace.extra`，命中 auto 档案额外打 `unverified_cache: true` ——
失败案例归因时才分得清"错在感知 / 错在检索 / 错在一条没人核过的缓存"。
`Evidence` 也带上 `provenance`，复核页的证据卡用绿/黄两色区分。

**为什么加这道护栏**：没有它，一条 0.62 置信度的自动结论会被后续所有同产品广告命中，
把一个错误固化成"记忆"，而且看板上的缓存命中率还会显得很好看。

### 2. feedback_ingest 的 supersede —— 实现方式说明

**结论：按 `(lower(brand), lower(product_name))` 唯一键 upsert，不做版本分叉。**

具体实现（`cache_store.supersede_with_human_verdict`）：

- 唯一索引 `idx_cache_key ON product_cache(lower(brand), lower(product_name))` 保证一产品一行
- 人工裁定命中已有 auto 档案 → `provenance` 升为 `human_verified`、`revision += 1`、
  记 `superseded_at` 与 `superseded_by`（触发覆盖的 audit_id），返回 `action="superseded"`
- **单向棘轮**：`provenance="auto"` 的写入遇到 `human_verified` 档案直接 `action="refused"`，
  只有另一次人工裁定能改写它

**为什么不做版本标记/版本链**：档案存的是"当前认定的营养事实"，不是审计流水。
流水已经完整地在 `audits.trace_json` 里了；再维护一条版本链，只会让 `cache_lookup`
每次面对"该取哪一版"的选择题，而这个问题没有比"取最新人工版"更好的答案。
`revision` + `superseded_at/by` 保留了可追溯性，代价是零。

### 3. adapter 标记 + eval 断言位

`StepTrace.adapter` 与 `Classification.adapter` 记录结果产出方：
`mock-vlm` / `mock-search` / `rule-fallback` / `gemini` / `qwen` / `openai` / `human`。

`eval/runner.py` 两道闸：

- `_preflight()` 开跑前查配置，`APP_ENV`/`VLM_PROVIDER`/`LLM_PROVIDER` 任一为 mock 直接
  抛 `MockResultRefused`，**默认配置就跑不起来**
- `_postflight()` 跑完后逐条查 `Prediction.adapters`，混入 mock/rule-fallback 就拒绝出指标
  （防运行中被切到兜底）

`--allow-mock` 仅供自测链路，输出会被强制打上 `MOCK_RESULT_DO_NOT_REPORT`。
批次页也会在检测到 mock adapter 时挂黄条提示"指标不可用于对外汇报"。

---

## 二、Day 3 正文

### 1. taxonomy 数据源

`api/data/taxonomy.json` 成为唯一事实来源，`services/taxonomy.py` 直接加载，
代码里不再有任何硬编码的分类数据。加载期硬校验：33 细类 / 12 大类 / 父类存在 /
`confusable_with` 不悬空 —— 数据错了在启动时炸，而不是跑批第 300 张才发现。

两份产物：

- **system prompt 文本块**：一行一类，`*` 标记 `evidence_needed` 非空的类别，
  `[dims]` 给关键区分维度，末尾附混淆对清单
- **级联选择器数据**：`GET /api/taxonomy`（`/api/review/taxonomy` 是同一份数据的别名）

派生量也从数据推，不写死：`hfss_codes()` 按名称语义正则推导，
taxonomy 名称一改，HFSS 集合自动跟着变。

**token 计量**（验收项）：

| | token |
|---|---|
| taxonomy 文本块 | **746** |
| classify system prompt（含规则与输出契约） | 1239 |
| adjudicate system prompt | 977 |
| 预算 | 2000 ✅ |

计量口径：优先 tiktoken `cl100k_base`；离线环境降级为保守启发式（CJK 1 字 = 1 token）。
`GET /api/taxonomy/tokens` 可随时查，`test_taxonomy.py` 把它钉成断言。

### 2. classify_initial

- taxonomy 全量进 prompt（不做 RAG）
- **粒度自适应**：`vlm.apply_granularity_policy()` 统一执行降级 ——
  叶子置信 < `DIRECT_THRESHOLD` 且父类置信 ≥ `GENERAL_FALLBACK_THRESHOLD` 时，
  `specific_code` 置空、`leaf_vs_parent="parent"`，候选叶子（含同父类的混淆兄弟）
  留在 `candidate_codes`，`reasoning` 追加说明"哪个营养指标能定夺"
- prompt 里的规则 4 让模型自己降级，代码里的 policy 兜底降级 —— 两者互为保险，
  保证 UI 的"确定层级/待定层级"和 eval 的粒度统计只有一套口径
- `StepTrace` 写入 `adapter`、`extra`（leaf_vs_parent / candidate_codes / taxonomy_version）
  与 `tokens_in/tokens_out/cost_usd` 空位（接真实 provider 后填）

### 3. 条件边①

沿用骨架的 `decide_route_1`（纯函数，节点调用后写进 state）+ `route_1`（只读）结构，
阈值全部从 `config` 读。新增一条：**叶子未定的结果永远不能走 `direct`** ——
报告要落到细类，父类级结果必须先去取证。

### 4. 层级置信度单测

`tests/test_hierarchy_confidence.py`：9 组置信度组合（含 4 组边界值：
叶子 =0.85 / =0.84、父类 =0.80 / =0.79），每组同时断言降级层级与路由结果；
另有 5 组针对 candidate_codes 保留、parent 不重复降级、历史码 22→32、非法码拒绝。

---

## 三、验收对照

| 验收项 | 结果 |
|---|---|
| 高置信图直出 END | ✅ `route_1=direct` → `output` → END，`status=direct` |
| 无法识别品牌走 human | ✅ 进队列，卡点原因显示"无法识别产品名/品牌" |
| trace_json 含完整 StepTrace | ✅ 节点序列 + ms + adapter + extra + 兜底原因全落库 |
| taxonomy prompt 块 ≤2000 token | ✅ **746** |

E2E 实测（mock）：粒度自适应样本 `initial.specific_code=None, candidates=[2,12]`
→ 取证 → `revised.specific_code=12` → `direct_verified`，叶子成功回落。

---

## 四、风险与待办

1. **taxonomy.json 名称是草案，33 条全部 `confirmed=false`**，且与《完整方案》第 2 节的
   codebook 有 **13 条语义级冲突**（7 条完全不同类、3 条语义相反、1 组编号互换）。
   最危险的是 `[11]`：json=含糖饮料 / codebook=瓶装水 —— HFSS 判定会整体翻转。
   完整清单见 `docs/taxonomy_conflicts.md`。**接 Gemini key 出指标前必须逐条核准。**
2. `_rule_based` 的阈值（糖 15g / 脂 3g / 脂 10g·盐 1.2g / 糖 2.5g）仍是占位值，
   adapter 打 `rule-fallback`，已被 eval 断言位拦住，出不了指标。
3. `tokens_in/out/cost_usd` 仍是空位，接真实 provider 时一并补（`TODO(W3-真实provider)`）。

## 五、mock 文件名约定（demo 与集成测试用）

| 文件名含 | 路径 |
|---|---|
| （其他） | 高置信 → `direct` 快路径 |
| `low` | 低置信 + 有品牌 → 缓存/搜索取证 |
| `parent` | 叶子低 + 父类高 → 粒度自适应按父类输出 |
| `conflict` | 搜索返回矛盾营养数据 → 证据冲突 → 人工 → supersede |
| `nobrand` | 无锚点 → 直接人工 |
