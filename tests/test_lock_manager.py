import asyncio
import logging
from typing import List

import pytest

from pokerapp.lock_manager import LockManager


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(record)


@pytest.mark.asyncio
async def test_lock_manager_acquire_when_free() -> None:
    logger = logging.getLogger("lock_manager_test_free")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    key = "stage:free"
    acquired = await manager.acquire(key, timeout=0.5)
    assert acquired
    manager.release(key)


@pytest.mark.asyncio
async def test_lock_manager_retries_before_acquire() -> None:
    logger = logging.getLogger("lock_manager_test_retry")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=0.5,
        max_retries=2,
        retry_backoff_seconds=0.05,
    )

    key = "stage:retry"

    async def holder() -> None:
        async with manager.guard(key, timeout=1):
            await asyncio.sleep(0.3)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    try:
        acquired = await manager.acquire(key, timeout=0.5)
        assert acquired
        manager.release(key)
        await hold_task

        assert any(
            "Timeout acquiring lock" in record.getMessage()
            for record in handler.records
        )
    finally:
        if not hold_task.done():
            await hold_task
        logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_lock_manager_guard_timeout() -> None:
    logger = logging.getLogger("lock_manager_test_timeout")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=0.1,
        max_retries=1,
        retry_backoff_seconds=0.05,
    )

    key = "stage:timeout"

    async def holder() -> None:
        async with manager.guard(key, timeout=1):
            await asyncio.sleep(0.3)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError):
        async with manager.guard(key, timeout=0.1):
            pass

    await hold_task
