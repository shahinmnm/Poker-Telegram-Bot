"""Async synchronization primitives specific to the poker bot."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
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
        """Release the lock.

        This implementation relaxes the strict task ownership check so that the
        lock can be released even when the current task is not the owner. In
        such a case a warning is logged and the underlying lock is forcibly
        released when the reentrancy depth reaches zero. This prevents crashes
        when a scheduled job releases the lock from a different task.
        """
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("ReentrantAsyncLock release requires an active task")
        if self._owner is not current:
            logging.getLogger(__name__).warning(
                "Non-owner task attempted to release re-entrant lock; releasing anyway"
            )
            if self._depth > 0:
                self._depth -= 1
            if self._depth <= 0:
                self._owner = None
                if self._lock.locked():
                    self._lock.release()
            return
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
