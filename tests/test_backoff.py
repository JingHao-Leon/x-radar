"""429 限流退避状态机与轮询循环的集成行为。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from x_radar.bus import EventBus  # noqa: E402
from x_radar.config import Settings  # noqa: E402
from x_radar.db import Store  # noqa: E402
from x_radar.pipeline.watcher import Backoff, Watcher  # noqa: E402
from x_radar.sources.syndication import RateLimitedError, SourceError  # noqa: E402


# ---------- Backoff 纯逻辑 ----------

def test_backoff_escalate_steps():
    b = Backoff()
    assert b.seconds == 0
    b.escalate()
    assert b.seconds == 300
    b.escalate()
    assert b.seconds == 600
    for _ in range(10):  # 封顶
        b.escalate()
    assert b.seconds == 3600


def test_backoff_decay_gradual():
    b = Backoff()
    for _ in range(5):
        b.escalate()
    assert b.seconds == 3600
    for expected in (1800, 1200, 600, 300, 0):
        b.decay()
        assert b.seconds == expected
    b.decay()  # 已归零，继续衰减不变负
    assert b.seconds == 0


def test_backoff_retry_after_override():
    b = Backoff()
    b.escalate(retry_after=900)
    assert b.seconds == 900  # Retry-After 大于档位时覆盖
    b.decay()  # 覆盖值先被清除，露出当前档位
    assert b.seconds == 300
    b.decay()
    assert b.seconds == 0  # 再一轮干净轮询后完全恢复
    b.escalate(retry_after=10)  # 小于档位时不降档（level 0 -> max(300,10)）
    assert b.seconds == 300
    b.escalate(retry_after="bad")  # 非法值安全忽略，仅升档
    assert b.seconds == 600


def test_backoff_reset():
    b = Backoff()
    b.escalate()
    b.reset()
    assert b.seconds == 0


# ---------- poll_once 限流行为 ----------

class FlakySource:
    name = "flaky"

    def __init__(self, script):
        self.script = list(script)  # 每次调用弹出一个行为
        self.calls: list[str] = []

    async def fetch_timeline(self, handle):
        self.calls.append(handle)
        action = self.script.pop(0) if self.script else "ok"
        if action == "429":
            raise RateLimitedError(retry_after=120)
        if action == "err":
            raise SourceError("HTTP 404")
        return [{"id": f"{handle}-1", "handle": handle.upper(), "text": "hi",
                 "created_at": "", "kind": "tweet", "metrics": {},
                 "external_urls": [], "author_name": handle, "avatar": "",
                 "url": "", "rt_handle": ""}]

    async def fetch_user(self, handle):
        return {"handle": handle.upper(), "name": handle, "avatar": ""}

    async def aclose(self):
        pass


def make_watcher(tmp_path, source):
    settings = Settings(data_dir=tmp_path)
    store = Store(tmp_path / "t.db")
    store.upsert_account("AAA")
    store.upsert_account("BBB")
    return Watcher(settings, store, EventBus(), source=source)


def test_poll_once_rate_limit_stops_remaining_accounts(tmp_path):
    """第一个账号 429 -> 全局暂停本轮，不再请求第二个账号。"""
    src = FlakySource(["429", "ok"])
    w = make_watcher(tmp_path, src)
    rate_limited = asyncio.run(w.poll_once())
    assert rate_limited is True
    assert src.calls == ["AAA"]  # BBB 被跳过
    assert w.backoff.seconds == 300  # Retry-After 120s 小于首档 300s，取档位值
    assert any("限流" in e for e in w.status["errors"])


def test_poll_once_recovers_and_decays(tmp_path):
    """限流恢复后：本轮拉取成功；回落由轮询循环执行（这里手动模拟）。"""
    src = FlakySource(["429", "ok"])
    w = make_watcher(tmp_path, src)
    assert asyncio.run(w.poll_once()) is True
    assert w.backoff.seconds == 300  # Retry-After 120s 不覆盖首档
    assert asyncio.run(w.poll_once()) is False  # 恢复
    assert src.calls == ["AAA", "AAA", "BBB"]  # 本轮两账号都拉取
    # 循环里干净轮询 -> decay：先清 Retry-After 覆盖/降档，再降一档归零
    w.backoff.decay()
    assert w.backoff.seconds == 300
    w.backoff.decay()
    assert w.backoff.seconds == 0


def test_poll_once_other_source_errors_continue(tmp_path):
    """非限流错误（如 404）不触发全局暂停，继续轮询其余账号。"""
    src = FlakySource(["err", "ok"])
    w = make_watcher(tmp_path, src)
    rate_limited = asyncio.run(w.poll_once())
    assert rate_limited is False
    assert src.calls == ["AAA", "BBB"]
    assert w.backoff.seconds == 0
