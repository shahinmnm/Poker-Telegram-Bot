import asyncio
import contextlib
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


@pytest.mark.asyncio
async def test_lock_manager_context_logging() -> None:
    logger = logging.getLogger("lock_manager_test_context")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    context = {"chat_id": 42, "game_id": "abc"}
    async with manager.guard("stage:context", context=context):
        pass

    messages = [record.getMessage() for record in handler.records]
    expected_fragment = "[context: chat_id=42, game_id='abc']"
    assert any("acquired" in message and expected_fragment in message for message in messages)
    assert any("released" in message and expected_fragment in message for message in messages)

    logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_lock_manager_enforces_lock_hierarchy() -> None:
    logger = logging.getLogger("lock_manager_test_hierarchy")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    async with manager.guard("stage:hierarchy", context={"order": "stage"}):
        async with manager.guard("player:hierarchy", context={"order": "player"}):
            pass

    async with manager.guard("player:reverse", context={"order": "player"}):
        with pytest.raises(RuntimeError):
            await manager.acquire("stage:reverse", context={"order": "stage"})


@pytest.mark.asyncio
async def test_detect_deadlock_cycle_detection() -> None:
    logger = logging.getLogger("lock_manager_test_deadlock")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=5,
        max_retries=0,
        retry_backoff_seconds=0.01,
    )

    ready_stage = asyncio.Event()
    ready_player = asyncio.Event()
    start_cycle = asyncio.Event()

    async def task_one() -> None:
        async with manager.guard(
            "stage:cycle",
            timeout=5,
            context={"task": "one"},
            level=5,
        ):
            ready_stage.set()
            await start_cycle.wait()
            try:
                acquired = await manager.acquire(
                    "player:cycle",
                    timeout=5,
                    context={"task": "one"},
                    level=5,
                )
                if acquired:
                    manager.release("player:cycle")
            except asyncio.CancelledError:
                raise

    async def task_two() -> None:
        async with manager.guard(
            "player:cycle",
            timeout=5,
            context={"task": "two"},
            level=5,
        ):
            ready_player.set()
            await start_cycle.wait()
            try:
                acquired = await manager.acquire(
                    "stage:cycle",
                    timeout=5,
                    context={"task": "two"},
                    level=5,
                )
                if acquired:
                    manager.release("stage:cycle")
            except asyncio.CancelledError:
                raise

    task_one_future = asyncio.create_task(task_one(), name="cycle-one")
    task_two_future = asyncio.create_task(task_two(), name="cycle-two")

    await ready_stage.wait()
    await ready_player.wait()
    start_cycle.set()

    await asyncio.sleep(0.1)
    snapshot = manager.detect_deadlock()

    assert snapshot["waiting"]
    assert snapshot["cycles"]
    assert any("cycle-one" in node for cycle in snapshot["cycles"] for node in cycle)
    assert any("cycle-two" in node for cycle in snapshot["cycles"] for node in cycle)

    task_one_future.cancel()
    task_two_future.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task_one_future
    with contextlib.suppress(asyncio.CancelledError):
        await task_two_future
