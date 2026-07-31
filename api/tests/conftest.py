"""测试隔离：全新库文件 + 强制 mock provider + 出站请求熔断。

## 两条隔离，缺一不可

### 1. 存储隔离（Day6 发现）

在这之前测试直接写 `data/adaudit.db`（生产同一个文件）。后果是跑批顺序会改变结论：

- `test_conflict_path_goes_human_and_supersedes_cache` 自己会 upsert 一条
  `ConflictBrand` 档案，整轮跑完这条档案留在库里。
- 再单独跑这一个文件时 `cache_lookup` 命中了上一轮的残留，
  `search_status` 从 `conflict` 变成 `cache`，`route_2` 成了 `direct_verified`
  —— **测试失败，但代码没错**。

这类"整轮绿、单跑红"最难查，而且方向反过来更危险：
一个真实的缓存逻辑回归，可能因为库里恰好有条旧档案而被掩盖成绿色。

### 2. Provider 隔离（OPEN-RISK-02，评审发现）

存储隔离修好后还剩一个更贵的洞：**`settings` 是从仓库根 `.env` 读的**。
`.env` 一旦是 `APP_ENV=dev` + `VLM_PROVIDER=qwen`（接真实 API 时的正常配置），
`pytest` 就会拿真 key 去打百炼。实测**一轮测试发出 28 次真实 POST**。

这不只是烧钱：
- 测试结果开始依赖网络与账户余额，红绿失去意义
- 熔断 ledger 被测试数据污染，`data/usage.json` 的累计数不再可信
- CI 里没 key 会红，本地有 key 会绿 —— 最糟的一种不可复现

所以本文件在**任何测试模块 import 之前**（conftest 由 pytest 最先加载）
把四个 provider 开关全部按死成 mock，和存储隔离同一位置、同一理由。

### 出站请求熔断

只改 settings 是"约定"，不是"保证"—— 新写的代码只要绕过 settings 直接建客户端，
洞就又开了。所以再加一层 `httpx` 补丁：测试期间任何真实出站请求**直接抛异常**，
异常信息告诉你该怎么办。要真打 API 的测试显式标 `@pytest.mark.realapi`。

回归口径：把 `.env` 设成 `dev` + `qwen` 再跑整轮，必须全绿且 `usage.json` 零累计。
`test_provider_isolation.py` 把这条钉死了。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from config import settings

# --------------------------------------------------------------------------- #
# 存储隔离 —— import 期生效
# --------------------------------------------------------------------------- #
_TMP = Path(tempfile.mkdtemp(prefix="adaudit-tests-"))

settings.db_path = str(_TMP / "adaudit.db")
settings.checkpoint_db_path = str(_TMP / "checkpoints.db")
settings.chroma_path = str(_TMP / "chroma")
settings.usage_path = str(_TMP / "usage.json")
Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Provider 隔离 —— 同上，import 期生效（OPEN-RISK-02）
# --------------------------------------------------------------------------- #
MOCKED_PROVIDER_SETTINGS = {
    "app_env": "mock",
    "vlm_provider": "mock",
    "llm_provider": "mock",
    "search_provider": "mock",
}
# 记下 .env 里原本是什么，供 test_provider_isolation 断言"确实覆盖掉了危险配置"
ENV_PROVIDER_SETTINGS = {k: getattr(settings, k) for k in MOCKED_PROVIDER_SETTINGS}

for _k, _v in MOCKED_PROVIDER_SETTINGS.items():
    setattr(settings, _k, _v)

# key 也一并抹掉：provider 万一被某个用例改回 qwen，没 key 会立刻失败，
# 而不是安静地把真实请求发出去
settings.dashscope_api_key = None
settings.gemini_api_key = None
settings.openai_api_key = None


# --------------------------------------------------------------------------- #
# 出站请求熔断
# --------------------------------------------------------------------------- #
class RealNetworkCallInTest(RuntimeError):
    """测试期间发起了真实出站请求。"""


_HINT = (
    "测试期间禁止真实出站请求（OPEN-RISK-02）。\n"
    "  · 代码里大概率有一条路径绕过了 config.settings 直接建了 HTTP 客户端\n"
    "  · 确实需要打真实 API 的用例请标 @pytest.mark.realapi，并用 --realapi 单独跑"
)

OUTBOUND_CALLS: list[str] = []


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "realapi: 需要真实外部 API 的用例；默认跳过，加 --realapi 才跑"
    )


def pytest_addoption(parser):
    parser.addoption(
        "--realapi", action="store_true", default=False,
        help="放行标了 realapi 的用例（会产生真实调用与费用）",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--realapi"):
        return
    skip = pytest.mark.skip(reason="需要真实 API：加 --realapi 才跑")
    for item in items:
        if "realapi" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _block_real_network(request, monkeypatch):
    """给每个用例装上出站熔断。标了 realapi 的用例放行。"""
    if "realapi" in request.keywords:
        yield
        return

    import httpx

    def _record(request_obj):
        line = f"{request_obj.method} {request_obj.url}"
        OUTBOUND_CALLS.append(line)
        raise RealNetworkCallInTest(f"{_HINT}\n  · 被拦下的请求：{line}")

    async def _async_send(self, req, **kw):
        _record(req)

    def _sync_send(self, req, **kw):
        _record(req)

    monkeypatch.setattr(httpx.AsyncClient, "send", _async_send, raising=False)
    monkeypatch.setattr(httpx.Client, "send", _sync_send, raising=False)
    yield


@pytest.fixture(scope="session", autouse=True)
def _isolated_storage():
    """会话级：建表。目录留在 tmp 下，由系统回收。"""
    import db

    db.init_db()
    yield
