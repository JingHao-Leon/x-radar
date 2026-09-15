"""截图与网页抽取（Playwright）。

隐私设计：截图一律走「匿名浏览器上下文」——每次任务新建 context，
不加载任何 Cookie / localStorage / 登录态；推文截图用 X 官方 embed 页
（platform.twitter.com/embed/Tweet.html），页面本身只含推文内容，
从机制上保证不会拍到自己的账号信息。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import async_playwright

from ..config import Settings


class ShotError(RuntimeError):
    pass


@dataclass
class PageContent:
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    meta_description: str = ""
    links: list[str] = field(default_factory=list)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000]+")


def clean_text(raw: str, limit: int = 4000) -> str:
    lines = []
    for line in raw.splitlines():
        line = _WS_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines)
    return text[:limit]


class ShotManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            self._pw = await async_playwright().start()
            launch_kwargs: dict = {"headless": self.settings.headless}
            if self.settings.proxy:
                launch_kwargs["proxy"] = {"server": self.settings.proxy}
            if self.settings.cdp_endpoint:
                self._browser = await self._pw.chromium.connect_over_cdp(
                    self.settings.cdp_endpoint
                )
            else:
                self._browser = await self._pw.chromium.launch(**launch_kwargs)
            return self._browser

    async def aclose(self) -> None:
        for closer in (lambda: self._browser.close() if self._browser else None,):
            try:
                coro = closer()
                if coro:
                    await coro
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._browser = None
        self._pw = None

    async def _new_context(self, width: int, height: int):
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=2,
            locale="zh-CN",
            user_agent=self.settings.user_agent,
        )
        # 纵然新 context 默认无 Cookie，也显式清一次，作为安全不变量
        await context.clear_cookies()
        return context

    # ---- 推文截图 ----
    async def tweet_shot(self, tweet_id: str, out_path: Path) -> Path:
        url = (
            "https://platform.twitter.com/embed/Tweet.html"
            f"?id={tweet_id}&theme=light&dnt=true&lang=zh-cn"
        )
        context = await self._new_context(width=560, height=1400)
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="load", timeout=45_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            article = page.locator("article").first
            try:
                await article.wait_for(state="visible", timeout=15_000)
            except Exception as e:
                raise ShotError(f"embed 未渲染出推文: {e}") from e
            # 等图片解码完成，避免截图半加载
            await page.wait_for_timeout(1200)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            await article.screenshot(path=str(out_path), animations="disabled")
            return out_path
        finally:
            await context.close()

    # ---- 外部网页截图 + 正文抽取 ----
    async def page_shot(self, url: str, out_path: Path, max_wait_s: float = 30.0) -> PageContent:
        context = await self._new_context(width=1440, height=900)
        content = PageContent(url=url)
        try:
            page = await context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=max_wait_s * 1000)
            except Exception as e:
                raise ShotError(f"打开页面失败: {e}") from e
            content.final_url = page.url
            if resp and resp.status >= 400:
                raise ShotError(f"HTTP {resp.status}")
            try:
                await page.wait_for_load_state("networkidle",
                                               timeout=max_wait_s * 1000)
            except Exception:
                pass
            # 触发懒加载后回到顶部
            await page.mouse.wheel(0, 1600)
            await page.wait_for_timeout(900)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(400)
            content.title = (await page.title() or "").strip()
            content.meta_description = await page.evaluate(
                "() => { const m = document.querySelector('meta[name=description]')"
                " || document.querySelector('meta[property=\"og:description\"]');"
                " return m ? m.content : ''; }"
            )
            body_text = await page.evaluate(
                "() => { const a = document.querySelector('article');"
                " return (a || document.body).innerText; }"
            )
            content.text = clean_text(body_text or "")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out_path), full_page=False,
                                  animations="disabled")
            return content
        finally:
            await context.close()


def sanitize_filename(tweet_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tweet_id)
