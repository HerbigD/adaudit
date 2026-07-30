# CLAUDE.md · AdAudit v2 工作约定

给在本仓库工作的 Claude Code / 协作者。改动前先读这一页。

---

## 项目一句话

置信度感知的食品广告品类审计 Agent：上传广告 → VLM 初分类（12 大类 / 33 细类）→
低置信联网取证重裁决 → 高置信直出 / 低置信人工双选项复核 → 结果回流 → 批次品类结构报告。
数据域：南亚四国（印度 IN / 孟加拉国 BD / 巴基斯坦 PK / 斯里兰卡 LK）。

## 常用命令

```bash
cd api && pytest -q                 # 全量测试
cd api && uvicorn main:app --reload --port 8000
cd web && npm run dev               # http://localhost:3000
docker compose up --build           # 一键起
curl localhost:8000/api/health      # 配置/版本/token/缓存一览
curl localhost:8000/api/taxonomy/tokens
```

---

## 三条硬约定

### 1. 模型侧语言一律英文

送进模型的一切文本 —— taxonomy prompt 文本块、classify / adjudicate / 抽取 prompt、
查询词 —— **全部英文**。理由：codebook 本来就是英文标准，英文 prompt 省一道翻译失真；
南亚四国的营养信息公开网页也绝大多数是英文。

- 中文名只留在 UI 展示层（`taxonomy.cascade()`、前端）。
- `taxonomy._DIM_ABBR` 负责把 taxonomy.json 的中文 `key_dimensions` 映射成英文，
  **缺映射会在加载期报错**，防止中文悄悄漏进 prompt。
- 查询词禁止出现中日韩字符，`search.assert_no_cjk()` 把这条钉死。
- 测试守卫：`test_prompt_block_is_english_only`、`test_queries_are_english_and_never_chinese`。

### 2. 国家 / 语言相关内容一律数据驱动

新增国家**只改 JSON，不改代码**：

| 文件 | 内容 |
|---|---|
| `api/data/taxonomy.json` | 12 大类 / 33 细类，唯一分类数据源 |
| `api/data/sources_by_country.json` | 域名黑名单 + 各国电商/营养数据库域名表 |
| `api/data/category_terms.json` | 本土品类词（查询时不剥离）+ 英文营销停用词 |

"为泛化而设计"是简历/面试论点，README 里已点明，别把它写死回代码里。

### 3. mock 结果绝不能变成指标

每条结果都带 `adapter` 标记（`mock-vlm` / `mock-search` / `mock-extract` /
`rule-fallback` / `gemini` / `qwen` / `openai` / `human`）。
`eval/runner.py` 前后两道闸：开跑前查 config，跑完后查每条 Prediction 的 adapters，
任一为 mock 即抛 `MockResultRefused`。`--allow-mock` 只用于自测，输出强制打
`MOCK_RESULT_DO_NOT_REPORT`。批次页检测到 mock adapter 会挂黄条。

---

## 架构要点（改之前先明白为什么这么写）

- **LangGraph 嵌在 FastAPI 进程内**，不拆服务。checkpointer 用 SQLite，
  interrupt 状态天然持久化，人工队列 = `audits.status='pending_human'`。
- **checkpointer 单独一个库文件**（`data/checkpoints.db`）。与业务表同文件时，
  节点内写业务表会撞上 LangGraph 持有的写事务，实测 `database is locked`。
- **路由决策显式落进 state**：`decide_route_*` 是纯函数由节点调用并写 state，
  条件边只读。这样 trace / eval 归因 / UI 都能读到"为什么走了这条路"。
- **`output` 是收敛点**：`direct` 与 `direct_verified` 都先过 `output` 再到 END，
  保证图结束前 `final` 必有值。
- **失败是一等公民**：搜索无结果 / 超时 / 预算耗尽 / 证据冲突**都不抛异常**，
  写进 `search_status` + trace，由条件边当作正常路由结果。

## 判断表 vs 字符串推断

`HFSS_VERDICTS`（HFSS 归属）与 `PAIR_NUTRIENTS`（混淆对判定维度）都是**显式表**，
不从名称正则推。教训：早期版本用名称正则推 HFSS，把"茶与咖啡（**不含**甜味粉剂冲调）"
判成了高糖 —— 正则读不懂否定。两张表的覆盖性都由 `taxonomy._validate` 在加载期强制，
taxonomy 新增编号而表里没写，**启动即报错**。

新增这类"叠加在数据之上的政策判断"时，照这个模式写：显式表 + 一句依据 + 覆盖性校验。

## 单位换算只有一处

`nutrition.normalize()` 是全项目唯一做单位换算的地方。裁决节点只看
`NutrientValue.normalized`，**不做任何换算**。换算不出（如 per serving 缺份量）
一律返回 `None`，让下游知道这条读数不能卡阈值。

## Evidence 是契约不是文本

裁决节点消费结构化 `Evidence`，不允许把搜索结果原文直接糊进 prompt。
`Classification.evidence_refs` 存的是 `Evidence.id`（`ev_001`…），
groundedness 指标要求结论能核到具体条目。

---

## 纪律

- **state schema 变更需先申报**（`AuditState` / `Classification` / `Evidence` / `StepTrace`）。
  改之前说明动机、影响面、连带改动清单，得到确认再动手。
- **阈值不写死在逻辑里**，全部放 `config.py`，eval 阶段要扫参。
- **每个待接入点打 `TODO(W?)` 标记**，可 grep。
- 每天的实现记录写进 `docs/daily/dayN.md`；被推翻的设计连同理由一起留档
  （见 `docs/archive/`），不要静默删掉。

## 已知遗留

- `adjudicate._rule_based` 的阈值（糖 15g / 脂 3g / 脂 10g·盐 1.2g / 糖 2.5g）是**占位值**，
  接真实营养分级模型时统一核。adapter 打 `rule-fallback`，已被 eval 双闸拦住。
- `StepTrace.tokens_in / tokens_out / cost_usd` 是空位，接真实 provider 时填。
- `search._search_once` 尚未接 MCP 联网工具（`TODO(W4/Day6)`），mock 下按查询词关键词返回。
- eval 按语言/国家**切片指标**未做；`Prediction` 已留 `language` / `country` 字段。
