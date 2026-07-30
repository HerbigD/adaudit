# AdAudit v2 · 置信度感知的食品广告品类审计 Agent

把「focus on category，置信度感知分析系统」升级成 **Agent + Web 应用**，实现全链路闭环：

> 上传广告 → 品类识别（12 大类 / 33 细类）→ 低置信 Agent 联网取证重裁决 →
> 高置信直出 / 低置信人工复核（双选项 original vs prediction）→ 人工结果回流 →
> 批次品类结构分析报告

当前状态：**Day 5 完成** —— 骨架 + `classify_initial` + 条件边① + 粒度自适应 + **搜索取证链路**
（多语言查询构造 / 候选筛选 / 单位换算 / LLM 抽取与降级 / 冲突判定 / `search_status` 状态机）。
mock 模式下六条路径全链路跑通。测试 **118 passed**。数据域：南亚四国（IN / BD / PK / LK）。

> `api/data/taxonomy.json` 已是 **v1.0-codebook**（逐条对照原始 codebook，33 条全部 `confirmed=true`）。
> v0.9-draft 时期的差异清单已归档于 `docs/archive/taxonomy_conflicts_resolved.md`。

---

## 快速开始

```bash
cp .env.example .env          # 默认 APP_ENV=mock，不需要任何 API key

# 激活虚拟环境
source /Users/d/.venv/bin/activate

# 后端
cd api && pip install -e . && uvicorn main:app --reload --port 8000

# 前端（另开一个终端）
cd web && npm install && npm run dev     # http://localhost:3000
```

或者一键起：

```bash
docker compose up --build     # web:3000 / api:8000，数据卷挂 ./data
```

**mock 模式约定**（不调任何外部 API，路径由文件名决定，方便 demo 与集成测试）：

| 上传的文件名 | 走的路径 |
|---|---|
| 任意（如 `ad.png`） | 高置信 → `direct` 快路径，一次 VLM 调用直出 |
| 含 `low`（如 `low-cereal.png`） | 低置信 + 有品牌 → 缓存/搜索取证 → 重裁决 |
| 含 `parent`（如 `parent-cereal.png`） | 叶子低 + 父类高 → **粒度自适应**按父类输出，取证后回落叶子 |
| 含 `conflict`（如 `conflict-yoghurt.png`） | 搜索返回矛盾营养数据 → 证据冲突 → 人工 → supersede |
| 含 `serving`（如 `serving-cereal.png`） | 抽到 per-serving 读数但缺份量 → `degraded`，阈值上调后仍需人工 |
| 含 `degraded`（如 `degraded-snack.png`） | 页面无营养面板 → 降级证据 |
| 含 `nobrand`（如 `nobrand-ad.png`） | 低置信 + 无锚点 → 直接 `human_review` 挂起 |

同一个产品第二次上传会**命中缓存直接出结果**（demo 里"记忆生效的哇时刻"）。

---

## 架构

```
┌──────────────────────────┐        ┌────────────────────────────────────┐
│  Next.js (App Router)    │  REST  │  FastAPI                           │
│  localhost:3000          │◀──────▶│  ├─ /api/audits      审计 CRUD     │
│  4 页 + SSE 客户端        │  SSE   │  ├─ /api/audits/:id/stream  流式   │
└──────────────────────────┘◀──────▶│  ├─ /api/review      人工复核      │
                                    │  ├─ /api/batches     批次与报告    │
                                    │  └─ graph/  LangGraph 状态机       │
                                    │       ├─ SQLite checkpointer       │
                                    │       ├─ 产品缓存库（向量+SQLite）  │
                                    │       └─ MCP 联网搜索工具（W4）     │
                                    └────────────────────────────────────┘
```

LangGraph 状态机（`GET /api/graph` 可实时导出这张图）：

```mermaid
graph TD;
	__start__([__start__]):::first
	classify_initial(classify_initial)
	cache_lookup(cache_lookup)
	web_search(web_search)
	adjudicate_with_evidence(adjudicate_with_evidence)
	human_review(human_review)
	feedback_ingest(feedback_ingest)
	output(output)
	__end__([__end__]):::last
	__start__ --> classify_initial;
	classify_initial -. direct .-> output;
	classify_initial -. search .-> cache_lookup;
	classify_initial -. human .-> human_review;
	cache_lookup -. hit .-> adjudicate_with_evidence;
	cache_lookup -. miss .-> web_search;
	web_search --> adjudicate_with_evidence;
	adjudicate_with_evidence -. direct_verified .-> output;
	adjudicate_with_evidence -. human .-> human_review;
	human_review --> feedback_ingest;
	feedback_ingest --> __end__;
	output --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

**结构性设计**：两个条件边把一张图切成**快路径**（VLM 一次调用直出，保吞吐与成本）和
**慢路径**（取证 + 可能人工，保难点样本准确率）。主干无环 —— 这不是 ReAct 式循环 Agent，
而是**有向无环状态机**，每次执行路径由数据决定。

---

## 目录结构

```
adaudit/
├── docker-compose.yml
├── .env.example
├── api/
│   ├── main.py                    # FastAPI 入口 + /api/health + /api/graph
│   ├── config.py                  # 模型选择、搜索预算、置信度阈值（全部可调）
│   ├── db.py                      # SQLite 连接 + 4 张表
│   ├── graph/
│   │   ├── builder.py             # StateGraph 组装 + AsyncSqliteSaver
│   │   ├── state.py               # AuditState / Classification / Evidence / StepTrace
│   │   ├── edges.py               # decide_route_1 / decide_route_2（纯函数，可单测）
│   │   ├── events.py              # 节点 → SSE 的 custom stream 通道
│   │   └── nodes/                 # 7 个节点
│   ├── services/
│   │   ├── taxonomy.py            # 33 类定义 → system prompt（唯一事实来源）
│   │   ├── vlm.py                 # mock/gemini/qwen/openai 适配层 + 结构化校验
│   │   ├── search.py              # 搜索预算 / 超时 / 重试
│   │   ├── nutrition.py           # 搜索结果 → Evidence（糖/脂/纤维/盐）+ 冲突检测
│   │   ├── cache_store.py         # 产品缓存库：SQLite + 向量 混合检索 + 重排
│   │   ├── memory.py              # few-shot 修正记忆
│   │   ├── vectorstore.py         # Chroma 薄封装（未装 chromadb 时自动降级）
│   │   ├── broker.py              # 按 audit_id 的事件总线（支持迟到订阅重放）
│   │   ├── runner.py              # 跑图 / resume + 事件翻译 + 落库
│   │   └── report.py              # 批次聚合统计 + LLM 报告
│   ├── routers/                   # audits / review / batches
│   ├── eval/                      # dataset / runner / metrics
│   └── tests/                     # 边逻辑单测 + 三条路径集成测试
├── web/
│   ├── app/                       # 4 页：/ · /audits/[id] · /review · /batches/[id]
│   ├── components/                # AuditCard / AgentTrace / ReviewCompare / CategoryChart / ReportView
│   └── lib/                       # api.ts（REST）· sse.ts（6 种事件）· types.ts
└── data/                          # adaudit.db / checkpoints.db / chroma / uploads
```

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/audits` | 上传图片（多张自动建批次），异步启动图 |
| GET | `/api/audits/{id}` | 单张当前状态（initial/revised/final/route/trace） |
| GET | `/api/audits/{id}/stream` | **SSE**：Agent 过程实时流 |
| GET | `/api/review/queue` | 人工队列（status=pending_human） |
| GET | `/api/review/taxonomy` | 33 类级联选择器数据源 |
| POST | `/api/review/{id}/decide` | 裁定 `{choice, manual_category?}` → resume 图 |
| GET | `/api/batches/{id}` | 批次状态 + 聚合统计 |
| POST | `/api/batches/{id}/report` | 生成 LLM 报告 |
| GET | `/api/batches/{id}/trend` | 跨批次看板曲线 |
| GET | `/api/taxonomy` | 两级级联选择器数据（taxonomy.json 的第二份产物） |
| GET | `/api/taxonomy/tokens` | prompt 块 token 计量（验收项 ≤2000） |
| GET | `/api/graph` | 导出状态机 mermaid |

### SSE 事件协议（前端只认这 6 种）

```
event: node_start   data: {"node":"web_search"}
event: node_log     data: {"node":"web_search","msg":"正在搜索「XX 牌酸奶」营养成分…"}
event: node_end     data: {"node":"adjudicate_with_evidence","ms":1840,"summary":"...","fallback_reason":null}
event: classified   data: {"initial":{...},"route_1":"search"}
event: need_human   data: {"initial":{...},"revised":{...},"reason":"证据冲突"}
event: done         data: {"final":{...},"route":"direct_verified","human_choice":null}
```

所有失败兜底（搜索无结果 / 超时 / 预算耗尽 / 证据冲突）都走 `node_log` + `need_human`，
前端在 trace 时间线上**标黄**。异常路径产品化，不抛异常。

---

## 已做的工程决策

1. **置信度阈值全部在 `config.py`**，不写死在边函数里 —— eval 阶段可扫参调优。
2. **路由决策显式落进 state**（`route_1` / `route_2`）：决策逻辑 `decide_*` 是纯函数由节点调用，
   条件边只读 state。trace、eval 归因、UI 都能直接读到"为什么走了这条路"。
3. **checkpointer 单独一个 db 文件**（`data/checkpoints.db`）：LangGraph 跑图时长时间持有写事务，
   与业务表同文件会在 `feedback_ingest` 写库时撞出 `database is locked`。
4. **`output` 节点作为收敛点**：`direct` 与 `direct_verified` 都先过 `output` 再到 END，
   保证图结束前 `final` 必定有值，下游只消费 `final`。
5. **事件总线 + 重放**：上传后立即跳转，SSE 迟连不丢前几条事件。
6. **向量库可降级**：没装 `chromadb` 时 `vectorstore` 自动切到本地相似度实现，骨架照样跑通；
   `pip install -e ".[vector]"` 后无需改任何调用方代码。
7. **taxonomy 单一事实来源**：`api/data/taxonomy.json` 是**分类数据**的唯一来源，代码里不硬编码分类。
   但 **HFSS 归属是叠加其上的政策判断**，用显式判定表 `taxonomy.HFSS_VERDICTS`（一类一行结论 +
   一句依据），不从名称字符串猜 —— 早期的名称正则会把"不含甜味粉剂"的茶咖判成高糖。
   判定表的覆盖性由加载期校验强制：taxonomy 新增编号而判定表没写，启动即报错。
8. **缓存写入护栏 + provenance 单向棘轮**：auto 档案要过六道条件才写；人工裁定按
   `(brand, product_name)` 唯一键 supersede，`auto` 永远覆盖不了 `human_verified`。
9. **mock 结果不可能变成指标**：每条结果带 `adapter` 标记，`eval.runner` 前后两道断言，
   默认配置直接拒跑；批次页检测到 mock adapter 会挂黄条。

---

## 测试

```bash
cd api && pytest -q      # 61 passed
```

| 文件 | 覆盖 |
|---|---|
| `test_edges.py` | 条件边①三路 / 条件边②两路分流、叶子未定不得直出 |
| `test_hierarchy_confidence.py` | 9 组层级置信度组合（含 4 组阈值边界）+ 候选保留 + 历史码 22→32 |
| `test_taxonomy.py` | taxonomy.json 加载校验、prompt token 预算、`*` 标记自洽、级联数据 |
| `test_cache_guardrails.py` | 六道写入护栏 + provenance 棘轮 + supersede |
| `test_eval_guard.py` | eval runner 的 mock 前置/后置断言 |
| `test_graph_flow.py` | 四条路径端到端 + trace 完整性 + adapter 标记 |
| `test_search_chain.py` | Day5 §8 七组：失败态/冲突判定/单位换算/降级/预算/多语言/域名表 |

---

## 后续排期（对齐 8 周）

| 周 | 交付物 | 当前状态 |
|---|---|---|
| W1–2 | repo + FastAPI + SSE + Next.js 骨架 + SQLite 建表 | ✅ 已完成 |
| W3 / Day3 | taxonomy.json 接入 + classify_initial + 条件边① + 粒度自适应 + 三项加固 | ✅ 已完成（`docs/daily/day3.md`） |
| Day3 补 | taxonomy 升级到 v1.0-codebook + HFSS 判定表 | ✅ 已完成（`docs/daily/day3-taxonomy-update.md`） |
| W4 / Day5 | 搜索取证链路（多语言查询 / 筛选 / 抽取 / 降级 / 冲突 / 状态机） | ✅ 已完成（`docs/daily/day5.md`）；`_search_once` 待接 MCP |
| W4 | MCP 联网搜索（`services/search.py::_search_once`）+ 真实 LLM 重裁决 | 预算/超时/兜底框架已就位 |
| W5 | Chroma 混合检索 + few-shot 记忆 | 接口已定，装 `[vector]` 即切换 |
| W6 | **红线：interrupt + 复核队列 + 裁定 resume** | ✅ 已串通（mock 下） |
| W7 | eval runner 跑 300 张 + Docker 一键起 | runner 骨架 + compose 已就位 |

代码里所有待接入点都用 `TODO(W3)` / `TODO(W4)` / `TODO(W5)` / `TODO(W7)` 标注，可直接 grep。

---

## Future Work（明确不做）

- 用户系统、权限、多租户
- 多语言界面
- 独立部署的 Agent 服务（当前 LangGraph 嵌在 FastAPI 进程内，W6 红线是全链路串通）
- 视频/动态广告素材

## 数据合规

demo 只使用公开渠道的广告图片。评测集与人工标注不随仓库分发。
