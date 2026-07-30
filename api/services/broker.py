"""按 audit_id 的事件总线：后台跑图 → 事件缓冲 → SSE 订阅（支持迟到订阅的重放）。

为什么需要它：POST /api/audits 立刻返回并异步启动图，而前端可能几百毫秒后
才连上 /stream。缓冲 + 重放保证「上传即跳转」的动线不会丢掉前几条事件。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

_MAX_BUFFER = 500


class _Channel:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.closed = False
        self._waiters: list[asyncio.Event] = []

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        del self.events[:-_MAX_BUFFER]
        self._wake()

    def close(self) -> None:
        self.closed = True
        self._wake()

    def _wake(self) -> None:
        for w in self._waiters:
            w.set()
        self._waiters.clear()

    async def subscribe(self, start: int = 0) -> AsyncIterator[dict[str, Any]]:
        idx = start
        while True:
            while idx < len(self.events):
                yield self.events[idx]
                idx += 1
            if self.closed:
                return
            waiter = asyncio.Event()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=15)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": {}}   # keep-alive


_channels: dict[str, _Channel] = {}


def channel(audit_id: str) -> _Channel:
    return _channels.setdefault(audit_id, _Channel())


def publish(audit_id: str, event: str, data: dict[str, Any]) -> None:
    channel(audit_id).publish({"event": event, "data": data})


def reopen(audit_id: str) -> None:
    """resume 时复用同一条通道（人工裁定后继续推送 done）。"""
    channel(audit_id).closed = False


def close(audit_id: str) -> None:
    channel(audit_id).close()


def drop(audit_id: str) -> None:
    _channels.pop(audit_id, None)
