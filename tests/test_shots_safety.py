"""截图通道隐私安全断言 + 基本网页抽取（需要 chromium，无 X 依赖）。"""

import asyncio
import http.server
import json
import threading
from pathlib import Path

import pytest

from x_radar.config import Settings
from x_radar.pipeline.shots import ShotManager

PAGE_HTML = b"""<!doctype html><html><head><title>RadarTest Page</title></head>
<body><article><h1>heading</h1><p>privacy-check-body-marker</p></article></body></html>"""


class EchoHandler(http.server.BaseHTTPRequestHandler):
    last_headers = {}

    def do_GET(self):  # noqa: N802
        EchoHandler.last_headers = dict(self.headers)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "trackme=1; Path=/")
        self.end_headers()
        self.wfile.write(PAGE_HTML)

    def log_message(self, *a):
        pass


@pytest.fixture()
def echo_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/doc"
    srv.shutdown()


def chromium_available() -> bool:
    """直接尝试无头启动（headless 模式用的是 headless shell）。"""
    try:
        from playwright.async_api import async_playwright

        async def probe():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                await browser.close()
                return True

        return asyncio.run(probe())
    except Exception:
        return False


def test_page_shot_no_cookies_and_extraction(echo_server, tmp_path):
    """核心安全断言：匿名上下文访问带 Set-Cookie 的站点，
    请求头中绝不出现 Cookie；截图与正文抽取正常。"""
    if not chromium_available():
        pytest.skip("chromium 未安装")
    settings = Settings(data_dir=tmp_path, proxy=None, headless=True)
    (settings.media_dir / "pages").mkdir(parents=True, exist_ok=True)
    out = settings.media_dir / "pages" / "t.png"

    async def run():
        mgr = ShotManager(settings)
        try:
            return await mgr.page_shot(echo_server, out)
        finally:
            await mgr.aclose()

    content = asyncio.run(run())
    headers = {k.lower(): v for k, v in EchoHandler.last_headers.items()}
    assert "cookie" not in headers, f"请求带了 Cookie: {headers}"
    assert "authorization" not in headers
    assert content.title == "RadarTest Page"
    assert "privacy-check-body-marker" in content.text
    assert out.exists() and out.stat().st_size > 1000


def test_tweet_shot_never_sends_cookies(echo_server, tmp_path, monkeypatch):
    """推文截图通道：把 embed 域名替换成本地 echo 服务，
    断言截图请求同样不带任何 Cookie/身份头。"""
    if not chromium_available():
        pytest.skip("chromium 未安装")
    settings = Settings(data_dir=tmp_path, proxy=None, headless=True)
    (settings.media_dir / "tweets").mkdir(parents=True, exist_ok=True)
    out = settings.media_dir / "tweets" / "t.png"
    mgr = ShotManager(settings)

    async def run():
        try:
            ctx = await mgr._new_context(560, 1400)
            page = await ctx.new_page()
            await page.goto(echo_server, wait_until="load", timeout=20000)
            await ctx.close()
        finally:
            await mgr.aclose()

    asyncio.run(run())
    headers = {k.lower(): v for k, v in EchoHandler.last_headers.items()}
    assert "cookie" not in headers, f"请求带了 Cookie: {headers}"
