"""轮询 + 处理管线编排。

poll 循环：每个周期遍历账号拉 syndication 时间线，新推文入库（pending）
并发 SSE。worker 池按队列处理：推文截图 -> 外链页截图+正文 -> 摘要 -> done。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..bus import EventBus
from ..config import Settings
from ..db import Store, utc_now
from ..sources.syndication import (
    SourceError,
    SyndicationSource,
    filter_for_handle,
    valid_handle,
)
from .shots import ShotError, ShotManager, sanitize_filename
from .summarize import summarize_page, summarize_tweet

log = logging.getLogger("x_radar.watcher")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Watcher:
    def __init__(self, settings: Settings, store: Store, bus: EventBus,
                 source: SyndicationSource | None = None,
                 shots: ShotManager | None = None):
        self.cfg = settings
        self.store = store
        self.bus = bus
        self.source = source or SyndicationSource(settings)
        self.shots = shots or ShotManager(settings)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.status: dict = {
            "last_poll_at": None, "next_poll_at": None, "poll_count": 0,
            "processed": 0, "failed": 0, "errors": [],
            "source": self.source.name, "headless": settings.headless,
        }

    # ---------- 生命周期 ----------
    async def start(self, workers: int = 2) -> None:
        self.store.reset_processing()
        for tid in self.store.pending_ids():
            self._enqueue(tid)
        self._tasks.append(asyncio.create_task(self._poll_loop()))
        for i in range(workers):
            self._tasks.append(asyncio.create_task(self._worker(i)))
        log.info("watcher started (interval=%ss)", self.cfg.poll_interval)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.source.aclose()
        await self.shots.aclose()

    def _publish_status(self) -> None:
        self.bus.publish("status", self.status)

    def _add_error(self, msg: str) -> None:
        self.status["errors"] = ([msg] + self.status["errors"])[:5]

    # ---------- 账号管理 ----------
    async def add_account(self, handle: str) -> dict:
        handle = handle.lstrip("@").strip()
        if not valid_handle(handle):
            raise SourceError("handle 只能含字母/数字/下划线，长度≤15")
        if self.store.get_account(handle):
            raise FileExistsError(f"@{handle} 已在监控列表")
        user = await self.source.fetch_user(handle)
        if not user:
            raise SourceError(f"未找到 @{handle} 或该账号没有可见推文")
        self.store.upsert_account(user["handle"], user.get("name", ""),
                                  user.get("avatar", ""), enabled=1)
        self.bus.publish("account_update",
                         {"accounts": self.store.list_accounts()})
        await self.poll_once([user["handle"]])
        return self.store.get_account(user["handle"]) or {}

    async def remove_account(self, handle: str) -> bool:
        handle = handle.lstrip("@")
        n = self.store.delete_account(handle)
        if n:
            self.bus.publish("account_update",
                             {"accounts": self.store.list_accounts()})
        return bool(n)

    # ---------- 轮询 ----------
    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 兜底，保持循环存活
                log.exception("poll loop error")
                self._add_error(f"轮询异常: {e}")
                self._publish_status()
            wait = self.cfg.poll_interval
            self.status["next_poll_at"] = _iso_now_offset(wait)
            self._publish_status()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self, only: list[str] | None = None) -> int:
        """拉一轮时间线，返回新增条数。"""
        accounts = [a["handle"] for a in self.store.list_accounts() if a["enabled"]]
        if only:
            accounts = [h for h in accounts if h.lower() in
                        {x.lower().lstrip('@') for x in only}] or only
        new_total = 0
        self.status["last_poll_at"] = utc_now()
        self.status["poll_count"] += 1
        for handle in accounts:
            try:
                tweets = await self.source.fetch_timeline(handle)
                tweets = filter_for_handle(tweets, handle)
            except SourceError as e:
                self._add_error(f"@{handle}: {e}")
                log.warning("poll %s failed: %s", handle, e)
                continue
            for t in tweets:
                t["first_seen_at"] = utc_now()
                if self.store.insert_tweet(t):
                    new_total += 1
                    self._enqueue(t["id"])
                    self.bus.publish("tweet_new", self.store.card_for(t["id"]))
                latest = t.get("created_at")
                if latest:
                    self.store.set_account_last_tweet(handle, latest)
            # 每个账号之间稍作间隔，礼貌抓取
            if len(accounts) > 1:
                await asyncio.sleep(2.0)
        if new_total:
            log.info("poll: %s new tweets", new_total)
        self._publish_status()
        return new_total

    # ---------- 队列处理 ----------
    def _enqueue(self, tweet_id: str) -> None:
        try:
            self.queue.put_nowait(tweet_id)
        except asyncio.QueueFull:
            log.warning("queue full, drop %s", tweet_id)

    async def _worker(self, wid: int) -> None:
        while not self._stop.is_set():
            try:
                tid = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self.process(tid)
            except Exception:
                log.exception("process %s crashed", tid)
                self.store.update_tweet(tid, status="failed", error="内部错误")
                self.bus.publish("tweet_update", self.store.card_for(tid))
                self.status["failed"] += 1

    async def process(self, tweet_id: str) -> None:
        row = self.store.get_tweet(tweet_id)
        if not row:
            return
        self.store.update_tweet(tweet_id, status="processing", error="")
        self.bus.publish("tweet_update", self.store.card_for(tweet_id))
        error = ""
        try:
            # 1) 推文截图（匿名 embed，零账号信息）
            shot_rel = ""
            try:
                path = self.cfg.media_dir / "tweets" / f"{sanitize_filename(tweet_id)}.png"
                await self.shots.tweet_shot(tweet_id, path)
                shot_rel = f"/media/tweets/{path.name}"
                self.store.update_tweet(tweet_id, shot=shot_rel)
            except (ShotError, Exception) as e:  # 截图失败不阻断摘要
                error = f"截图失败: {e}"
                log.warning("tweet shot %s: %s", tweet_id, e)

            # 2) 外链网页截图 + 正文
            urls = (row.get("external_urls") or "") or "[]"
            import json as _json
            url_list = _json.loads(urls) if isinstance(urls, str) else urls
            url_list = (url_list or [])[: self.cfg.max_pages]
            for idx, page_url in enumerate(url_list):
                await self._process_page(tweet_id, idx, page_url)

            # 3) 摘要（推文 + 汇总 pages 由前端分别展示）
            summary, model = await summarize_tweet(
                self.cfg, row["text"], row["handle"], row["kind"], row["rt_handle"] or "")
            self.store.update_tweet(tweet_id, summary=summary, summary_model=model,
                                    status="done", error=error)
            self.status["processed"] += 1
        finally:
            final = self.store.card_for(tweet_id)
            self.store.update_tweet(tweet_id, status=final["status"])
            self.bus.publish("tweet_update", final)

    async def _process_page(self, tweet_id: str, idx: int, page_url: str) -> None:
        self.store.upsert_page(tweet_id, idx, url=page_url, status="processing")
        self.bus.publish("tweet_update", self.store.card_for(tweet_id))
        try:
            path = self.cfg.media_dir / "pages" / f"{sanitize_filename(tweet_id)}_{idx}.png"
            content = await self.shots.page_shot(page_url, path)
            summary, model = await summarize_page(
                self.cfg, content.title, content.text, content.meta_description)
            self.store.upsert_page(
                tweet_id, idx, url=page_url, final_url=content.final_url,
                title=content.title or content.final_url,
                shot=f"/media/pages/{path.name}", summary=summary,
                status="done", error="")
        except Exception as e:
            log.warning("page %s[%d] %s: %s", tweet_id, idx, page_url, e)
            self.store.upsert_page(tweet_id, idx, url=page_url,
                                   status="failed", error=str(e)[:200])

    # ---------- 插件推送 ----------
    async def ingest(self, items: list[dict]) -> tuple[int, int]:
        accepted = dup = 0
        for item in items:
            tid = str(item.get("id") or "").strip()
            handle = str(item.get("handle") or "").lstrip("@").strip()
            if not tid or not valid_handle(handle):
                continue
            existing = self.store.get_account(handle)
            tweet = {
                "id": tid,
                "handle": handle,
                "author_name": (existing or {}).get("name", handle),
                "avatar": (existing or {}).get("avatar", ""),
                "text": str(item.get("text") or "")[:2000],
                "created_at": str(item.get("created_at") or ""),
                "first_seen_at": utc_now(),
                "url": f"https://x.com/{handle}/status/{tid}",
                "kind": item.get("kind") or "tweet",
                "rt_handle": item.get("rt_handle") or "",
                "metrics": {},
                "external_urls": [u for u in (item.get("external_urls") or [])
                                  if str(u).startswith("http")][: self.cfg.max_pages],
                "source": "extension",
            }
            if self.store.insert_tweet(tweet):
                accepted += 1
                self._enqueue(tid)
                self.bus.publish("tweet_new", self.store.card_for(tid))
                if not existing:
                    self.store.upsert_account(handle)
                    self.bus.publish("account_update",
                                     {"accounts": self.store.list_accounts()})
            else:
                dup += 1
        return accepted, dup

    async def refresh(self, tweet_id: str) -> bool:
        if not self.store.tweet_exists(tweet_id):
            return False
        self.store.update_tweet(tweet_id, status="pending", error="")
        self._enqueue(tweet_id)
        return True


def _iso_now_offset(seconds: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds")
