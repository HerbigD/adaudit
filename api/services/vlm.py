"""VLM 适配层：统一 `classify(image_path) -> Classification` 接口。

provider 由 config.settings.vlm_provider 切换：mock / gemini / qwen / openai。
结构化输出校验放在这里（而不是节点里）—— 校验失败的重试不需要走图的调度。

每个 adapter 都必须声明 `adapter` 名，并写进 Classification.adapter：
mock 与规则兜底的输出因此在 trace 里可识别，eval runner 据此拒绝跑批。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import random
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from config import settings
from graph.state import Classification
from services import taxonomy, usage

logger = logging.getLogger(__name__)

MAX_PARSE_RETRIES = 2

MOCK_ADAPTERS = {"mock-vlm", "rule-fallback", "mock-llm"}


class VLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _b64_image(path: str) -> tuple[str, str]:
    p = Path(path)
    if not p.exists():
        raise VLMError(f"图片不存在: {path}")
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    return base64.b64encode(p.read_bytes()).decode(), mime


def _extract_json(text: str) -> dict[str, Any]:
    """LLM 输出里挖 JSON —— 容忍 ```json 围栏和前后废话。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise VLMError(f"输出中没有 JSON: {text[:200]}")
    return json.loads(raw[start : end + 1])


def parse_classification(
    text: str, *, source: str, model: str | None, adapter: str
) -> Classification:
    """结构化校验：JSON 合法 + 编号在 33 类内 + 粒度标记自洽。

    校验失败抛 VLMError，由 `BaseVLM.classify` 就地重试（不走图的调度）。
    """
    data = _extract_json(text)
    tx = taxonomy.load()

    code = tx.normalize(data.get("specific_code"))
    level = data.get("leaf_vs_parent") or ("leaf" if code is not None else "parent")

    if level == "leaf" and code is None:
        raise VLMError(f"leaf 级输出但 specific_code 非法: {data.get('specific_code')!r}")

    gid = data.get("general_id")
    if code is not None:
        gid = tx.specifics[code].parent_id
    if gid not in tx.generals:
        raise VLMError(f"general_id 非法: {gid!r}")

    payload = {
        "product_name": data.get("product_name"),
        "brand": data.get("brand"),
        "name_brand_identifiable": bool(
            data.get("name_brand_identifiable", data.get("name_or_brand_legible", True))
        ),
        "ad_language": (data.get("ad_language") or "en").lower()[:5],
        "country": (data.get("country") or None),
        "general_id": gid,
        "specific_code": code,
        "candidate_codes": data.get("candidate_codes") or [],
        "leaf_vs_parent": level,
        "specific_confidence": float(data.get("specific_confidence", 0.0)),
        "general_confidence": float(
            data.get("general_confidence", data.get("specific_confidence", 0.0))
        ),
        "reasoning": data.get("reasoning", ""),
        "evidence_refs": data.get("evidence_refs") or [],
        "conflict": bool(data.get("conflict", False)),
        "source": source,
        "model": model,
        "adapter": adapter,
    }
    return Classification(**payload)


def apply_granularity_policy(c: Classification) -> Classification:
    """粒度自适应（方案 §2 末段）：叶子置信低但父类置信高 → 按父类输出。

    与 prompt 里的第 4 条规则互为保险：模型自觉降级最好，没降级则在这里兜底降级，
    这样 UI 的"确定层级 / 待定层级"和 eval 的粒度统计都有一致的判定口径。
    """
    if c.leaf_vs_parent == "parent":
        return c
    if (
        c.specific_confidence < settings.direct_threshold
        and c.general_confidence >= settings.general_fallback_threshold
    ):
        cands = list(c.candidate_codes)
        if c.specific_code is not None and c.specific_code not in cands:
            cands.insert(0, c.specific_code)
        # 补上同父类里与它构成混淆对的兄弟，给下游搜索一个明确的"在争什么"
        if c.specific_code is not None:
            s = taxonomy.get(c.specific_code)
            for sib in (s.confusable_with if s else ()):
                if sib not in cands and taxonomy.general_id_of(sib) == c.general_id:
                    cands.append(sib)
        note = (
            f"（粒度自适应：父类可定为「{c.general_category}」，"
            f"叶子在 {'/'.join(f'[{x}]' for x in cands)} 之间未定，需营养证据）"
        )
        return c.model_copy(
            update={
                "specific_code": None,
                "leaf_vs_parent": "parent",
                "candidate_codes": cands,
                "reasoning": (c.reasoning or "") + note,
            }
        )
    return c


# --------------------------------------------------------------------------- #
# Provider 接口
# --------------------------------------------------------------------------- #
class BaseVLM(ABC):
    name: str = "base"
    adapter: str = "base"

    @abstractmethod
    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        """返回模型原始文本输出。"""

    async def classify(
        self, image_path: str, *, few_shots: list[str] | None = None
    ) -> Classification:
        # 熔断放在**基类**而不是各家 adapter 里：这样任何一个 provider 实现
        # 都不可能"忘了过闸"，新增 provider 时也不用重复写。
        usage.guard(self.adapter)
        prompt = taxonomy.build_classify_prompt(few_shots)
        last: Exception | None = None
        for attempt in range(MAX_PARSE_RETRIES + 1):
            try:
                text = await self._raw_classify(image_path, prompt)
                c = parse_classification(
                    text, source="vlm", model=self.name, adapter=self.adapter
                )
                return apply_granularity_policy(c)
            except usage.BudgetExceeded:
                raise                    # 熔断不重试：重试既救不回预算，也会掩盖拒调原因
            except Exception as exc:  # noqa: BLE001 — 校验失败就地重试
                last = exc
                await asyncio.sleep(0.3 * (attempt + 1))
        raise VLMError(f"{self.name} 结构化输出校验连续失败: {last}")


class MockVLM(BaseVLM):
    """W1–W3 用：不调外部 API，按文件名 hash 稳定产出结果。

    文件名约定（demo 与集成测试靠它确定性覆盖每条路径）：
      含 `nobrand` → 无法识别名称/品牌 → 条件边① 直接转人工
      含 `parent`  → 叶子低置信 + 父类高置信 → 粒度自适应按父类输出
      含 `low`     → 低置信 + 有品牌 → 取证路径
      其他         → 高置信 → 快路径直出
    """

    name = "mock-vlm"
    adapter = "mock-vlm"

    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        await asyncio.sleep(0.6)
        tx = taxonomy.load()
        stem = Path(image_path).stem.lower()
        rng = random.Random(stem)

        # 优先从混淆对里挑，保证 mock 数据能打到 eval 关注的那几对
        pair = rng.choice(list(tx.confusing_pairs))
        code = pair[0]
        gid = tx.specifics[code].parent_id

        lang, country = "en", "IN"
        if "nobrand" in stem:
            spec, gen, ident, brand, pname = 0.42, 0.55, False, None, None
        elif "conflict" in stem:
            # 品牌名带 Conflict → mock 搜索返回互相矛盾的营养数据 → 条件边②转人工
            spec, gen, ident = 0.55, 0.86, True
            brand, pname = "ConflictBrand", "Disputed Yoghurt 200g"
            code, gid, pair = 5, 3, (5, 19)          # 落在 5/19 对上，冲突维度 = fat
        elif "degraded" in stem:
            spec, gen, ident = 0.55, 0.86, True
            brand, pname = "DegradedBrand", "Crispy Snack 45g"
            lang, country = "bn", "BD"
        elif "serving" in stem:
            spec, gen, ident = 0.55, 0.86, True
            brand, pname = "ServingBrand", "Morning Cereal 375g"
        elif "parent" in stem:
            spec, gen, ident = 0.48, 0.93, True
            brand, pname = "MockBrand", "Mock Cereal 500g"
        elif "low" in stem:
            spec, gen, ident = 0.55, 0.85, True
            brand, pname = "MockBrand", "Mock Crunchy Cereal 500g"
        else:
            spec, gen, ident = 0.93, 0.97, True
            brand, pname = "MockBrand", "Mock Product"

        return json.dumps(
            {
                "product_name": pname,
                "brand": brand,
                "name_brand_identifiable": ident,
                "ad_language": lang,
                "country": country,
                "general_id": gid,
                "specific_code": code,
                "candidate_codes": list(pair),
                "leaf_vs_parent": "leaf",
                "specific_confidence": spec,
                "general_confidence": gen,
                "reasoning": "[mock] 依据包装正面文字与产品形态判断。",
            },
            ensure_ascii=False,
        )


class GeminiVLM(BaseVLM):
    name = "gemini"
    adapter = "gemini"

    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        # TODO(W3): 可换 google-genai 官方 SDK；当前用 REST 保持零额外依赖
        import httpx

        if not settings.gemini_api_key:
            raise VLMError("缺少 GEMINI_API_KEY")
        b64, mime = _b64_image(image_path)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.gemini_model}:generateContent"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {
                    "parts": [
                        {"inline_data": {"mime_type": mime, "data": b64}},
                        {"text": "Classify this advertisement."},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.0, "response_mime_type": "application/json"},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                url, json=payload, headers={"x-goog-api-key": settings.gemini_api_key}
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]


# --------------------------------------------------------------------------- #
# DashScope（百炼）—— Day6 起的唯一真实 provider
# --------------------------------------------------------------------------- #
# JSON mode 是否可用按 (base_url, model) 记忆，避免每次调用都白试一遍
_JSON_MODE_SUPPORTED: dict[str, bool] = {}


def json_mode_state() -> dict[str, bool]:
    """探测结论快照，进日报用。"""
    return dict(_JSON_MODE_SUPPORTED)


async def dashscope_chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    want_json: bool = True,
    enable_search: bool = False,
    timeout: float = 90.0,
) -> tuple[str, dict[str, Any]]:
    """百炼 OpenAI 兼容端点的统一入口。返回 (文本, 原始响应)。

    三件事都在这里做掉，调用方不用重复：
      1) 调用前过成本熔断（`usage.guard`），超预算直接抛，不发请求
      2) JSON mode 探测：先试 `response_format`，被拒（400）则记住并降级到
         prompt 契约 + 运行时校验 —— 结论进 `json_mode_state()` 写日报
      3) 调用后按响应里的 `usage` 记账（tokens_in/out + 估算成本）
    """
    import httpx

    model = model or settings.qwen_model
    # 熔断是最外层的闸：超预算时连"有没有 key"都不必问，直接拒
    usage.guard(model)
    if not settings.dashscope_api_key:
        raise VLMError("缺少 DASHSCOPE_API_KEY")
    usage.note_model(model)

    key = f"{settings.dashscope_base_url}|{model}"
    use_json = want_json and settings.qwen_json_mode != "off" and _JSON_MODE_SUPPORTED.get(key, True)

    def build(with_json: bool) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": model,
            "temperature": 0.0,
            "messages": messages,
            # 非思考模式：分类/抽取是结构化任务，thinking 只拖延迟和 token
            "enable_thinking": settings.qwen_enable_thinking,
        }
        if with_json:
            p["response_format"] = {"type": "json_object"}
        if enable_search:
            p["enable_search"] = True
            p["search_options"] = {"forced_search": True, "enable_source": True,
                                   "search_strategy": "standard"}
        return p

    headers = {"Authorization": f"Bearer {settings.dashscope_api_key}"}
    url = f"{settings.dashscope_base_url}/chat/completions"

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=build(use_json), headers=headers)
        if r.status_code == 400 and use_json:
            # JSON mode 不被该模型支持 —— 记住，改走 prompt 契约 + 运行时校验
            logger.warning("模型 %s 拒绝 response_format，降级到 prompt 契约：%s",
                           model, r.text[:200])
            _JSON_MODE_SUPPORTED[key] = False
            r = await client.post(url, json=build(False), headers=headers)
        elif use_json and r.status_code < 400:
            _JSON_MODE_SUPPORTED.setdefault(key, True)
        if r.status_code >= 400:
            raise VLMError(f"DashScope {r.status_code}: {r.text[:400]}")
        data = r.json()

    u = data.get("usage") or {}
    usage.record(model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    return data["choices"][0]["message"].get("content") or "", data


class QwenVLM(BaseVLM):
    name = "qwen"
    adapter = "qwen"

    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        b64, mime = _b64_image(image_path)
        text, _ = await dashscope_chat(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": "Classify this advertisement."},
                    ],
                },
            ],
            model=settings.vlm_model or settings.qwen_model,
        )
        return text


class OpenAIVLM(BaseVLM):
    """方案 §7：GPT-4o 只在 eval runner 里作为对照出现。"""

    name = "openai"
    adapter = "openai"

    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        import httpx

        if not settings.openai_api_key:
            raise VLMError("缺少 OPENAI_API_KEY")
        b64, mime = _b64_image(image_path)
        payload = {
            "model": settings.openai_model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": "Classify this advertisement."},
                    ],
                },
            ],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


_REGISTRY: dict[str, type[BaseVLM]] = {
    "mock": MockVLM,
    "gemini": GeminiVLM,
    "qwen": QwenVLM,
    "openai": OpenAIVLM,
}


def get_vlm(provider: str | None = None) -> BaseVLM:
    key = provider or settings.vlm_provider
    if key not in _REGISTRY:
        raise VLMError(f"未知 VLM provider: {key}")
    return _REGISTRY[key]()


async def classify(image_path: str, *, few_shots: list[str] | None = None) -> Classification:
    return await get_vlm().classify(image_path, few_shots=few_shots)


def settings_llm_name() -> str:
    """当前纯文本 LLM 的 adapter 名 —— 抽取出来的 Evidence 要打这个标记。"""
    return settings.llm_provider


# --------------------------------------------------------------------------- #
# 纯文本 LLM（adjudicate / 报告生成共用）
# --------------------------------------------------------------------------- #
async def complete(system_prompt: str, user_prompt: str) -> str:
    """纯文本调用（抽取 / 重裁决 / 报告）。mock 下抛错，由调用方走本地兜底并打 rule-fallback。"""
    provider = settings.llm_provider
    if provider == "mock":
        raise VLMError("mock LLM: 调用方应走本地兜底逻辑")
    usage.guard(provider)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    if provider == "qwen":
        text, _ = await dashscope_chat(
            messages, model=settings.llm_model or settings.qwen_model
        )
        return text

    if provider == "openai":
        import httpx

        model = settings.openai_model
        usage.guard(model)
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": model, "temperature": 0.0, "messages": messages},
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            r.raise_for_status()
            data = r.json()
        u = data.get("usage") or {}
        usage.record(model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        return data["choices"][0]["message"]["content"]

    raise VLMError(f"complete() 暂不支持 provider={provider}")


async def probe_model() -> dict[str, Any]:
    """真跑前的可用性探测：型号在不在、能不能传图、JSON mode 支不支持。"""
    model = settings.vlm_model or settings.qwen_model
    out: dict[str, Any] = {"provider": settings.vlm_provider, "model": model}
    try:
        text, raw = await dashscope_chat(
            [{"role": "user", "content": 'Reply with exactly {"ok":true}'}],
            model=model, want_json=True, timeout=30,
        )
        out.update(reachable=True, sample=text[:80],
                   json_mode=json_mode_state().get(
                       f"{settings.dashscope_base_url}|{model}", True),
                   usage=raw.get("usage"))
    except Exception as exc:  # noqa: BLE001
        out.update(reachable=False, error=str(exc)[:400])
    return out
