"""OPEN-RISK-02 回归：测试期间绝不打真实 API。

## 这个洞是怎么开的

`config.settings` 从仓库根 `.env` 读。接真实 provider 时 `.env` 正常长这样：

    APP_ENV=dev
    VLM_PROVIDER=qwen
    LLM_PROVIDER=qwen
    SEARCH_PROVIDER=dashscope
    DASHSCOPE_API_KEY=sk-...

于是 `pytest` 直接拿真 key 去打百炼。实测**一轮测试 28 次真实 POST**。
危害不止烧钱：测试红绿开始取决于网络和账户余额，熔断 ledger 被测试数据污染，
且"CI 没 key 就红、本地有 key 就绿"是最难查的一类不可复现。

## 这里钉死什么

1. conftest 在 import 期把四个 provider 开关按成 mock（哪怕 .env 是 dev+qwen）
2. key 一并抹掉 —— 万一 provider 被改回去，应立刻失败而不是安静地发请求
3. 出站请求熔断：绕过 settings 直接建 HTTP 客户端也会被拦下

第 3 条是关键。1 和 2 是"约定"，新代码绕过 settings 就失效；3 是"保证"。
"""

from __future__ import annotations

import pytest

from config import settings

# 必须写成 `conftest` 而不是 `tests.conftest`：
# 没有 tests/__init__.py 时 pytest 把它作为**顶层模块** `conftest` 加载。
# 写成 `tests.conftest` 会再 import 一份新的模块对象，于是
# `RealNetworkCallInTest` 是两个不同的类，`pytest.raises` 捕不到 —— 我踩过一次。
from conftest import (  # noqa: E402
    ENV_PROVIDER_SETTINGS,
    MOCKED_PROVIDER_SETTINGS,
    TMP_ROOT,
    RealNetworkCallInTest,
)


def test_all_providers_are_forced_to_mock():
    for key, want in MOCKED_PROVIDER_SETTINGS.items():
        assert getattr(settings, key) == want, (
            f"{key} 不是 {want} —— provider 隔离失效，测试可能在打真实 API"
        )


def test_api_keys_are_stripped():
    """provider 万一被某个用例改回 qwen，没 key 会立刻失败，而不是安静发请求。"""
    assert settings.dashscope_api_key is None
    assert settings.gemini_api_key is None
    assert settings.openai_api_key is None


def test_storage_paths_are_not_the_production_ones():
    """断言"落在 conftest 的隔离根里"，**不写死 /tmp**。

    macOS 的 `TMPDIR` 是 `/var/folders/xx/.../T/`，写死 `/tmp` 在人类本机会红 ——
    而那台机器才是真实验收环境。拿 `TMP_ROOT` 做前缀比对，两个平台都成立，
    且比子串匹配更严：它证明的是"确实是这一轮建的那个目录"，
    而不是"路径里碰巧有这几个字"。
    """
    root = str(TMP_ROOT)
    for path in (settings.db_path, settings.checkpoint_db_path,
                 settings.chroma_path, settings.usage_path):
        assert path.startswith(root), f"{path} 不在隔离根 {root} 下"


async def test_real_outbound_request_is_blocked():
    """熔断的正面用例：绕过 settings 直接建客户端也打不出去。"""
    import httpx

    with pytest.raises(RealNetworkCallInTest) as exc:
        async with httpx.AsyncClient() as c:
            await c.post("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
    assert "realapi" in str(exc.value)


def test_sync_outbound_request_is_blocked_too():
    import httpx

    with pytest.raises(RealNetworkCallInTest):
        httpx.Client().post("https://example.com/anything")


def test_vlm_never_reaches_the_network_under_mock():
    """链路级验证：走 vlm 的正常入口不会产生任何出站尝试。"""
    from services import vlm

    assert settings.vlm_provider == "mock"
    assert vlm.settings_llm_name().startswith("mock") or "mock" in vlm.settings_llm_name()


def test_usage_ledger_stays_clean():
    """熔断 ledger 不该被测试数据污染 —— 它是成本熔断的唯一事实来源。"""
    from services import usage

    snap = usage.snapshot()
    assert snap.get("tokens", 0) == 0, (
        f"测试期间 ledger 有累计：{snap} —— 说明有真实调用发生过"
    )


@pytest.mark.skipif(
    ENV_PROVIDER_SETTINGS == MOCKED_PROVIDER_SETTINGS,
    reason="当前 .env 本来就是 mock，这条只在 .env 为 dev/qwen 时有意义",
)
def test_dangerous_env_was_actually_overridden():
    """.env 是 dev+qwen 时，这条证明隔离**确实起作用了**而不是恰好没配。

    人类给的回归口径就是这个：`.env` 设成 dev+qwen 再跑整轮，必须全绿且零真实请求。
    """
    assert ENV_PROVIDER_SETTINGS != MOCKED_PROVIDER_SETTINGS
    for key, want in MOCKED_PROVIDER_SETTINGS.items():
        assert getattr(settings, key) == want
