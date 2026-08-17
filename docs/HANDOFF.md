# AdAudit v2 · 交接文档

> 给**新会话**用。截至 Day 8 收工（2026-07-31）。
> 读完这一份就能直接开工，不需要翻历史对话。
>
> **优先级**：先看 §1（现状）→ §2（等你做的三件事）→ §3（待决策）→ 再开工。

---

## §1 现状快照

| 项 | 值 |
|---|---|
| 进度 | Day 8 收工，**代码侧实际已跑到 Day 11**（缓存库/记忆/interrupt/并发/批次报告在骨架期超前做完） |
| 测试 | **243 passed, 1 skipped**（16 个测试文件） |
| 仓库 | `~/github/adaudit`，分支 `day6-annex4-splits`（Day6/Day7 已 commit） |
| 前端 | `npm run build` 通过 |
| 真实调用 | Day 7、Day 8 均为 **0 次**，账本 0 token |

### 关键路径与文件

```
api/
  config.py                     所有阈值/开关（禁止散落硬编码）
  db.py                         5 张表 + MIGRATIONS + POST_MIGRATION_SCHEMA
  graph/
    state.py                    AuditState + Classification/Evidence/StepTrace
    builder.py                  StateGraph；两个终点：output→END、feedback_ingest→END
    nodes/                      classify_initial / cache_lookup / web_search /
                                adjudicate_with_evidence / human_review / feedback_ingest / output
  services/
    taxonomy.py                 33 类单一事实来源 + HFSS_VERDICTS + 混淆对三档制
    nutrient_rules.py           Annex 4 判定引擎（代码里不写任何阈值）
    nutrition.py                筛选→抽取→Evidence→冲突判定；单位换算唯一入口
    cache_store.py              混合检索 + 六道写入护栏 + strict 匹配
    vectorstore.py              chroma / difflib 双后端 + 降级守卫
    memory.py                   few-shot 修正记忆
    runner.py                   跑图/resume + SSE 翻译 + **唯一落库点 _persist**
    report.py                   batch 聚合（stats_json 是报告数字的唯一来源）
  eval/
    split.py                    A2 分层切分
    ablation.py                 A3 四臂消融 + 抽样清单
    metrics.py / dataset.py / runner.py
  data/splits/                  pool_4942 / dev 200 / eval 300 / smoke 12 /
                                unrepresentable_gold 27 / ablation_manifest（进 git）
docs/
  OPEN-QUESTIONS.md             ★ 待决策清单（先读这个）
  真实跑数手册.md                ★ 本机跑真实 API 照抄即可
  daily/day3..day8.md           11 份日报
  HANDOFF.md                    本文件
```

### 项目知识库里的记录（跨会话可见）

`AdAudit_Day8_移植性_chromadb_回流.md` / `Day7_缓存观测与strict预备.md` /
`Day6c_裁决执行_金标池重建.md` / `Day6b_六项决策_代码侧执行.md` /
`六项决策执行_Annex4核对.md`（数据侧独立核对，很重要）/ `Day5_搜索链路记录.md` 等。

---

## §2 立刻要人类做的三件事（都卡着后续）

### 2.1 macOS 整轮 pytest ⏳

```bash
cd api && pytest -q          # 期望 243 passed
```

Day 8 修了三个 bug，**根因都是"测试环境比真实环境干净"**：
`chroma_path` 裸赋值泄漏、`assert "/tmp" in path` 写死、
索引建在新增列上导致**老库**迁移失败。Claude 侧只能模拟 `TMPDIR=/var/folders/...`，
模拟不等于真机 —— 这一项必须人类在 macOS 上亲自确认。

### 2.2 chroma 一侧的 backend 对比 ⏳

```bash
cd api
python3 scripts/day8_backend_compare.py --out ../data/day8/backend_chroma.json
python3 scripts/day8_backend_compare.py --report ../data/day8/backend_difflib.json ../data/day8/backend_chroma.json
```

difflib 一侧已测：**近名误命中率 1.0**（`Amul Double Toned Milk` 命中了
`Amul Toned Milk` 的档案，跨 5/19）。chroma 一侧沙箱跑不了 ——
chromadb 首次使用要**联网下载 ~80MB embedding 模型**。

⚠️ 从今起任何缓存指标都必须注明 `cache_backend`，两种后端的命中率不可比。

### 2.3 消融跑批（已批准，720 次调用 ≈ $1–2）⏳

清单已出并经人类过目：`api/data/splits/ablation_manifest{,_ids}.json/csv`

```bash
python3 -m eval.runner --ablation --tier-quota "definitional=60,definitional_compositional=30"
```

- 混淆组 90（Tier1 五对各 12 / Tier2 八对各 3–4）+ 对照组 90
- 支撑量：**B−A n=60 ✅**｜**B2−B n=30 ✅**｜**C−B2 n=0 ❌**（Tier 3 尚无经验对）
- `overlap_with_held_out: 0`（代码断言）
- 跑完把结果给新会话，补四臂 DiD 对比表进日报

### 2.4 Day 6 还欠的 5 项真实产物

见 `docs/真实跑数手册.md`（每项含命令/预期输出/验收点/失败排查）。
**其中一项必须人眼确认**：百炼内置联网返回的 `search_info.search_results`
字段形状是照文档写的、没跑过真的。字段名对不上时 `evidence` 会**全空但不报错**。
第一条真实 Evidence 出来后务必确认 `source_url` 不是空字符串。

---

## §3 待决策（详见 `docs/OPEN-QUESTIONS.md`）

| 编号 | 问题 | 拖着的代价 |
|---|---|---|
| **A5** | 五处定义细节我补进了 `description_zh`（面条不含油炸 / 果汁≥98% / 益生菌饮品 / 无糖口香糖归 28），**需与手上 codebook 比对** | 中：无糖口香糖算 21 还是 28 直接改变那批样本对错 |
| **B4** | Parle Smooth 那张图的 GT，按 A1 单标签原则应只留 19，待确认 | 低 |
| **D2** | BD/PK/LK 三国 `nutrition_db` 域名表是空的 | 低，等失败案例再补 |
| **D4** | 混淆对自动推导后，饮料类跨源冲突判定覆盖收窄 | 低，可从 dev 注入经验对 |
| **D6** | 缓存命中**结构性依赖语义分**：`0.55+0.20=0.75 < 0.82 阈值` | 已裁决装 chromadb，但权重耦合仍在，调阈值/权重时会有用例报红 |
| **D7** | strict 挡不住"非维度形容词"类误命中（`Mock Crunchy Cereal` → `Mock Cereal`） | 低，是规则能力边界 |

### 已裁决、执行完毕的（不要重开）

A1 单标签 · A2 dev200/eval300 分层 · A3 混淆对**三档制** · A4 维持 33 类（27 张 parked）·
A6 金标池已重建 · A7 22→32 按同义 · A8/裁决② 切片只描述不对比 ·
A9 消融集从「池−eval−smoke」抽 · A10 配额 60/30 + 单对上限 ·
B1/B2/B3 Annex 4 权威阈值 · B5 成本估算保持下限 · D3 语言国家切片 ·
D5 判据分歧（→三档制）· OPEN-RISK-02 测试打真实 API（已修）

---

## §4 必须遵守的纪律（踩过坑才写下来的）

1. **模型侧一律英文**；国家/语言相关全部数据驱动（改 JSON，不改代码）
2. **mock 结果永不进指标**：`eval/runner.py` 双闸（开跑前查 config + 跑完查 adapters）
3. **测试永不打真实 API**：`conftest` 强制四个 provider 为 mock + 抹 key + httpx 出站熔断。
   要真调的用例标 `@pytest.mark.realapi`，`pytest --realapi` 单独跑
4. **阈值不写在代码里**：Annex 4 数值全在 `taxonomy.json` 的 `thresholds`
5. **不要从表象反推语义**（吃过三次亏）：
   - HFSS 从类别名正则推 → 读不懂"不含甜味"，改显式 `HFSS_VERDICTS`
   - 向量库守卫按异常类型判"谁的错" → chroma 把网络失败也包成 `ValueError`
   - 前端时间线曾想从 summary 文案找"缓存命中" → 改读 `extra` 结构化字段
6. **判不了就交出去**，不要回落到 `pool[0]`：那是把"判不了"伪装成"判出来了"
7. **份量不明不许拿 per-100g 顶替** per-serve 判据（Annex 4 的 8/23/9）
8. **`t.extra` 用 `.update()` 不用 `=`**：整体赋值会静默抹掉前面写的证据
9. **指标必须自带口径**：`split` / `pairs_arm` / `cache_backend` / `adapters` 都跟着数字走
10. **切片只描述不对比**（裁决②）：`slice_gap` 已删除，有用例钉死不许回来

---

## §5 协作机制的固定约束

1. **Claude 侧永远没外网** —— 云沙箱与 `device_bash` 都不通。所有真实 API 调用只能人类跑
2. **git 命令不在人类机器上跑** —— 会留下清不掉的 `.git/index.lock`。commit/push 始终由人类做
3. **Claude 不能删人类机器上的文件** —— 只能 `mv` 到 `_to_delete/`（已进 `.gitignore`），人类手动 `rm -rf`
4. 同步方式：Claude 打包 → `SendUserFile` → `device_commit_files` → 解包 → **md5 逐条比对**

---

## §6 下一步建议

按原排期，Day 9 是 **interrupt + 复核队列页**，但那部分骨架期已完成并跑通。
实际该做的是：

1. 人类跑完 §2 的三件事 → 把结果给新会话
2. 新会话据此补：Day 6 日报 §0.4 真实调用累计、Day 8 的 backend 对比表、消融四臂 DiD 表
3. 然后进 **Day 12（eval 跑批）**：`--split eval` 出终版指标，**只跑一次**
4. Day 12 同时处理 **D1 / OPEN-RISK-01**：strict 模式已实现但**默认未启用**
   （`CACHE_MATCH_MODE=legacy`）。启用前先看 `cache_overturn_rate`，
   有数据支撑再切 —— 没数据就改行为等于拿准确率赌直觉

### 给新会话的开场白（可直接粘）

> 项目 AdAudit v2，仓库在 `~/github/adaudit`。先读 `docs/HANDOFF.md`，
> 再读 `docs/OPEN-QUESTIONS.md` 和 `CLAUDE.md`。
> 项目知识库里有 Day3–Day8 的记录，重点看 `AdAudit_Day8_移植性_chromadb_回流.md`。
> 今日任务：<粘 Day N 的 prompt>
