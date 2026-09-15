"""为 README 生成仪表盘截图：1440x900 @2x，先有真实回测数据再跑。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from playwright.async_api import async_playwright  # noqa: E402

DASH = "http://127.0.0.1:8787/"
OUT = ROOT / "docs" / "screenshots"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900},
                                        device_scale_factor=2)
        page = await ctx.new_page()
        await page.goto(DASH, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(OUT / "dashboard.png"), full_page=False)
        # 有推文截图时，顺带截一张 lightbox 大图
        thumb = page.locator("img.tweet-shot").first
        if await thumb.count():
            await thumb.click()
            await page.wait_for_timeout(1200)
            await page.screenshot(path=str(OUT / "lightbox.png"))
        await browser.close()
    print("saved:", OUT / "dashboard.png")


if __name__ == "__main__":
    asyncio.run(main())
