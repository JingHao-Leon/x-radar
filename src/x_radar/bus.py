"""进程内事件总线：watcher 发布，SSE 订阅。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

_QUEUE_MAX = 200


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        for q in list(self._subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass
