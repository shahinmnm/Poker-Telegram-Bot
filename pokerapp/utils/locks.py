"""Async synchronization primitives specific to the poker bot."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any, AsyncIterator, Optional


class LockOwnershipError(RuntimeError):
    """Raised when a non-owner task attempts to release the lock."""


class ReentrantAsyncLock:
    """A task-aware re-entrant lock for asyncio."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task[Any]] = None
        self._owner_id: Optional[int] = None
        self._count = 0

    async def acquire(self) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("ReentrantAsyncLock requires an active asyncio task")
        current_task_id = id(current)

        if self._owner_id == current_task_id and self._count > 0:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = current
        self._owner_id = current_task_id
        self._count = 1

    def release(self) -> None:
        """Release the lock, enforcing task ownership."""

        logger = logging.getLogger(__name__)

        try:
            current = asyncio.current_task()
        except RuntimeError as exc:  # pragma: no cover - no running event loop
            logger.error(
                "Re-entrant lock release attempted without an active asyncio task",
                extra={
                    "owner_task_id": self._owner_id,
                    "release_task_id": None,
                    "lock_depth": self._count,
                },
            )
            raise LockOwnershipError("Re-entrant lock release requires an active asyncio task") from exc

        if current is None:
            logger.error(
                "Re-entrant lock release attempted without an active asyncio task",
                extra={
                    "owner_task_id": self._owner_id,
                    "release_task_id": None,
                    "lock_depth": self._count,
                },
            )
            raise LockOwnershipError("Re-entrant lock release requires an active asyncio task")

        current_task_id = id(current)

        if self._owner_id != current_task_id:
            logger.error(
                "Non-owner task attempted to release re-entrant lock",
                extra={
                    "owner_task_id": self._owner_id,
                    "release_task_id": current_task_id,
                    "lock_depth": self._count,
                },
            )
            raise LockOwnershipError(
                "Re-entrant lock can only be released by the owning task"
            )

        if self._count <= 0:
            logger.error(
                "Re-entrant lock release requested but lock is not held",
                extra={
                    "owner_task_id": self._owner_id,
                    "release_task_id": current_task_id,
                    "lock_depth": self._count,
                },
            )
            raise RuntimeError("Cannot release an un-acquired re-entrant lock")

        logger.debug(
            "Releasing re-entrant lock",
            extra={
                "owner_task_id": self._owner_id,
                "release_task_id": current_task_id,
                "lock_depth": self._count,
            },
        )

        self._decrement_depth_and_maybe_release()

    def _decrement_depth_and_maybe_release(self) -> None:
        if self._count > 0:
            self._count -= 1
        if self._count <= 0:
            self._count = 0
            self._owner = None
            self._owner_id = None
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
