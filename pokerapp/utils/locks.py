"""Async synchronization primitives specific to the poker bot."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional


class ReentrantAsyncLock:
    """A task-aware re-entrant lock for asyncio."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task[Any]] = None
        self._depth = 0

    async def acquire(self) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("ReentrantAsyncLock requires an active asyncio task")
        if self._owner is current:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = current
        self._depth = 1

    def release(self) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("ReentrantAsyncLock release requires an active task")
        if self._owner is not current:
            raise RuntimeError("Lock can only be released by the owning task")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    @asynccontextmanager
    async def context(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            self.release()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.release()
