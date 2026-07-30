"""VLM 适配层：统一 `classify(image_path) -> Classification` 接口。

provider 由 config.settings.vlm_provider 切换：mock / gemini / qwen / openai。
结构化输出校验放在这里（而不是节点里）—— 校验失败的重试不需要走图的调度。
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import random
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from config import settings
from graph.state import Classification
from services import taxonomy

MAX_PARSE_RETRIES = 2


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


def parse_classification(text: str, *, source: str, model: str | None) -> Classification:
    """结构化校验：JSON 合法 + specific_code 在 33 类内，否则抛错由调用方重试。"""
    data = _extract_json(text)
    code = data.get("specific_code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if not taxonomy.is_valid(code):
        raise VLMError(f"specific_code 非法: {code!r}")
    data["specific_code"] = code
    general = taxonomy.general_of(code)
    # 大类以细类为准回填，避免模型自造大类名
    data["general_category"] = general
    data.setdefault("general_confidence", data.get("specific_confidence", 0.5))
    return Classification(**{**data, "source": source, "model": model})


# --------------------------------------------------------------------------- #
# Provider 接口
# --------------------------------------------------------------------------- #
class BaseVLM(ABC):
    name: str = "base"

    @abstractmethod
    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        """返回模型原始文本输出。"""

    async def classify(
        self, image_path: str, *, few_shots: list[str] | None = None
    ) -> Classification:
        prompt = taxonomy.build_classify_prompt(few_shots)
        last: Exception | None = None
        for attempt in range(MAX_PARSE_RETRIES + 1):
            try:
                text = await self._raw_classify(image_path, prompt)
                return parse_classification(text, source="vlm", model=self.name)
            except Exception as exc:  # noqa: BLE001 — 校验失败就地重试
                last = exc
                await asyncio.sleep(0.3 * (attempt + 1))
        raise VLMError(f"{self.name} 结构化输出校验连续失败: {last}")


class MockVLM(BaseVLM):
    """W1–W2 用：不调外部 API，按文件名 hash 稳定产出一个结果。

    文件名里带 'low' → 低置信（走搜索链路）；带 'nobrand' → 无法识别品牌（直接转人工）。
    这样 demo 和集成测试可以确定性地覆盖三条路由。
    """

    name = "mock-vlm"

    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        await asyncio.sleep(0.6)
        stem = Path(image_path).stem.lower()
        rng = random.Random(stem)
        code = rng.choice([2, 12, 5, 19, 8, 23, 25, 32])
        if "nobrand" in stem:
            conf, legible, brand, pname = 0.42, False, None, None
        elif "low" in stem:
            conf, legible = 0.55, True
            brand, pname = "MockBrand", "Mock Crunchy Cereal 500g"
        else:
            conf, legible = 0.93, True
            brand, pname = "MockBrand", "Mock Product"
        return json.dumps(
            {
                "product_name": pname,
                "brand": brand,
                "general_category": taxonomy.general_of(code),
                "specific_code": code,
                "specific_confidence": conf,
                "general_confidence": min(0.99, conf + 0.3),
                "reasoning": "[mock] 依据包装正面文字与产品形态判断。",
                "alternative_code": rng.choice([c for c in taxonomy.CODES if c != code]),
                "name_or_brand_legible": legible,
            },
            ensure_ascii=False,
        )


class GeminiVLM(BaseVLM):
    name = "gemini"

    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        # TODO(W3): pip install google-genai；改用官方 SDK 的 async client
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


class QwenVLM(BaseVLM):
    name = "qwen"

    async def _raw_classify(self, image_path: str, system_prompt: str) -> str:
        # DashScope 兼容 OpenAI 协议
        import httpx

        if not settings.dashscope_api_key:
            raise VLMError("缺少 DASHSCOPE_API_KEY")
        b64, mime = _b64_image(image_path)
        payload = {
            "model": settings.qwen_model,
            "temperature": 0.0,
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
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


class OpenAIVLM(BaseVLM):
    """方案 §7：GPT-4o 只在 eval runner 里作为对照出现。"""

    name = "openai"

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


# --------------------------------------------------------------------------- #
# 纯文本 LLM（adjudicate / 报告生成共用）
# --------------------------------------------------------------------------- #
async def complete(system_prompt: str, user_prompt: str) -> str:
    """TODO(W4): 接真实 provider；mock 下由调用方走自己的兜底逻辑。"""
    provider = settings.llm_provider
    if provider == "mock":
        raise VLMError("mock LLM: 调用方应走本地兜底逻辑")
    import httpx

    if provider == "openai":
        key, url, model = settings.openai_api_key, "https://api.openai.com/v1", settings.openai_model
    elif provider == "qwen":
        key, url, model = (
            settings.dashscope_api_key,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            settings.qwen_model,
        )
    else:
        raise VLMError(f"complete() 暂不支持 provider={provider}")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{url}/chat/completions",
            json={
                "model": model,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            headers={"Authorization": f"Bearer {key}"},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
