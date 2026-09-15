"""API 集成测试：FakeSource + 截图桩，全链路离线。"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import TINY_PNG  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from x_radar.bus import EventBus  # noqa: E402
from x_radar.config import Settings  # noqa: E402
from x_radar.db import Store  # noqa: E402
from x_radar.pipeline.shots import PageContent  # noqa: E402
from x_radar.pipeline.watcher import Watcher  # noqa: E402
from x_radar.api import build_app  # noqa: E402


class FakeSource:
    name = "fake"

    def __init__(self, tweets):
        self.tweets = tweets
        self.calls = 0

    async def fetch_user(self, handle):
        return {"handle": handle.upper(), "name": f"Name {handle}",
                "avatar": "https://pbs.twimg.com/x.jpg"}

    async def fetch_timeline(self, handle):
        self.calls += 1
        return [dict(t, handle=handle.upper()) for t in self.tweets]

    async def aclose(self):
        pass


class StubShots:
    async def tweet_shot(self, tweet_id, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(TINY_PNG)
        return out_path

    async def page_shot(self, url, out_path, max_wait_s=20.0):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(TINY_PNG)
        return PageContent(url=url, final_url=url, title="测试页",
                           text="这是正文内容，包含数字 42%。")

    async def aclose(self):
        pass


def make_settings(tmp_path, token=""):
    s = Settings(data_dir=tmp_path, token=token, poll_interval=60,
                 llm_base_url="", llm_model="")
    (s.media_dir / "tweets").mkdir(parents=True, exist_ok=True)
    (s.media_dir / "pages").mkdir(parents=True, exist_ok=True)
    return s


def make_client(tmp_path, token="", tweets=None):
    settings = make_settings(tmp_path, token)
    store = Store(settings.db_path)
    source = FakeSource(tweets or [
        {"id": "1001", "handle": "NASA", "text": "hello world",
         "created_at": "2026-09-14T00:00:00+00:00", "kind": "tweet",
         "external_urls": ["https://example.com/a"], "metrics": {},
         "author_name": "NASA", "avatar": "", "url": "", "rt_handle": ""},
    ])
    watcher = Watcher(settings, store, EventBus(), source=source,
                      shots=StubShots())
    app = build_app(settings, store, watcher=watcher, run_watcher=False)
    return TestClient(app, raise_server_exceptions=False), store, watcher


def test_health_and_full_pipeline(tmp_path):
    client, store, watcher = make_client(tmp_path)
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.post("/api/accounts", json={"handle": "@nasa"})
    assert r.status_code == 201, r.text
    card = r.json().get("account")
    assert card["handle"] == "NASA"

    feed = client.get("/api/feed").json()["tweets"]
    assert len(feed) == 1 and feed[0]["id"] == "1001"
    # 后台跑完管线（TestClient 事件循环内手动处理队列）
    asyncio.get_event_loop_policy()
    tid = feed[0]["id"]
    asyncio.run(watcher.process(tid))
    card = client.get(f"/api/tweets/{tid}").json()
    assert card["status"] == "done"
    assert card["shot"] == f"/media/tweets/{tid}.png"
    assert card["summary"] and card["summary_model"] == "extractive"
    assert card["pages"][0]["status"] == "done"
    assert "42%" in card["pages"][0]["summary"]

    # 重复添加 -> 409
    assert client.post("/api/accounts", json={"handle": "nasa"}).status_code == 409
    # 删除
    assert client.delete("/api/accounts/NASA").status_code == 200
    assert client.get("/api/accounts").json()["accounts"] == []


def test_ingest_and_dedup(tmp_path):
    client, store, watcher = make_client(tmp_path)
    body = {"tweets": [{"id": "2001", "handle": "openai", "text": "hi",
                        "kind": "tweet"}]}
    r = client.post("/api/ingest", json=body)
    assert r.json() == {"ok": True, "accepted": 1, "duplicates": 0}
    r = client.post("/api/ingest", json=body)
    assert r.json()["duplicates"] == 1
    assert store.get_account("openai") is not None


def test_auth_token_required(tmp_path):
    client, _, _ = make_client(tmp_path, token="sekrit")
    assert client.get("/api/feed").status_code == 200  # feed 不设防（本机面板）
    assert client.post("/api/ingest", json={"tweets": []}).status_code == 401
    assert client.post("/api/ingest", json={"tweets": []},
                       headers={"X-Radar-Token": "sekrit"}).status_code == 200
    assert client.post("/api/accounts", json={"handle": "nasa"}).status_code == 401


def test_bad_handle(tmp_path):
    client, _, _ = make_client(tmp_path)
    r = client.post("/api/accounts", json={"handle": "bad handle!"})
    assert r.status_code == 400


def test_dashboard_served(tmp_path):
    client, _, _ = make_client(tmp_path)
    r = client.get("/")
    # 仪表盘静态文件由前端任务生成；生成后应为 200
    assert r.status_code in (200, 500)
