"""AI 摘要：OpenAI 兼容接口（Kimi/DeepSeek/OpenAI…），失败降级为规则摘要。"""

from __future__ import annotations

import re

import httpx

from ..config import Settings

TWEET_PROMPT = (
    "你是资讯助理。请用中文总结这条 X(Twitter) 推文，不超过120字："
    "保留关键事实、数字、结论与链接域名；若为转推请带上原作者；直接输出摘要正文。"
)
PAGE_PROMPT = (
    "你是资讯助理。以下是一篇网页的标题与正文节选，"
    "请用中文总结其要点，不超过160字：保留核心结论与关键数据，直接输出摘要正文。"
)

_SENT_SPLIT = re.compile(r"(?<=[。！？!?])|(?<=[.!?])\s+")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")

llm_client: httpx.AsyncClient | None = None


def _get_client(settings: Settings) -> httpx.AsyncClient:
    global llm_client
    if llm_client is None:
        llm_client = httpx.AsyncClient(timeout=httpx.Timeout(25.0))
    return llm_client


async def aclose_llm() -> None:
    global llm_client
    if llm_client:
        await llm_client.aclose()
        llm_client = None


def extractive(text: str, limit: int = 110) -> str:
    """无 LLM 时的降级摘要：取前几句 + 数字要点。"""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    sents = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    out = ""
    for s in sents:
        if len(out) + len(s) > limit:
            break
        out += s
    if not out:
        out = text[:limit]
    nums = _NUM_RE.findall(text)
    extra = "；含数据：" + "、".join(nums[:4]) if nums and len(nums) > 1 else ""
    if len(out) + len(extra) <= limit + 40:
        out += extra
    return out[: limit + 40]


async def _llm(settings: Settings, system: str, user: str) -> str:
    client = _get_client(settings)
    resp = await client.post(
        f"{settings.llm_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[:6000]},
            ],
            "temperature": 0.3,
            "max_tokens": 400,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return (content or "").strip()


async def summarize_tweet(settings: Settings, text: str, handle: str,
                          kind: str, rt_handle: str) -> tuple[str, str]:
    """返回 (summary, model)；LLM 失败自动降级 extractive。"""
    prefix = f"@{handle} " + (f"转推 @{rt_handle}：" if kind == "retweet" and rt_handle else "")
    body = prefix + text
    if not settings.llm_enabled:
        return extractive(body), "extractive"
    try:
        s = await _llm(settings, TWEET_PROMPT, body)
        return (s or extractive(body), settings.llm_model) if s else (extractive(body), "extractive")
    except Exception:
        return extractive(body), "extractive"


async def summarize_page(settings: Settings, title: str, text: str,
                         meta_desc: str = "") -> tuple[str, str]:
    body = f"标题：{title}\n描述：{meta_desc}\n正文：{text[:3500]}"
    if not settings.llm_enabled:
        return extractive(f"{title}。{meta_desc or text}", 150), "extractive"
    try:
        s = await _llm(settings, PAGE_PROMPT, body)
        if s:
            return s, settings.llm_model
    except Exception:
        pass
    return extractive(f"{title}。{meta_desc or text}", 150), "extractive"
