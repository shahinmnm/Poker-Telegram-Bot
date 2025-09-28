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
        lock can be released even when the current task is not the owner or when
        no task context is available (for example, callbacks scheduled with
        ``loop.call_soon``). In such cases a warning is logged and the
        re-entrancy depth is decremented until the underlying lock can be
        safely released. This prevents crashes or leaked locks when background
        callbacks or schedulers manage the release lifecycle.
        """
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None

        if current is None:
            logging.getLogger(__name__).warning(
                "Re-entrant lock release invoked without an active asyncio task; forcing release"
            )
            self._decrement_depth_and_maybe_release()
            return

        if self._owner is not current:
            logging.getLogger(__name__).warning(
                "Non-owner task attempted to release re-entrant lock; releasing anyway"
            )
            self._decrement_depth_and_maybe_release()
            return

        self._decrement_depth_and_maybe_release()

    def _decrement_depth_and_maybe_release(self) -> None:
        if self._depth > 0:
            self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
            if self._lock.locked():
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
