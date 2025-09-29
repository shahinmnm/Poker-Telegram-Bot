import asyncio
import contextlib
import logging
from typing import List

import pytest

from pokerapp.lock_manager import LockManager, LockOrderError


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

    key = "stage:1"
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
            "Timeout acquiring Lock" in record.getMessage()
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

    context = {"request_category": "test"}
    context_extra = {"game_id": "abc"}
    async with manager.guard("stage:42", context=context, context_extra=context_extra):
        pass

    messages = [record.getMessage() for record in handler.records]
    identity_fragment = "Lock 'stage:42' (level=1, chat_id=42, game_id=abc)"
    context_fragment = (
        "[context: chat_id=42, game_id='abc', lock_category='stage', lock_key='stage:42',"
        " lock_level=1, lock_name='stage:42', request_category='test']"
    )
    assert any(
        "acquired" in message
        and identity_fragment in message
        and context_fragment in message
        for message in messages
    )
    assert any(
        "released" in message
        and identity_fragment in message
        and context_fragment in message
        for message in messages
    )

    assert any(
        record.__dict__.get("event_type") == "lock_acquired"
        and record.__dict__.get("call_site_function") not in {None, "unknown"}
        for record in handler.records
    )
    assert any(
        record.__dict__.get("event_type") == "lock_released"
        and record.__dict__.get("release_function") not in {None, "unknown"}
        for record in handler.records
    )

    logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_lock_manager_category_timeout_override() -> None:
    logger = logging.getLogger("lock_manager_test_category_timeout")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=5,
        max_retries=0,
        retry_backoff_seconds=0.01,
        category_timeouts={"engine_stage": 0.05},
    )

    key = "stage:category-timeout"

    async def holder() -> None:
        async with manager.guard(key, timeout=0.2):
            await asyncio.sleep(0.2)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError):
        async with manager.guard(key):
            pass

    await hold_task


@pytest.mark.asyncio
async def test_lock_manager_metrics_recording() -> None:
    logger = logging.getLogger("lock_manager_test_metrics")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=0.5,
        max_retries=0,
        retry_backoff_seconds=0.01,
        category_timeouts={"engine_stage": 0.1},
    )

    key = "stage:metrics"

    async def holder() -> None:
        async with manager.guard(key, timeout=0.2):
            await asyncio.sleep(0.15)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError):
        async with manager.guard(key, timeout=0.05):
            pass

    await hold_task

    metrics = manager.metrics
    assert metrics["lock_timeouts"] >= 1
    assert metrics["lock_contention"] >= 1


@pytest.mark.asyncio
async def test_lock_manager_release_from_loop_callback() -> None:
    logger = logging.getLogger("lock_manager_test_callback_release")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    key = "stage:callback-release"
    assert await manager.acquire(key, timeout=0.5)

    loop = asyncio.get_running_loop()
    release_future: asyncio.Future[None] = loop.create_future()

    def _release_from_callback() -> None:
        try:
            manager.release(key)
        except Exception as exc:  # pragma: no cover - defensive
            if not release_future.done():
                release_future.set_exception(exc)
        else:
            if not release_future.done():
                release_future.set_result(None)

    loop.call_soon(_release_from_callback)
    await release_future

    reacquired = await manager.acquire(key, timeout=0.5)
    assert reacquired
    manager.release(key)


def test_lock_manager_empty_snapshot_logs_debug() -> None:
    logger = logging.getLogger("lock_manager_test_empty_snapshot")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    manager._log_lock_snapshot_on_timeout("empty_stage")

    snapshot_records = [
        record
        for record in handler.records
        if record.getMessage().startswith("Lock snapshot (empty_stage)")
    ]
    assert snapshot_records, "Expected lock snapshot log to be emitted"
    assert all(record.levelno < logging.WARNING for record in snapshot_records)

    logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_lock_manager_allows_mixed_lock_levels_without_release() -> None:
    logger = logging.getLogger("lock_manager_test_hierarchy")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    async with manager.guard(
        "stage:hierarchy", context={"order": "engine_stage"}
    ):
        async with manager.guard(
            "pokerbot:player_report:hierarchy",
            context={"order": "player_report"},
        ):
            assert True


@pytest.mark.asyncio
async def test_lock_manager_detects_reverse_lock_order() -> None:
    logger = logging.getLogger("lock_manager_test_hierarchy_violation")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    async with manager.guard("wallet:reverse", context={"order": "wallet"}):
        with pytest.raises(LockOrderError):
            await manager.acquire(
                "stage:reverse", context={"order": "engine_stage"}
            )


@pytest.mark.asyncio
async def test_lock_manager_allows_increasing_lock_order() -> None:
    logger = logging.getLogger("lock_manager_test_increasing_violation")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    async with manager.guard(
        "stage:increasing", context={"order": "engine_stage"}
    ):
        acquired = await manager.acquire(
            "chat:increasing", context={"order": "chat"}
        )
        try:
            assert acquired is True
        finally:
            manager.release("chat:increasing")


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
                    "wallet:cycle",
                    timeout=5,
                    context={"task": "one"},
                    level=5,
                )
                if acquired:
                    manager.release("wallet:cycle")
            except asyncio.CancelledError:
                raise

    async def task_two() -> None:
        async with manager.guard(
            "wallet:cycle",
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
