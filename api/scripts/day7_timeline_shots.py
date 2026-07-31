"""Day7 验收：AgentTrace 三态 + 两种时间线的 Playwright 复验（mock 模式）。

产出 3 张截图到 `docs/daily/day7-shots/`：

  01-timeline-search.png    联网搜索路径：web_search 正常执行，右上角"联网搜索取证"
  02-timeline-cache.png     缓存命中路径：web_search 标"已跳过"（虚线灰点），徽标变"缓存命中·跳过搜索"
  03-timeline-fallback.png  兜底标黄：conflict 样本的 web_search 与 adjudicate 两步

strict 否决态**不出截图**：用现有 mock 品名凑不出"得分 ≥0.82 且触发否决"的场景，
为一张图去加 mock 分支不值当。该行为由 `tests/test_cache_match_modes.py` 走完整
`lookup()` 锁死（19 例），证据强度高于截图。

用法（需先起 api:8000 与 web:3000）：
    python scripts/day7_timeline_shots.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                                                    # noqa: E402
from graph.state import Classification, Evidence, NutrientValue  # noqa: E402
from services import cache_store                             # noqa: E402

WEB = "http://localhost:3000"
API = "http://localhost:8000"
SHOTS = Path(__file__).resolve().parents[2] / "docs" / "daily" / "day7-shots"

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100" "05fe02fe" "0000000049454e44ae426082"
)


def _seed_cache(brand: str, name: str, code: int) -> str:
    ev = [
        Evidence(
            id="ev_seed",
            source_url="https://mock.example/product",
            nutrients=[
                NutrientValue(nutrient="sugar", value=8.0, unit="g/100g", normalized=8.0),
                NutrientValue(nutrient="fiber", value=9.0, unit="g/100g", normalized=9.0),
            ],
        )
    ]
    verdict = Classification(
        general_id=1, specific_code=code, brand=brand, product_name=name,
        specific_confidence=0.9, general_confidence=0.95,
    )
    return cache_store.upsert(brand, name, ev, verdict)["id"]


async def _upload(page, stem: str) -> str:
    """上传一张图并返回 audit_id。文件名决定 mock VLM 的行为（见 services/vlm.py）。"""
    tmp = Path("/tmp") / f"{stem}.png"
    tmp.write_bytes(PNG_1X1)
    resp = await page.request.post(
        f"{API}/api/audits",
        multipart={"files": {"name": tmp.name, "mimeType": "image/png",
                             "buffer": tmp.read_bytes()}},
    )
    data = await resp.json()
    assert data.get("audits"), f"上传失败：{data}"
    return data["audits"][0]["audit_id"]


async def _shoot(page, audit_id: str, out: str, *, wait_for: str) -> None:
    await page.goto(f"{WEB}/audits/{audit_id}", wait_until="networkidle")
    await page.wait_for_selector(wait_for, timeout=25_000)
    await page.wait_for_timeout(600)          # 让状态点的动画稳定下来
    trace = page.get_by_test_id("agent-trace")
    SHOTS.mkdir(parents=True, exist_ok=True)
    await trace.screenshot(path=str(SHOTS / out))

    statuses = await page.eval_on_selector_all(
        "[data-testid^='step-']",
        "els => els.map(e => [e.dataset.testid, e.dataset.status])",
    )
    print(f"  {out}: {statuses}")
    return statuses


async def main() -> None:
    from playwright.async_api import async_playwright

    db.init_db()
    results: dict[str, list] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 900, "height": 800})

        # ① 联网搜索路径（低置信 + 无缓存）
        with db.cursor() as cur:
            cur.execute("DELETE FROM product_cache")
        aid = await _upload(page, "low-cereal")
        results["search"] = await _shoot(
            page, aid, "01-timeline-search.png",
            wait_for="[data-testid='step-web_search'][data-status='done']",
        )

        # ② 缓存命中路径 —— web_search 应显示"已跳过"
        _seed_cache("MockBrand", "Mock Cereal 500g", 2)
        aid = await _upload(page, "parent-cereal")
        results["cache"] = await _shoot(
            page, aid, "02-timeline-cache.png",
            wait_for="[data-testid='step-web_search'][data-status='skipped']",
        )

        # ③ 兜底标黄：证据冲突
        with db.cursor() as cur:
            cur.execute("DELETE FROM product_cache")
        aid = await _upload(page, "conflict-yoghurt")
        results["fallback"] = await _shoot(
            page, aid, "03-timeline-fallback.png",
            wait_for="[data-testid='step-adjudicate_with_evidence'][data-status='fallback']",
        )

        await browser.close()

    print("\n验收：")
    print(f"  ① 搜索路径 web_search=done      : {('step-web_search', 'done') in map(tuple, results['search'])}")
    print(f"  ② 缓存路径 web_search=skipped   : {('step-web_search', 'skipped') in map(tuple, results['cache'])}")
    print(f"  ③ 冲突路径 adjudicate=fallback  : "
          f"{('step-adjudicate_with_evidence', 'fallback') in map(tuple, results['fallback'])}")


if __name__ == "__main__":
    asyncio.run(main())
