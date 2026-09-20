"""数据源：X 官方 syndication 页面（无需登录、无需 API key）。

原理：https://syndication.twitter.com/srv/timeline-profile/screen-name/<handle>
返回的 Next.js 页面在 <script id="__NEXT_DATA__"> 里内嵌了整条时间线的 JSON
（tweet id / 全文 / 实体 / 互动数 / 用户信息），服务端直接解析即可。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import Settings

NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

SELF_HOSTS = ("x.com", "twitter.com", "t.co", "pic.x.com", "pic.twitter.com",
              "abs.twimg.com", "pbs.twimg.com", "platform.twitter.com",
              "syndication.twitter.com", "cdn.syndication.twimg.com")


class SourceError(RuntimeError):
    pass


class RateLimitedError(SourceError):
    """syndication 返回 429：同一 IP 请求过频。retry_after 取自响应头。"""

    def __init__(self, retry_after: int | None = None):
        super().__init__(
            "HTTP 429 限流" + (f"（Retry-After {retry_after}s）" if retry_after else "")
        )
        self.retry_after = retry_after


def _retry_after_of(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("Retry-After", "")
    return int(raw) if raw.isdigit() else None


def valid_handle(handle: str) -> bool:
    return bool(HANDLE_RE.match(handle))


def parse_twitter_time(raw: str) -> str:
    """'Mon Sep 14 17:59:22 +0000 2026' -> ISO8601 UTC。"""
    try:
        dt = datetime.strptime(raw.strip(), "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def expand_text(full_text: str, entities: dict) -> str:
    """用 entities.urls 把 t.co 短链替换回真实链接。"""
    text = full_text or ""
    for u in (entities or {}).get("urls", []):
        short, expanded = u.get("url", ""), u.get("expanded_url", "")
        if short and expanded:
            text = text.replace(short, expanded)
    return text


def classify(tweet: dict) -> tuple[str, str]:
    """(kind, rt_handle)"""
    full = tweet.get("full_text") or ""
    if full.startswith("RT @") and "retweeted_status" in tweet:
        return "retweet", (tweet["retweeted_status"].get("user") or {}).get("screen_name", "")
    if "quoted_tweet" in tweet or "quoted_status" in tweet:
        return "quote", ""
    return "tweet", ""


def external_urls_of(tweet: dict) -> list[str]:
    urls: list[str] = []
    for group in ("urls",):
        for u in (tweet.get("entities") or {}).get(group, []):
            e = u.get("expanded_url") or ""
            if e and e not in urls:
                urls.append(e)
    for u in (tweet.get("entities") or {}).get("media", []) or []:
        e = u.get("expanded_url") or ""
        if e and e not in urls:
            urls.append(e)
    out: list[str] = []
    for u in urls:
        host = u.split("//", 1)[-1].split("/", 1)[0].lower()
        if any(host == h or host.endswith("." + h) for h in SELF_HOSTS):
            continue
        if u not in out:
            out.append(u)
    return out


def parse_entry_tweet(et: dict, source: str = "poll") -> dict:
    """__NEXT_DATA__ 里的 tweet 对象 -> 统一推文字典。"""
    tweet = et.get("retweeted_status") if (et.get("full_text") or "").startswith("RT @") else et
    kind, rt_handle = classify(et)
    user = et.get("user") or {}
    text = expand_text((tweet or et).get("full_text") or "", (tweet or et).get("entities") or {})
    tid = et.get("id_str") or str(et.get("id") or "")
    return {
        "id": tid,
        "handle": (user.get("screen_name") or "").strip(),
        "author_name": user.get("name", ""),
        "avatar": user.get("profile_image_url_https", ""),
        "text": text,
        "created_at": parse_twitter_time(et.get("created_at") or ""),
        "url": f"https://x.com/{user.get('screen_name','')}/status/{tid}",
        "kind": kind,
        "rt_handle": rt_handle,
        "metrics": {
            "likes": et.get("favorite_count", 0),
            "retweets": et.get("retweet_count", 0),
            "replies": et.get("reply_count", 0),
        },
        "external_urls": external_urls_of(tweet or et),
        "source": source,
    }


def parse_next_data(html: str) -> list[dict]:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise SourceError("页面中未找到 __NEXT_DATA__（X 结构可能变化）")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise SourceError(f"__NEXT_DATA__ 解析失败: {e}") from e
    page = (data.get("props") or {}).get("pageProps") or {}
    if "timeline" not in page:
        raise SourceError(f"syndication 返回异常: {str(data)[:200]}")
    entries = (page.get("timeline") or {}).get("entries") or []
    out = []
    for entry in entries:
        et = ((entry.get("content") or {}).get("tweet")) or {}
        if et.get("id_str") or et.get("id"):
            out.append(parse_entry_tweet(et))
    return out


def parse_user(html: str) -> dict:
    """取页面上任一推文的 user 字段作为账号资料。"""
    m = NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    entries = ((data.get("props") or {}).get("pageProps") or {}).get("timeline", {}).get("entries") or []
    for entry in entries:
        user = ((entry.get("content") or {}).get("tweet") or {}).get("user") or {}
        if user.get("screen_name"):
            return {
                "handle": user["screen_name"],
                "name": user.get("name", ""),
                "avatar": user.get("profile_image_url_https", ""),
            }
    return {}


class SyndicationSource:
    name = "syndication"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            proxy = self.settings.proxy or None
            self._client = httpx.AsyncClient(
                proxy=proxy,
                timeout=httpx.Timeout(25.0),
                follow_redirects=True,
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                },
            )
        return self._client

    async def fetch_timeline(self, handle: str) -> list[dict]:
        handle = handle.lstrip("@")
        if not valid_handle(handle):
            raise SourceError(f"非法 handle: {handle!r}")
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}?showReplies=false"
        client = await self._ensure_client()
        try:
            resp = await client.get(url)
        except httpx.HTTPError as e:
            raise SourceError(f"网络错误: {e}") from e
        if resp.status_code == 429:
            raise RateLimitedError(_retry_after_of(resp))
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}（账号不存在或被限流）")
        return parse_next_data(resp.text)

    async def fetch_user(self, handle: str) -> dict:
        handle = handle.lstrip("@")
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}?showReplies=false"
        client = await self._ensure_client()
        resp = await client.get(url)
        if resp.status_code == 429:
            raise RateLimitedError(_retry_after_of(resp))
        if resp.status_code != 200:
            raise SourceError(f"HTTP {resp.status_code}（账号不存在或被限流）")
        return parse_user(resp.text)

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


def filter_for_handle(tweets: list[dict], handle: str) -> list[dict]:
    """监控账号自己的时间线返回的推文统一归属该账号（含其转推）。"""
    out = []
    for t in tweets:
        t = dict(t)
        t["handle"] = handle.lstrip("@")
        out.append(t)
    return out
