# Day 6 日报 · 主线从 mock 切真实（代码就绪，真跑待执行）

测试 **127 passed**（新增 9 条熔断用例）｜前端 build 通过

---

## §0 申报区

### 0.1 配置项变动（`config.py`）

| 项 | 变化 | 理由 |
|---|---|---|
| `qwen_model` | `qwen-vl-max-latest` → **`qwen3.7-plus`** | 单一模型全链路，已确认型号 |
| `vlm_model` / `llm_model` | 新增（默认 None → 落回 `qwen_model`） | 保留分别覆盖能力 |
| `qwen_enable_thinking` | 新增，默认 `false` | 分类/抽取是结构化任务，thinking 只拖延迟与 token |
| `qwen_json_mode` | 新增，`auto` | 先试 `response_format`，被拒则降级到 prompt 契约 |
| `dashscope_base_url` | 新增 | 中国站/国际站切换只改这一行 |
| `daily_token_budget` | 新增，`500_000` | **主熔断** |
| `usage_path` | 新增，`data/usage.json` | 跨进程账本 |
| `llm_price_in_per_mtok` / `llm_price_out_per_mtok` / `cost_currency` | 新增，`2 / 8 / CNY` | 成本估算配置化 |
| `search_provider` | 新增，`mock`\|`dashscope` | 搜索后端选型 |

### 0.2 State schema 变动

**无。** `AuditState` / `Classification` / `Evidence` 一个字段没动。
`StepTrace.tokens_in / tokens_out / cost_usd` 是 Day3 就留好的空位，今天只是**填实**。

### 0.3 新增文件与接口

- `api/services/usage.py` —— token 记账 + 成本熔断
- `api/scripts/day6_real_run.py` —— 真跑脚本（探测 / 单张 / OCR 冒烟 / 混淆对 / 熔断实调）
- `api/tests/test_budget_fuse.py` —— 9 条熔断用例
- `web/components/UsageBadge.tsx` —— 顶栏预算指示器
- `GET /api/usage`；`/api/health` 增加 `llm_provider` / `search_provider` / `model` / `usage`

### 0.4 当日真实调用累计

| 指标 | 值 |
|---|---|
| 真实调用次数 | **0** |
| tokens_in / out | **0 / 0** |
| 估算成本 | **0.0000 CNY** |

原因见下节 —— 不是没做，是**我这边的两个执行环境都没有外网**。

---

## §1 阻塞说明：真实调用必须在你的机器上跑

开工第一件事是探网，结论：

| 环境 | DashScope | 独立搜索 API | 备注 |
|---|---|---|---|
| 云端沙箱（我跑测试的地方） | `000` 不可达 | Tavily/Serper/Brave 全 `000` | 只放行 pypi/npm |
| `device_bash`（你机器上的 VM） | 无网络 | 无网络 | 设计如此 |

`DASHSCOPE_API_KEY` 已确认在 `api/.env` 里（118 字符，值未打印）。
所以**代码、熔断、测试、真跑脚本我全部写完并在 mock 下验证**，
但验收要求的三件真实产物（真实 Evidence JSON / 真实 trace + token 成本 / 混淆对冒烟）
只能由你在本机执行 —— 命令见 §7。

---

## §2 任务 0 · 成本熔断（先行完成）

### 为什么主熔断是 token 不是钱

token 没有货币歧义：同一把 key 打中国站计 CNY、打国际站计 USD，汇率与阶梯价都会变，
唯独"这次烧了多少 token"是 provider 直接返回的事实。
所以 `daily_token_budget` 是**硬闸**，成本只是按配置单价换算的**估算值**，供人看、不参与拦截。

### 跨进程不丢

uvicorn 可能多 worker、跑批脚本又是另一个进程，计数只放内存必然漏。
落 `data/usage.json`，每次 read-modify-write 走 `fcntl.flock` 排他锁 + `os.replace` 原子替换，跨日自动归零。

### 闸门装在基类，不是各家 adapter

`usage.guard()` 放在 `BaseVLM.classify()` 与 `vlm.complete()` 的入口，
**不是**放在 QwenVLM 内部。这样任何 provider 实现都不可能"忘了过闸"，
将来加 Gemini/OpenAI 也不用重复写。

### 熔断先于网络与 key 校验

`dashscope_chat` 里 `usage.guard()` 排在"有没有 key"之前。
实测（伪真实配置 + 假 key + 无网络）：

```json
{
  "refused": true,
  "refusal_message": "当日 token 预算已用尽：2/1（估算成本 0.0000 CNY）。调高 DAILY_TOKEN_BUDGET 或等次日归零。",
  "verdict": "PASS 熔断生效：真实调用在发出前被拒"
}
```

**超预算时连请求都不会发出去** —— 这条在没有网络的环境里反而更好证明。

### 熔断不静默降级

`adjudicate_with_evidence` 遇到 `BudgetExceeded` **不退回规则兜底**，而是直接
`route_2=human` + `fallback_reason=budget_exceeded`。
理由：静默兜底会让一次"省钱的降级"在报告里长得跟正常裁决一模一样。

### 测试（9 条，`tests/test_budget_fuse.py`）

记账与估算 / mock 不入账 / 跨进程持久化 / 预算内放行 / 超预算拒绝 /
snapshot 暴露给 UI / **classify 节点被拒且 trace 有据可查** /
**adjudicate 不静默兜底** / 换币种只改配置。

关键断言长这样：

```python
assert t.fallback_reason == "budget_exceeded"
assert t.extra["budget"]["exceeded"] is True
assert t.extra["budget"]["total_tokens"] == 1100
```

---

## §3 任务 1 · 搜索后端（百炼内置联网）

按你的选型走 `enable_search=true`，复用同一把 key。实现要点：

**URL 只认 `search_info.search_results`。** 模型正文里自己写的 URL 可能是编的，
而 `search_info` 里的条目是检索器实际访问过的页面。两路合并策略：

- URL ← `search_info`（可信）
- snippet ← 模型正文 JSON（带回了页面上的营养数字原文，抽取阶段要用）
- 以 URL 为键合并，**对不上的正文条目一律丢弃** —— 宁可少一条证据，也不能让编造的链接进 trace

Day5 的预算/超时/重试/Q1→Q3 序列**一行没改**：后端只负责 `query -> hits` 的形状转换。

---

## §4 任务 2 · Qwen provider

- 统一入口 `vlm.dashscope_chat()`：熔断 → JSON mode 探测 → 调用 → 按响应 `usage` 记账
- **JSON mode 探测**：先带 `response_format={"type":"json_object"}`；返回 400 就记住
  `(base_url, model)` 不支持并**立即用同一份 messages 重发一次**（不带 response_format），
  降级到 prompt 契约 + 既有的运行时校验。探测结论存在 `vlm.json_mode_state()`，
  真跑后会进 `probe.json` —— **结论待你跑完补进本节**
- `enable_thinking=false`
- `tokens_in/out/cost_usd` 通过 `usage.collect()` 归集进对应节点的 StepTrace：
  `classify_initial` 收感知那一次，`web_search` 收「搜索 + 抽取」两段，
  `adjudicate_with_evidence` 收裁决那一次

---

## §5 任务 4 · VERIFIED_THRESHOLD 的设计理由（已写进 config 注释）

0.75 低于 DIRECT_THRESHOLD 的 0.85，**不是笔误**：

> 初分类只有"看图"一个信息源，0.85 是在要求模型对纯视觉判断非常笃定；
> 重裁决多了一层外部营养证据，同样的 0.75 背后实际信息量更大 —— 证据加持
> 本就该换来更低的直出门槛，否则取证白做，慢路径样本会全部堆到人工。
> 反过来，证据质量不够时（`search_status=degraded`）这个门槛会被 +0.05 抬回去。

---

## §6 顺手修的一处（不在任务书内）

`test_cache_guardrails` 是**顺序相关的 flaky**：向量库是进程级单例，
上一个测试文件写进去的档案会漂到下一个，命中得分因此从 1.00 掉到 0.75，
在全量跑时随机失败。加了 `vectorstore.reset()` 并让该文件的 fixture 隔离 `chroma_path`，
连跑三次全绿。

> 这个现象和 OPEN-RISK-01（缓存近名误命中）是同一类问题的两面：
> 命中得分对"库里还有什么"过于敏感。今天按约定没动生产逻辑。

---

## §7 你要跑的命令

```bash
cd ~/github/adaudit/api
source /Users/d/.venv/bin/activate
```

**第一步：切真实配置**（编辑 `api/.env`，key 已在里面）

```
APP_ENV=dev
VLM_PROVIDER=qwen
LLM_PROVIDER=qwen
SEARCH_PROVIDER=dashscope
QWEN_MODEL=qwen3.7-plus
DAILY_TOKEN_BUDGET=500000
```

**第二步：探测**（几百 token，先确认型号与 JSON mode）

```bash
python scripts/day6_real_run.py --probe
```

不可达就先别往下跑，把 `data/day6/probe.json` 贴我。

**第三步：验收主项 —— 一张真实南亚广告走完整链路**

```bash
python scripts/day6_real_run.py --ad ~/path/to/amul-double-toned-milk.jpg
```

产出 `data/day6/` 下三个文件：`real_run_*.json`（含 route/成本）、
`evidence_*.json`（**全项目第一条真实证据**）、`trace_*.json`。

**第四步：两个冒烟**

```bash
python scripts/day6_real_run.py --ocr-smoke ~/path/to/五语种目录/
python scripts/day6_real_run.py --confusion ~/path/to/混淆对目录/     # ≥3 张
```

**第五步：熔断实调**

```bash
python scripts/day6_real_run.py --fuse-test
```

跑完把 `data/day6/` 整个目录贴回来（或告诉我路径，我读你仓库），
我把 §0.4 的累计数字、§4 的 JSON mode 结论、以及验收结论补全。

⚠️ 脚本自带自检：还在 mock 配置上会直接退出并告诉你该改哪几行，不会白烧 token。
⚠️ `data/day6/`、`data/usage.json` 已加进 `.gitignore`（今天补的 —— 原来的规则只挡了
`data/*.db`、`data/chroma/`、`data/uploads/`，真实跑批产物会漏出来）。

---

## §8 待你跑完才能填的空

- [ ] §0.4 当日真实调用累计（次数 / tokens / 成本）
- [ ] §4 JSON mode 是否可用的结论
- [ ] 验收 1：真实广告 direct_verified + 真实 URL + 单条成本
- [ ] 验收 2：混淆对 ≥3 张改判方向
- [ ] 任务 2 的 OCR 五语种抽取正确率
- [ ] 任务 5：SSE 在真实延迟（秒级）下的 broker 重放复验
