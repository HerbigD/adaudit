# Day 7 日报 · 缓存实测与观测

> 235 tests passed（此前 214，新增 21）｜web build 通过｜**Claude 侧真实调用 0 次，账本 0 token**。
> 缓存库按决议不重建，只做实测与观测。
> 实测过程中撞到一个**会让今日验收项永远过不了**的缺陷，见 §5。

---

## §0 接口 / 字段变更申报

**新表** `cache_hit_log`（`api/db.py`）—— 缓存命中观测台

| 列 | 含义 |
|---|---|
| `audit_id`（主键） | 一次审计最多一次命中，天然幂等 |
| `cache_id` / `score` / `provenance` / `match_mode` | 命中当下的事实 |
| `route_1` / `route_2` / `human_choice` | 命中之后走了哪条路 |
| `cached_code` / `final_code` / `overturned` | 缓存给的叶子 vs 人工最终结论 |

**`stats_json` 新增两数**（`services/report.aggregate`）：

- `cache_hit_rate` = 缓存命中数 / 批次总数
- `cache_overturn_rate` = 命中且人工改判 / 命中总数
- 附 `cache_overturn_detail`（分档明细，见 §1 的坑）

**新配置** `CACHE_MATCH_MODE=legacy|strict`，默认 `legacy`（不启用）。
`/api/health` 新增 `cache_match_mode` 字段 —— 改判率数字出来后要说得清是在哪种匹配下测的。

**SSE `node_end` 事件新增 `extra`**：后端 `StepTrace.extra` 原样透传。
前端时间线靠它区分"缓存命中跳过搜索"与"正在搜索"，**不解析 summary 文案**。

**新数据块** `category_terms.json` → `dimension_terms`（5 组 37 词），strict 模式的词表来源。

---

## §1 改判率统计口径

### 写在哪、什么时候写

- **命中当下**（`cache_lookup` 节点）落一行：得分、档案 id、provenance、匹配模式
- **落库时**（`services/runner._persist`）补齐：路由、人工裁定、改判与否

`_persist` 是 `start` 与 `resume` **都必经的唯一落库点**。放节点里就得写两处
（图有 `output→END` 与 `feedback_ingest→END` 两个终点），还会漏掉 interrupt 挂起的中间态。

### `overturned` 是三态，不是布尔

| 值 | 含义 |
|---|---|
| `1` | 人工改判了 |
| `0` | 人工确认了 |
| `NULL` | **还没走到人工** |

把 NULL 算成 0 会让改判率虚低 —— 而这个指标存在的意义恰恰是发现缓存在悄悄喂错答案。

### ⚠️ 这个指标本身有个坑，我一并报了出来

决议定义的分母是"缓存命中总数"。照做了，但它**会被"命中但没走到人工"的样本稀释**：
人工复核率越低，`cache_overturn_rate` 越接近 0，**与缓存质量无关**。

所以我同时输出 `cache_overturn_detail.overturn_rate_among_reviewed`
（只在经人工裁定过的命中里算）。两个一起看才有意义：

```json
"cache_hit_rate": 1.0,
"cache_overturn_rate": 0.0,
"cache_overturn_detail": {
  "hits": 1, "reviewed": 0, "overturned": 0,
  "overturn_rate_among_reviewed": null,
  "unreviewed": 1,
  "by_match_mode": {"legacy": 1},
  "by_provenance": {"auto": 1}
}
```

判断缓存质量看 `overturn_rate_among_reviewed`；`reviewed` 太小时它不可解读。

---

## §2 strict 模式（已实现，**未启用**）

`CACHE_MATCH_MODE` 默认 `legacy`。strict 两道否决：

**① 非对称覆盖** —— 档案名 token 必须被查询名全覆盖。
挡的是"查询比档案少词"的方向。

**② 维度词差集否决** —— ①放行后，看 `查询 − 档案` 的差集里有没有判定维度词。

②才是 Amul 那个方向的解药。数据侧指出的"只修了一半"就在这：
档案 `Amul Toned` 的 token 恰是查询 `Amul Double Toned Milk` 的子集，①放行，靠②才挡住。

### 双态回归（决议要求的那条锁）

`tests/test_cache_match_modes.py`，走**完整 `lookup()`** 而不只是 `strict_reject`：

| 查询 | 档案 | legacy | strict |
|---|---|---|---|
| `Amul Double Toned Milk` | `Amul Toned Milk` | **命中** | **否决**（维度词 `double toned`） |
| `Amul Toned Milk` | `Amul Double Toned Milk` | 命中 | 否决（非对称覆盖） |
| `Amul Toned Milk 1L` | `Amul Toned Milk` | 命中 | 命中（只多规格） |

两态结论必须相反 —— 这条就是 OPEN-RISK-01 的锁。
被否决时**仍返回得分**：返回 0.0 会让"库里没有"和"库里有但我们没用"长得一样。

### 词表数据驱动

`category_terms.json` 的 `dimension_terms`，5 组 37 词。
**没有复用 `keep_terms`** —— 那张表管"构造查询时不剥离"，里面有 `namkeen`/`atta`
这类纯品类词；合用会把它们也当成维度词。用途不同就分表。

### 一个 strict 挡不住的残留风险

原始的 OPEN-RISK-01 mock case（`Mock Crunchy Cereal 500g` → `Mock Cereal 500g`）
**strict 下仍然放行** —— `crunchy` 不是营养维度词。

strict 修的是"跨营养维度边界"那一类（Amul、full cream、instant、zero sugar），
修不了"纯口味/形态形容词"那一类。这是两道规则的能力边界，不是实现漏掉。
要覆盖后者得另立规则（比如"差集里有任何未知形容词就降权"），今日不做。

---

## §3 审计页时间线

`AgentTrace` 从"一条事件一行"改成"**一个节点一块**"。
理由：三态是**节点的状态**，而事件是状态的变化 —— 按事件渲染表达不了"这个节点正在进行中"。

四种呈现：

| 状态 | 视觉 |
|---|---|
| 进行中 | 蓝点脉冲 + 蓝底 |
| 完成 | 绿点 + 灰底 |
| **兜底** | 黄点 + **黄框**，附兜底原因 |
| **已跳过** | 灰点 + **虚线框**（视觉上不抢正常步骤的注意力） |

缓存命中时 `web_search` 根本不会启动，SSE 里没有它的事件 ——
时间线**补一个合成的"已跳过"步骤**，而不是凭空少一节。右上角徽标同步切换：
「缓存命中 · 跳过搜索」vs「联网搜索取证」。

判断依据全部读结构化字段（`extra.cache_id` / `extra.strict_rejected`），
**不去 summary 里找"缓存命中"四个字** —— 教训见 HFSS 那次从文案推语义。

### Playwright 复验（mock 模式，3 张截图）

`docs/daily/day7-shots/`：

```
01-timeline-search.png    classify=done cache=done web_search=done adjudicate=done output=done
02-timeline-cache.png     classify=done cache=done web_search=skipped adjudicate=done output=done
03-timeline-fallback.png  web_search=fallback adjudicate=fallback（黄框 + 兜底原因 conflict）
```

三项断言全过（脚本 `api/scripts/day7_timeline_shots.py` 末尾自打印验收行）。

strict 否决态**没出截图**：用现有 mock 品名凑不出"得分 ≥0.82 且触发否决"的场景，
为一张图去加 mock 分支不值当。该行为由 21 个单测锁死，证据强度高于截图。

---

## §4 真实跑数手册

`docs/真实跑数手册.md`，五项产物各一节（命令 / 预期输出 / 验收点 / 常见失败），
外加开跑前检查、缓存二次免搜索、成本纪律。

顺手修了 `--fuse-test` 的一处误导：mock 模式下 provider 在熔断之前就抛 `VLMError`，
原来一律报 **FAIL**，会让人以为熔断坏了然后去改一段本来没问题的代码。
现在报 **SKIP** 并说明"请在 `LLM_PROVIDER=qwen` 下重跑本项"。

`--force-search` 已验可用（把 `DIRECT_THRESHOLD` 临时抬到 1.01，只改进程内不写 `.env`）。

---

## §5 实测撞到的缺陷：fallback 向量库跨进程不刷新

### 怎么发现的

做验收项"手动插一条档案 → 验证二次免搜索"时，脚本插完档案，
已在运行的 API 进程**查不到**，`cache_lookup` 报：

```
缓存未命中（best score=0.75）
```

### 0.75 这个数字很特别

```
W_EXACT_BRAND (0.55) + W_NAME_OVERLAP (0.20) = 0.75
CACHE_HIT_THRESHOLD                          = 0.82
```

品牌精确匹配 + 名称 100% 重叠 = 0.75，**语义分为 0**。
所以这不是"没匹配上"，是"向量库里没有这条"。

### 根因

`_FallbackCollection.__init__` 把 `store.data[name]` 存成实例属性，
于是 collection 永远看的是**构造那一刻**的快照。
`fallback.json` 确实落盘了，但客户端构造后再也不读它。

后果：API 进程启动后，任何别的进程（脚本、eval runner、第二个 worker）
写进去的档案对它完全不可见。**今日验收项正是跨进程的，不修就永远过不了。**

### 修法

`_FallbackClient` 按 **mtime 惰性重载**；`_data` 改成 property，每次访问先过一遍重载。
半截文件（另一进程正在写）读到 `JSONDecodeError` 就跳过，下次再读。
**没有改任何权重或阈值** —— 这是存储层的缺陷修复，不是匹配策略变更。

修完实测（API 不重启）：

```
① 另一个进程插档案: created
② API 进程（未重启）: 缓存命中 MockBrand / Mock Cereal 500g（score=1.00）
   web_search 被跳过: True
```

### ⚠️ 但它顺带暴露了一件更该你知道的事

**`0.55 + 0.20 = 0.75 < 0.82` 意味着任何一次缓存命中都依赖语义分。**

- `chromadb` 是 **optional extra**，当前环境**没装**，跑的是 fallback
- fallback 用 `difflib` 字符相似度冒充语义，"够 demo，不够 6000 张评测"（它自己的注释）
- 向量库一挂 → 缓存命中率直接归零，而 SQLite 档案还好端端躺着 ——
  看板上会表现为"记忆机制失效"，排查方向却会指向缓存写入

今日**不动权重**（决议：保持原始行为继续观察），已写成用例
`test_score_ceiling_without_semantic_is_below_the_hit_threshold` 钉住这个耦合，
哪天有人调权重或阈值，那里会红。已登记 OPEN-QUESTIONS 的 D6。

---

## §6 成本

| 项 | 数 |
|---|---|
| Claude 侧真实调用 | **0 次** |
| 账本累计 | **0 token** |
| 测试触发的出站请求 | **0 次**（conftest 出站熔断） |

---

## §7 验收对照

| 验收项 | 结果 |
|---|---|
| mock 全绿含新增回归 | ✅ 235 passed（新增 21） |
| `stats_json` 两个新字段可查 | ✅ 端到端实测见 §1 |
| strict / legacy 双态测试锁死 | ✅ 走完整 `lookup()`，两态结论相反 |
| Playwright 两种时间线截图 | ✅ 3 张，三项断言自动打印 |
| 跑数手册可照抄执行 | ✅ `docs/真实跑数手册.md` |
| 今日 Claude 侧零真实调用 | ✅ 账本 0 token |

---

## §8 卡点与偏差

1. **strict 否决态无截图** —— 见 §3 末，用单测替代，已说明理由
2. **strict 挡不住非维度形容词** —— 见 §2 末，是规则的能力边界
3. **fallback 向量库的修复超出了"今日不重建缓存库"的字面范围** ——
   但它是**存储层缺陷**而非匹配策略变更，且不修则今日验收项无法完成。
   没有触碰任何权重、阈值、护栏。如果你认为这也该等，我可以回退（一行 revert）

---

## §9 明日建议

- `cache_overturn_rate` 现在有了，但**要人工复核发生过才有数**。
  真实跑数时若有转人工的样本，记得走完裁定，观测台才有 `overturned` 非 NULL 的行
- `chromadb` 装不装该定了（见 OPEN-QUESTIONS D6）——
  它同时影响缓存命中率和 few-shot 记忆召回，评测跑批前必须定
