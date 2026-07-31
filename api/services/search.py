"""联网搜索：查询构造 + 预算 / 超时 / 重试。

Day5 §1 原则 1：**搜索链路的失败 80% 是查询词烂，不是工具烂**，所以查询构造是本文件的重点。

语言策略（Day5 §3）：查询语言跟随 `initial.ad_language`，识别不出或为南亚本土语言时
**一律回退英文** —— 四国食品营养信息的公开网页绝大多数是英文。
**禁止用中文构造查询**，禁止假设中文信息源（`assert_no_cjk` 会把这条钉死）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from config import settings

SearchStatus = Literal["ok", "no_result", "timeout", "budget_exceeded", "error"]

# 本土文字 Unicode 区段：天城文(hi/mr) / 孟加拉文(bn) / 阿拉伯文(ur) / 僧伽罗文(si) / 泰米尔文(ta)
NATIVE_SCRIPTS = re.compile(
    r"[ऀ-ॿঀ-৿؀-ۿ඀-෿஀-௿]+"
)
# 中日韩统一表意文字 —— 出现即视为构造错误
CJK = re.compile(r"[一-鿿぀-ヿ]")

# 只含类目词的泛查询会召回品类平均值，污染 Evidence（Day5 §3 规则 3）
GENERIC_CATEGORY_WORDS = {
    "milk", "yoghurt", "yogurt", "juice", "water", "cereal", "biscuit", "biscuits",
    "noodles", "chips", "snack", "snacks", "chocolate", "candy", "bread", "rice",
    "oil", "cheese", "drink", "beverage", "soda", "tea", "coffee",
}


# --------------------------------------------------------------------------- #
# 数据驱动的词表（新增国家只改 JSON）
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _terms() -> dict[str, Any]:
    return json.loads(
        (Path(settings.category_terms_path)).read_text(encoding="utf-8")
    )


def keep_terms(country: str | None) -> list[str]:
    t = _terms()["keep_terms"]
    return sorted(set(t.get("_global", [])) | set(t.get((country or "").upper(), [])))


def stopwords() -> set[str]:
    return {w.lower() for w in _terms()["stopwords"]["en"]}


# --------------------------------------------------------------------------- #
# 查询构造
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Query:
    text: str
    tier: int                     # 1 = brand+product，2 = 官网，3 = 去品牌（裁决降权）
    lang: str = "en"
    rationale: str = ""


def split_script(text: str | None) -> tuple[str, str]:
    """拆成 (拉丁部分, 本土文字部分)。品牌常见 "Maggi मैगी" 这种双写。"""
    if not text:
        return "", ""
    native = " ".join(NATIVE_SCRIPTS.findall(text)).strip()
    latin = NATIVE_SCRIPTS.sub(" ", text)
    latin = re.sub(r"\s+", " ", latin).strip()
    return latin, native


def strip_marketing(name: str, country: str | None) -> str:
    """剥离英文营销修饰词，但**保留**本土高频品类词（它们携带判定信息）。"""
    keep = {t.lower() for t in keep_terms(country)}
    # 先保护多词短语（toned milk / double toned milk）
    protected: list[str] = []
    working = name
    for phrase in sorted(keep, key=len, reverse=True):
        if " " in phrase and phrase in working.lower():
            idx = working.lower().index(phrase)
            protected.append(working[idx : idx + len(phrase)])
            working = working[:idx] + f" __KEEP{len(protected) - 1}__ " + working[idx + len(phrase) :]

    stops = stopwords()
    kept_tokens = []
    for tok in working.split():
        bare = re.sub(r"[^\w]", "", tok).lower()
        if bare in stops and bare not in keep:
            continue
        kept_tokens.append(tok)
    out = " ".join(kept_tokens)
    for i, phrase in enumerate(protected):
        out = out.replace(f"__KEEP{i}__", phrase)
    return re.sub(r"\s+", " ", out).strip()


def shorten(name: str, country: str | None, limit: int = 20) -> str:
    """product_name >20 字符时截到核心品名（Day5 §3 规则 1）。

    截断以词为单位，且**优先保住**本土品类词 —— 截掉 "toned" 等于把 5/19 的判据扔了。
    """
    cleaned = strip_marketing(name, country)
    if len(cleaned) <= limit:
        return cleaned

    # 多词品类短语（"instant noodles"）要整体保住 —— 只截到 "instant" 等于把判据丢了一半
    lowered = cleaned.lower()
    protected_tokens: set[str] = set()
    for phrase in keep_terms(country):
        if phrase.lower() in lowered:
            protected_tokens |= {w for w in phrase.lower().split()}

    out: list[str] = []
    for tok in cleaned.split():
        bare = re.sub(r"[^\w]", "", tok).lower()
        candidate = " ".join(out + [tok])
        if len(candidate) > limit and out and bare not in protected_tokens:
            break
        out.append(tok)
    return " ".join(out) or cleaned[:limit]


def is_generic(query_core: str) -> bool:
    """只含类目词 → 泛查询，禁止发出。"""
    tokens = [t for t in re.findall(r"[a-z]+", query_core.lower()) if len(t) > 1]
    return bool(tokens) and all(t in GENERIC_CATEGORY_WORDS for t in tokens)


def assert_no_cjk(queries: list[Query]) -> None:
    bad = [q.text for q in queries if CJK.search(q.text)]
    if bad:
        raise ValueError(f"查询词包含中文字符（南亚数据域禁止）: {bad}")


def build_queries(
    brand: str | None,
    product_name: str | None,
    *,
    ad_language: str = "en",
    country: str | None = None,
) -> list[Query]:
    """构造有序查询序列 Q1→Q3，按预算逐条执行、命中即停。

    Q1  "{brand} {product_name} nutrition facts"
    Q2  "{brand} {product_name} official site"     → 进官网找 nutrition 页
    Q3  "{product_name} nutrition"                 → 去品牌，防 OCR 认错品牌（tier=3 降权）
    """
    brand_latin, brand_native = split_script(brand)
    name_latin, name_native = split_script(product_name)
    name_core = shorten(name_latin, country) if name_latin else ""

    queries: list[Query] = []
    anchor = " ".join(x for x in (brand_latin, name_core) if x).strip()

    if anchor and not is_generic(anchor):
        queries.append(
            Query(f"{anchor} nutrition facts", tier=1, rationale="品牌+品名，首选")
        )
        queries.append(
            Query(f"{anchor} official site", tier=2, rationale="进官网找 nutrition 页")
        )

    # Q3：去品牌。本土文字原文允许出现在这里（Daraz 类本土电商常有本土文字标题）
    if name_core and not is_generic(name_core):
        tail = f" {name_native}" if name_native else (f" {brand_native}" if brand_native else "")
        queries.append(
            Query(
                f"{name_core} nutrition{tail}".strip(),
                tier=3,
                rationale="去品牌，防 OCR 认错品牌；本土文字仅在此层出现",
            )
        )
    elif not queries and (brand_latin or brand_native):
        # 只认出品牌，没认出品名：仍值得一试，但按 tier 3 降权
        probe = brand_latin or brand_native
        queries.append(Query(f"{probe} nutrition", tier=3, rationale="仅品牌锚点"))

    queries = queries[: settings.search_max_queries]
    assert_no_cjk(queries)
    return queries


# --------------------------------------------------------------------------- #
# 预算与执行
# --------------------------------------------------------------------------- #
@dataclass
class SearchBudget:
    max_queries: int = field(default_factory=lambda: settings.search_max_queries)
    per_query_timeout: float = field(default_factory=lambda: settings.search_timeout_s)
    total_timeout: float = field(default_factory=lambda: settings.search_total_timeout_s)
    max_retries: int = field(default_factory=lambda: settings.search_retries)


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str


@dataclass
class QueryRecord:
    """一条查询的执行留档 —— 失败案例归因时要知道"死在哪条查询"。"""

    text: str
    tier: int
    ms: int
    results: int
    status: str
    attempts: int = 1


@dataclass
class SearchOutcome:
    status: SearchStatus = "no_result"
    hits: list[SearchHit] = field(default_factory=list)
    hit_query: Query | None = None
    queries_used: int = 0
    records: list[QueryRecord] = field(default_factory=list)
    detail: str = ""
    logs: list[str] = field(default_factory=list)

    @property
    def tier(self) -> int:
        return self.hit_query.tier if self.hit_query else 0


SEARCH_SYSTEM_PROMPT = """You are a web research assistant for a nutrition-policy audit system.
Search the web for the requested product and report the pages that actually carry a
nutrition / ingredients panel.

Rules:
1. Report ONLY pages you actually retrieved. Never invent a URL.
2. Prefer the brand's official site, then nutrition databases, then major e-commerce
   product pages. Skip news articles, videos, forums and blogs.
3. `snippet` must quote the nutrition figures verbatim from the page when present
   (e.g. "Per 100 g: Energy 1580 kJ, Fat 4.2 g, Sugars 24.6 g"). Do not paraphrase numbers,
   do not convert units, do not fill in values that are absent.
4. Pages may be in English, Hindi, Bengali, Urdu, Sinhala or Tamil — quote whichever
   language carries the panel.
5. Return at most 5 results.

Output strict JSON only:
{"results":[{"title":string,"url":string,"snippet":string}]}"""


async def _search_once(query: str) -> list[SearchHit]:
    """单次查询。后端由 `SEARCH_PROVIDER` 决定，上面的预算/重试/降级逻辑与后端无关。"""
    provider = settings.search_provider
    if provider == "mock" or settings.app_env == "mock":
        return _mock_search(query)
    if provider == "dashscope":
        return await _dashscope_search(query)
    raise NotImplementedError(f"未知 search_provider: {provider}")


def _hits_from_search_info(raw: dict[str, Any]) -> list[SearchHit]:
    """从百炼返回的 `search_info.search_results` 取真实 URL。

    这是**唯一可信的 URL 来源** —— 正文里模型自己写的 URL 可能是编的，
    而 search_info 里的条目是检索器实际访问过的页面。
    """
    info = (raw.get("search_info") or {}) if isinstance(raw, dict) else {}
    if not info:
        choices = raw.get("choices") or []
        if choices:
            info = (choices[0].get("message") or {}).get("search_info") or {}
    out: list[SearchHit] = []
    for item in (info.get("search_results") or []):
        url = item.get("url") or item.get("link") or ""
        if url:
            out.append(SearchHit(
                url=url,
                title=item.get("title") or item.get("site_name") or url,
                snippet=item.get("snippet") or "",
            ))
    return out


async def _dashscope_search(query: str) -> list[SearchHit]:
    """百炼内置联网。

    取证策略是**两路合并**：
      · `search_info.search_results` —— 检索器真实访问过的页面，URL 可信
      · 模型正文里的 JSON —— 带回了页面上的营养数字原文（snippet），抽取阶段要用
    以 URL 为键合并：URL 只认 search_info 的，snippet 优先用模型摘出来的那份。
    URL 对不上的正文条目一律丢弃 —— 宁可少一条证据，也不能让编造的链接进 trace。
    """
    from services import vlm  # 延迟导入，避免 search ↔ vlm 循环

    text, raw = await vlm.dashscope_chat(
        [
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": f"Search query: {query}"},
        ],
        model=settings.llm_model or settings.qwen_model,
        want_json=True,
        enable_search=True,
        timeout=settings.search_timeout_s * 3,
    )

    retrieved = _hits_from_search_info(raw)
    by_url = {h.url.rstrip("/"): h for h in retrieved}

    try:
        payload = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        for item in payload.get("results", [])[: settings.search_hits_per_query]:
            url = (item.get("url") or "").rstrip("/")
            if url in by_url:
                by_url[url] = SearchHit(
                    url=by_url[url].url,
                    title=item.get("title") or by_url[url].title,
                    snippet=item.get("snippet") or by_url[url].snippet,
                )
    except Exception:  # noqa: BLE001 — 正文不是 JSON 也无所谓，search_info 已够用
        pass

    return list(by_url.values())[: settings.search_hits_per_query]


def _mock_search(query: str) -> list[SearchHit]:
    """mock 搜索：按查询词里的关键词决定返回什么，让集成测试可确定性覆盖每条分支。"""
    q = query.lower()
    if "nobrand" in q:
        return []
    if "conflictbrand" in q:
        # 同一 nutrient 跨来源大分歧 → §6 冲突判定
        return [
            SearchHit(
                "https://brandsite.example/product",
                "[mock] ConflictBrand Yoghurt — Official",
                "Nutrition per 100 g: Energy 260 kJ, Fat 1.2 g, Sugars 4.1 g, "
                "Fibre 0.2 g, Sodium 45 mg, Protein 3.4 g.",
            ),
            SearchHit(
                "https://amazon.in/dp/B0MOCK",
                "[mock] ConflictBrand Yoghurt 200g",
                "Nutritional Information per 100 g: Fat 9.8 g, Sugars 18.7 g, "
                "Fibre 0.1 g, Sodium 41 mg, Protein 3.1 g.",
            ),
        ]
    if "degradedbrand" in q:
        # 有页面但没有营养面板 → 降级证据
        return [
            SearchHit(
                "https://daraz.com.bd/products/mock",
                "[mock] DegradedBrand Snack — Daraz Bangladesh",
                "Crispy savoury snack, 45g pack. Best before 6 months. "
                "Made in Bangladesh. No nutrition panel on this listing.",
            )
        ]
    if "servingbrand" in q:
        # per-serving 且缺份量 → normalized 应为 None
        return [
            SearchHit(
                "https://bigbasket.com/pd/mock",
                "[mock] ServingBrand Cereal",
                "Nutrition per serving: Sugars 9 g, Fat 1.5 g, Fibre 2 g, Sodium 120 mg.",
            )
        ]
    return [
        SearchHit(
            "https://brandsite.example/products/mock",
            "[mock] MockBrand product — Nutrition Information",
            "Nutrition per 100 g: Energy 1580 kJ, Fat 4.2 g, Sugars 24.6 g, "
            "Fibre 3.1 g, Sodium 168 mg, Protein 7.5 g.",
        )
    ]


async def search_product(
    brand: str | None,
    product_name: str | None,
    *,
    ad_language: str = "en",
    country: str | None = None,
    budget: SearchBudget | None = None,
    on_log=None,
) -> SearchOutcome:
    """逐条执行 Q1→Q3，命中即停。失败**不抛异常**，作为正常路由结果返回。"""
    budget = budget or SearchBudget()
    outcome = SearchOutcome()
    started = time.perf_counter()

    try:
        queries = build_queries(
            brand, product_name, ad_language=ad_language, country=country
        )
    except ValueError as exc:
        outcome.status, outcome.detail = "error", str(exc)
        return outcome

    if not queries:
        outcome.status = "no_result"
        outcome.detail = "没有可用的品牌/产品名锚点，或只剩泛类目词"
        return outcome

    # 失败原因优先级：真实故障（超时/网络错误）盖过"预算用完"这种记账结果，
    # 否则 trace 上看到的永远是 budget_exceeded，归因时找不到真凶。
    salience = {"error": 3, "timeout": 3, "budget_exceeded": 2, "no_result": 1}

    def demote_to(status: SearchStatus, detail: str) -> None:
        if salience.get(status, 0) > salience.get(outcome.status, 0):
            outcome.status, outcome.detail = status, detail

    for q in queries:
        if outcome.queries_used >= budget.max_queries:
            demote_to("budget_exceeded", f"查询预算 {budget.max_queries} 次已用尽")
            break
        if time.perf_counter() - started > budget.total_timeout:
            outcome.status = "timeout"
            outcome.detail = f"整链路超时 >{budget.total_timeout}s"
            break

        msg = f"正在搜索「{brand or product_name}」营养成分…（Q{q.tier}）"
        outcome.logs.append(msg)
        if on_log:
            await on_log(msg)

        attempts, q_started, hits, status = 0, time.perf_counter(), [], "no_result"
        # 只对超时/网络错误重试，不对"无结果"重试
        while attempts <= budget.max_retries:
            attempts += 1
            outcome.queries_used += 1
            try:
                hits = await asyncio.wait_for(
                    _search_once(q.text), timeout=budget.per_query_timeout
                )
                status = "ok" if hits else "no_result"
                break
            except asyncio.TimeoutError:
                status = "timeout"
                if attempts > budget.max_retries:
                    break
            except Exception as exc:  # noqa: BLE001
                status = f"error:{type(exc).__name__}"
                break

        outcome.records.append(
            QueryRecord(
                text=q.text,
                tier=q.tier,
                ms=int((time.perf_counter() - q_started) * 1000),
                results=len(hits),
                status=status,
                attempts=attempts,
            )
        )

        if hits:
            outcome.hits, outcome.hit_query, outcome.status = hits, q, "ok"
            break
        if status == "timeout":
            demote_to("timeout", f"查询超时: {q.text}")
        elif status.startswith("error"):
            demote_to("error", status)
        else:
            demote_to("no_result", "全部查询无结果")

    if outcome.status == "ok" and on_log:
        await on_log(f"搜索命中 {len(outcome.hits)} 条结果（Q{outcome.tier}）")
    return outcome
