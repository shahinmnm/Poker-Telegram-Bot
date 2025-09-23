"""Centralized asynchronous lock management for the poker bot."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional

from pokerapp.utils.locks import ReentrantAsyncLock


class LockManager:
    """Manage keyed re-entrant async locks with timeout and retry support."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        default_timeout_seconds: Optional[float] = 5,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1,
    ) -> None:
        self._logger = logger
        self._default_timeout_seconds = default_timeout_seconds
        self._max_retries = max(0, max_retries)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._locks: Dict[str, ReentrantAsyncLock] = {}
        self._locks_guard = asyncio.Lock()

    async def _get_lock(self, key: str) -> ReentrantAsyncLock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = ReentrantAsyncLock()
                self._locks[key] = lock
            return lock

    async def acquire(self, key: str, timeout: Optional[float] = None) -> bool:
        """Attempt to acquire the lock identified by ``key``."""

        lock = await self._get_lock(key)
        total_timeout = self._default_timeout_seconds if timeout is None else timeout
        deadline: Optional[float]
        loop = asyncio.get_running_loop()
        if total_timeout is None:
            deadline = None
        else:
            deadline = loop.time() + max(0.0, total_timeout)

        attempts = self._max_retries + 1
        for attempt in range(attempts):
            attempt_start = loop.time()
            attempt_timeout: Optional[float]
            if deadline is None:
                attempt_timeout = None
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                remaining_attempts = attempts - attempt
                attempt_timeout = remaining / remaining_attempts
                if attempt_timeout <= 0:
                    break

            try:
                if attempt_timeout is None:
                    await lock.acquire()
                else:
                    await asyncio.wait_for(lock.acquire(), timeout=attempt_timeout)
                elapsed = loop.time() - attempt_start
                if attempt == 0 and elapsed < 0.1:
                    self._logger.info(
                        "Lock '%s' acquired quickly in %.3fs", key, elapsed
                    )
                else:
                    self._logger.info(
                        "Lock '%s' acquired after %d attempt(s) in %.3fs",
                        key,
                        attempt + 1,
                        elapsed,
                    )
                return True
            except asyncio.TimeoutError:
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - loop.time())
                self._logger.warning(
                    "Timeout acquiring lock '%s' on attempt %d (remaining %.3fs)",
                    key,
                    attempt + 1,
                    remaining if remaining is not None else float("inf"),
                )
            except asyncio.CancelledError:
                self._logger.warning(
                    "Lock acquisition for '%s' cancelled on attempt %d", key, attempt + 1
                )
                raise

            if attempt < attempts - 1:
                backoff = self._retry_backoff_seconds * (2**attempt)
                if backoff > 0:
                    if deadline is None:
                        await asyncio.sleep(backoff)
                    else:
                        remaining_sleep = deadline - loop.time()
                        if remaining_sleep <= 0:
                            break
                        await asyncio.sleep(min(backoff, remaining_sleep))

        self._logger.error("Failed to acquire lock '%s' after %d attempts", key, attempts)
        return False

    @asynccontextmanager
    async def guard(self, key: str, timeout: Optional[float] = None) -> AsyncIterator[None]:
        acquired = await self.acquire(key, timeout=timeout)
        if not acquired:
            message = f"Timeout acquiring lock '{key}'"
            self._logger.warning(message)
            raise TimeoutError(message)
        try:
            yield
        finally:
            self.release(key)

    def release(self, key: str) -> None:
        lock = self._locks.get(key)
        if lock is None:
            self._logger.debug("Release requested for unknown lock '%s'", key)
            return
        try:
            lock.release()
        except RuntimeError:
            self._logger.exception(
                "Failed to release lock '%s' due to ownership mismatch", key
            )
            raise

__all__ = ["LockManager"]
