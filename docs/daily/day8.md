# Day 8 日报 · 移植性修复 + chromadb 落地 + 回流实测 + 评测集导入

> 243 tests passed（此前 235，新增 8）｜Claude 侧真实调用 **0 次**，账本 **0 token**。
> 移植性两项已修并**在模拟 macOS 布局下复验**，但**最终验收要你本机确认**。

---

## §0 接口 / 字段变更申报

| 变更 | 位置 |
|---|---|
| `cache_hit_log` 新增 `cache_backend` 列 | `db.py`（走 MIGRATIONS，老库自动 ALTER） |
| `eval_samples` 新增 `audit_id` 列 + 部分唯一索引 | 同上；**回流的幂等键** |
| `stats_json.cache_overturn_detail` 新增 `by_cache_backend` | `services/report.py` |
| `stats_json.cache.backend_degrade_reason` | `services/cache_store.stats()` |
| trace `cache_lookup.extra.cache_backend` | `graph/nodes/cache_lookup.py` |
| trace `classify_initial.extra.{memory_enabled, few_shots_injected, few_shots}` | `graph/nodes/classify_initial.py` |
| trace `feedback_ingest.extra.{eval_sample_id, memory_vector_id}` | `graph/nodes/feedback_ingest.py` |
| **新配置** `MEMORY_ENABLED=true` | `config.py` |
| `vectorstore.backend()` 返回值由 `fallback` 改为 **`difflib`** | 名字要说清楚降级之后用的是什么 |
| 新增 `vectorstore.degrade_reason()` / `force_fallback()` | 对比实验要能主动选后端 |
| 新增 `VectorStoreUsageError` | 把"我们传错参数"与"环境坏了"在源头分开 |

---

## §1 两个移植性缺陷（评审指出，已修）

### 1.1 `chroma_path` 裸赋值泄漏

原来是：

```python
settings.chroma_path = str(path)   # 裸赋值，永不还原
```

跑完这条用例，**整个会话**的 `chroma_path` 都指着一个即将被删的 tmp 目录。
Linux 上它恰好排在靠后没炸，macOS 的 tmp 路径与用例顺序不同，整轮就红。

改成 `isolated_vectorstore` fixture：`monkeypatch.setattr` 自动还原，
`vectorstore.reset()` 前后各调一次保证单例不把旧路径带进带出。

**外加一道兜底**：`conftest` 新增 `_no_settings_leak` autouse fixture，
每个用例结束后无条件还原 11 个隔离关键配置。
这样将来再有人裸赋值，也毒不到后面的用例 —— 上次是库文件、这次是向量库路径，
同一类问题第二次出现，值得加一层制度而不是只修个案。

### 1.2 `/tmp` 写死

```python
assert "/tmp" in path or "adaudit-tests-" in path
```

macOS 的 `TMPDIR` 是 `/var/folders/xx/.../T/`。改成对 `conftest.TMP_ROOT` 做**前缀匹配** ——
两个平台都成立，且比子串匹配更严：它证明的是"确实是这一轮建的那个目录"。

### 1.3 复验

```
模拟 macOS：TMPDIR=/var/folders/xx/T pytest -q   → 243 passed
泄漏验证：单跑向量库那条 + provider 隔离         → 8 passed（旧代码此处会红）
反证：把断言改回写死 /tmp                        → assert '/tmp' in '/var/folders/xx/T/...' ❌
```

⚠️ **这只是模拟。真实 macOS 上的 `pytest -q` 请你亲自跑一遍确认** —— 那才是验收环境。

---

## §2 D6 落地：chromadb

### 装上了，但它有个隐藏的网络依赖

```
chromadb 1.5.9 已安装
```

**装上不等于能用**：默认 embedding function（ONNXMiniLM_L6_V2）
**首次使用要联网下载 ~80MB 模型**。沙箱里 `ProxyError: 403`。
你的机器有网，首次跑会慢一下（下载），之后走本地缓存。
但 Day 13 做 Docker 时要注意：镜像里得预置模型或首启放行网络。

### 降级现在会响

之前降级是**静默**的，而这件事的后果被严重低估了：

```
命中得分 = 0.55 品牌 + 0.20 名称重叠 + 0.25 语义，阈值 0.82
0.55 + 0.20 = 0.75 < 0.82
```

**语义分是每一次命中的必要条件。** 所以降级不是"检索差一点"，是**缓存直接失效**，
而表现却像"这批广告恰好没命中过"。现在降级一律打醒目 warning，
`backend()` 如实返回，且这个值进 trace / `cache_hit_log` / `stats_json`。

### 守卫写了三版才对，两处值得记

**① 守卫包错了范围。** 第一版只包 `upsert`/`query`，
但 chroma 在 `get_or_create_collection` 时就构造 embedding function 并联网。
异常从那里漏出去，被调用方 `cache_store` 的 `except Exception: pass`
（"向量库不可用不应阻断主链路"）吞掉 —— **没降级、没 warning、什么都没写，
`backend()` 仍报 chroma**。缓存命中率静默塌到 0.75 以下而日志一片安静。

**② 用异常类型判"谁的错"是错的。** 第二版按类型分流：
`ValueError`/`TypeError` 视为调用方 bug 原样抛出，其余降级。
结果 chroma 把 embedding 下载失败**也包成 `ValueError`** 从
`_validate_and_prepare_upsert_request` 抛出 —— 最该降级的那种失败被当成了"我们传错参数"。

这和 HFSS 正则那次同类：**不要从表象反推语义**。
现在改成显式校验：我们自己的参数由 `_validate_upsert` 校验并抛 `VectorStoreUsageError`，
从 chroma 出来的一律当环境失败。

**③ 还有个陈旧句柄问题**：降级后，已经拿在手里的 collection 句柄仍指着坏掉的 chroma。
对会抛错的操作没关系（走重试），但 `count()` 不需要 embedding、**不抛错**，
于是安静地返回 chroma 里的空数据。实测：数据写进了 fallback，旧句柄 `count()` 报 0、
新句柄报 1。已改为每次操作前解析当前后端。

### backend 对比（difflib 一侧已测，chroma 一侧待你跑）

12 条档案 + 12 条查询（6 exact / 4 variant / **2 decoy 跨类别近名**）：

| cache_backend | hit_rate | 同产品召回 | **近名误命中** | 平均分 |
|---|---|---|---|---|
| **difflib** | 1.0 | 1.0 | **1.0** ❌ | 0.9683 |
| chroma | 待你跑 | | | |

**近名误命中 1.0 —— 这就是 OPEN-RISK-01 的量化值。** 两条 decoy 全部误命中：

```
decoy  Amul Double Toned Milk 1L        score=0.9293  命中 ❌   （档案是 Amul Toned Milk 1L，跨 5/19）
decoy  Maggi Atta Instant Noodles 70g   score=0.9141  命中 ❌   （档案是 Maggi Masala Instant Noodles）
```

difflib 是字符相似度，`Double ` 这 7 个字符对它几乎没有区分度。
**这也说明 strict 模式确实必要** —— 它对这两条都会否决（`double toned` / `atta` 都在维度词表里）。

你那边跑 chroma 一侧：

```bash
cd api
python3 scripts/day8_backend_compare.py --out ../data/day8/backend_chroma.json
python3 scripts/day8_backend_compare.py --report ../data/day8/backend_difflib.json ../data/day8/backend_chroma.json
```

**从今天起，任何带缓存指标的结果都必须注明 `cache_backend`** ——
两种后端下的命中率放在一起比没有意义。

---

## §3 三处回流实测

### 幂等：原来**不幂等**，已修

`memory.remember()` 每次调用都 `add_eval_sample` 生成新 uuid ——
同一次审计回流两遍就在 eval 集里留两行。

`resume` 会重新驱动整张图，人工也可能改主意再裁一次，**重复是常态不是意外**。
后果不是"多几行"：eval 集是拿去算准确率的，重复样本等于**给某几张图加权**。

修法：`audit_id` 作幂等键，两处都按它去重 ——
`eval_samples` 加 `audit_id` 列 + 部分唯一索引（人工导入的金标没有 audit_id，
所以是 `WHERE audit_id IS NOT NULL` 的**部分**索引），向量 id 直接用 audit_id。

幂等键选 `audit_id` 而不是 `image_path`：同一张图可以被审计多次（换模型重跑），
那是两条独立的人工裁定，不该互相覆盖。

### 三处可查

`feedback_ingest` 的 trace 现在带 `eval_sample_id` / `memory_vector_id` / `cache_write`，
验收时不用猜"到底写没写"。8 条用例覆盖：

| 用例 | 验的是 |
|---|---|
| `test_remember_is_idempotent_by_audit_id` | 三次调用返回同一 id，eval 只一行 |
| `test_repeated_feedback_updates_in_place...` | 改主意时**改写**而非追加 |
| `test_memory_vector_id_is_the_audit_id...` | 向量库那一处同样幂等 |
| `test_without_audit_id_it_falls_back...` | 明确边界：不传 audit_id 就是旧行为 |
| `test_all_three_sinks_are_written_and_queryable` | 三处都能查回来 |
| `test_human_verdict_supersedes_an_auto_archive` | 单向棘轮，auto 盖不回去 |

### MEMORY_ENABLED 开关

开关放在 `memory.retrieve()` 里返回空，**不在调用点加 if** ——
调用点加 if 会让两臂走不同代码，比出来的差异就不知道是开关造成的还是路径造成的。

注入证据落 trace：`memory_enabled` / `few_shots_injected` / `few_shots`（内容前 160 字）。
只记条数不够 —— 两次跑批条数相同但内容不同，看起来会一模一样。

```
开：memory_enabled=True   few_shots_injected>=1   few_shots=[...]
关：memory_enabled=False  few_shots_injected=0    （无 few_shots 键）
```

### 顺手修的一个静默覆盖

`classify_initial` 里成功路径是 `t.extra = {...}` **整体赋值**，
会把前面写进去的 few-shot 注入证据**静默抹掉**。改成 `.update()`。
这类"后写覆盖先写"在 trace 上尤其难查：字段不见了不会报错，只是验收时发现证据不在。

---

## §4 eval_samples 导入 300 行

```
导入 300 行（source=split:eval）
对账：CSV 300 行 / 库 300 行
  缺失 0｜金标不一致 0｜库里多出 0
  落在混淆对上的：214 行
  gold 分布 top5：25→70, 20→55, 14→32, 1→21, 15→18
✅ 逐行对账一致
```

金标取 **`gold_specific`**（已应用 22→32），不是 `gold_code_raw`。
`source` 用 `split:eval` 而不是笼统的 `manual_label` ——
这样人工回流（`human_feedback`）与不同切分的导入在库里分得开，清理时不会误删回流数据。

### 对账不是走过场，我做了反证

条数对上不代表内容对上：用错列会让 gold=22 那批全判错，而**行数完全一致**。
所以人为把 5 行改成 `gold_code_raw` 再跑对账：

```
✗ Pakistan/1631023715893.jpg: CSV=32 库=22
... （共 5 行）
❌ 对账不一致
```

抓得住。（eval 300 里有 5 张属于那 80 张 gold=22。）

---

## §5 又一个只在真实机器上出现的 bug（已修）

导入脚本第一次跑直接炸：

```
sqlite3.OperationalError: no such column: audit_id
```

`idx_eval_audit` 索引建在**新增列**上，却写在 `SCHEMA` 里和 `CREATE TABLE` 一起执行。
新库没问题（CREATE TABLE 已含该列）；**老库直接炸** ——
`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，列还没 ALTER 上去，索引就先建了。

**而测试用的永远是新库，所以这个 bug 只会在真实机器上出现** —— 和今天修的两个移植性
缺陷是同一类：测试环境比真实环境"干净"，干净掩盖了问题。

修法：抽出 `POST_MIGRATION_SCHEMA`，在 ALTER 之后执行。
已用一个"没有 audit_id 列的老库"实测迁移成功且旧数据不丢。

---

## §6 消融抽样清单（等你过目 → 你本机跑）

```bash
python3 -m eval.runner --ablation --dry-run --tier-quota "definitional=60,definitional_compositional=30"
```

| 档 | 对 | n | 占本档 |
|---|---|---|---|
| definitional | 2/12 | 12 | 20% |
| definitional | 3/18 | 12 | 20% |
| definitional | 5/19 | 12 | 20% |
| definitional | 7/24 | 12 | 20% |
| definitional | 8/23 | 12 | 20% |
| compositional | 1/13 · 4/17 · 6/15 · 7/27 · 11/25 · 14/16 | 各 4 | 13% |
| compositional | 31/32 · 33/34 | 各 3 | 10% |

- 混淆组 90 / 对照组 90（LK 63 / IN 14 / PK 9 / BD 4）
- `overlap_with_held_out: 0`（代码断言，不通过抛异常）｜`seed: 20260731`
- 支撑量：**B−A n=60 ✅**｜**B2−B n=30 ✅**｜**C−B2 n=0 ❌**（Tier 3 尚无经验对，如实写）

确认后：

```bash
python3 -m eval.runner --ablation --tier-quota "definitional=60,definitional_compositional=30"
```

180 条 × 4 臂 = 720 次真实调用，约 $1–2。**四臂 DiD 对比表等你跑完我来填。**

---

## §7 成本

| 项 | 数 |
|---|---|
| Claude 侧真实调用 | **0 次** |
| 账本累计 | **0 token** |
| 测试触发的出站请求 | **0 次** |

---

## §8 验收对照

| 验收项 | 结果 |
|---|---|
| macOS 整轮全绿 | ⏳ 模拟 TMPDIR 下 243 passed；**待你本机确认** |
| 两种 backend 命中率对比有数 | ⚠️ difflib 一侧已测（近名误命中 **1.0**）；chroma 一侧待你跑（沙箱无网下不了模型） |
| 三处回流幂等可查 | ✅ 8 例；原来**不幂等**，已修 |
| eval_samples 300 行对账一致 | ✅ 含反证 |
| 消融清单 | ✅ 见 §6 |
| 四臂表 | ⏳ 等你跑 |

---

## §9 卡点与偏差

1. **chroma 一侧的对比数我跑不出来** —— embedding 模型要联网下载，沙箱 403。
   脚本已就绪，一条命令的事
2. **macOS 验收只能你做** —— 我只能模拟 `TMPDIR`，模拟不等于真机
3. **`memory.remember` 的幂等是今天新加的行为**，不是修 bug 前就有的。
   如果你希望保留"每次回流都留一行"的旧语义，说一声，我加开关

---

## §10 明日建议

Day 8 暴露的三个 bug（`chroma_path` 泄漏、`/tmp` 写死、迁移顺序）有同一个根因：
**测试环境比真实环境干净**。建议在 Day 13 的 Docker 验收里加一条
"用一份**老库**启动一次"，把迁移路径也纳入常规验收 —— 这类 bug 只在那条路径上出现。
