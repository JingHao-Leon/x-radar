"""FastAPI 应用：仪表盘静态托管 + JSON API + SSE + 插件 ingest。"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .bus import EventBus
from .config import Settings, load_settings
from .db import Store
from .pipeline.summarize import aclose_llm
from .pipeline.watcher import Watcher
from .sources.syndication import SourceError

log = logging.getLogger("x_radar.api")


def build_app(settings: Settings | None = None,
              store: Store | None = None,
              bus: EventBus | None = None,
              watcher: Watcher | None = None,
              run_watcher: bool = True):
    settings = settings or load_settings()
    store = store or Store(settings.db_path)
    bus = bus or EventBus()
    watcher = watcher or Watcher(settings, store, bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_watcher:
            await watcher.start()
        yield
        if run_watcher:
            await watcher.stop()
        await aclose_llm()

    app = FastAPI(title="X Radar", version=__version__, lifespan=lifespan)

    # ---- 鉴权 ----
    def auth(x_radar_token: str = Header(default="")):
        if settings.token and x_radar_token != settings.token:
            raise HTTPException(401, "token 无效")

    # ---- API ----
    @app.get("/api/health")
    async def health():
        c = store.counts()
        return {"ok": True, "version": __version__, "llm": settings.llm_enabled,
                "accounts": c["accounts"], "tweets": c["tweets"],
                "poll_interval": settings.poll_interval}

    @app.get("/api/status")
    async def status():
        return watcher.status

    @app.get("/api/accounts")
    async def accounts():
        return {"accounts": store.list_accounts()}

    @app.post("/api/accounts", status_code=201)
    async def add_account(body: dict, _: None = Depends(auth)):
        handle = str(body.get("handle") or "").strip()
        try:
            account = await watcher.add_account(handle)
        except SourceError as e:
            raise HTTPException(400, str(e)) from e
        except FileExistsError as e:
            raise HTTPException(409, str(e)) from e
        return {"account": account}

    @app.delete("/api/accounts/{handle}")
    async def delete_account(handle: str, _: None = Depends(auth)):
        if not await watcher.remove_account(handle):
            raise HTTPException(404, f"@{handle} 不在监控列表")
        return {"ok": True}

    @app.get("/api/feed")
    async def feed(limit: int = Query(50, ge=1, le=200), handle: str = "",
                   q: str = "", before_id: str = ""):
        rows = store.list_feed(limit=limit, handle=handle, q=q, before_id=before_id)
        return {"tweets": store.cards(rows)}

    @app.get("/api/tweets/{tweet_id}")
    async def get_tweet(tweet_id: str):
        card = store.card_for(tweet_id)
        if not card:
            raise HTTPException(404, "推文不存在")
        return card

    @app.post("/api/tweets/{tweet_id}/refresh")
    async def refresh_tweet(tweet_id: str, _: None = Depends(auth)):
        if not await watcher.refresh(tweet_id):
            raise HTTPException(404, "推文不存在")
        return {"ok": True}

    @app.post("/api/ingest")
    async def ingest(body: dict, _: None = Depends(auth)):
        items = body.get("tweets") or []
        if not isinstance(items, list) or len(items) > 100:
            raise HTTPException(400, "body 需为 {tweets: [...]} 且≤100条")
        accepted, dup = await watcher.ingest(items)
        return {"ok": True, "accepted": accepted, "duplicates": dup}

    # ---- SSE ----
    @app.get("/api/stream")
    async def stream():
        q = bus.subscribe()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---- 静态资源 ----
    static_dir = Path(__file__).parent / "static"
    media_dir = settings.media_dir
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static_dir / "index.html")

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    app = build_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port,
                log_level="info")


if __name__ == "__main__":
    main()
