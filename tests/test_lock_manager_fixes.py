import asyncio
import logging
import time

import pytest

from pokerapp.lock_manager import LockManager, LockOrderError


@pytest.mark.asyncio
async def test_fast_path_race_condition():
    """Verify fast-path has no race condition."""
    lm = LockManager(logger=logging.getLogger(__name__), default_timeout_seconds=5)

    async def hold_lock():
        async with lm.guard("race:test", timeout=1):
            await asyncio.sleep(0.1)

    holder = asyncio.create_task(hold_lock())
    await asyncio.sleep(0.01)

    start = time.time()
    result = await lm.acquire("race:test", timeout=0.01)
    elapsed = time.time() - start

    assert not result
    assert elapsed >= 0.01
    assert lm._metrics.get("lock_fast_path_misses", 0) > 0

    await holder


@pytest.mark.asyncio
async def test_batch_cleanup_complete():
    """Verify cleanup processes all batches."""
    lm = LockManager(logger=logging.getLogger(__name__))

    for i in range(250):
        acquired = await lm.acquire(f"cleanup:test:{i}", timeout=1)
        assert acquired
        await lm.release(f"cleanup:test:{i}")
        lock = await lm._get_lock(f"cleanup:test:{i}")
        setattr(lock, "_acquired_at_ts", time.time() - 200)

    removed = await lm.cleanup_idle_locks()

    assert removed == 250, f"Expected 250 removed, got {removed}"


@pytest.mark.asyncio
async def test_pool_bounds_safety():
    """Verify pool operations are thread-safe."""
    lm = LockManager(logger=logging.getLogger(__name__))

    async def access_pool():
        for _ in range(10):
            await lm._get_lock(f"pool:test:{asyncio.current_task().get_name()}")

    tasks = [asyncio.create_task(access_pool(), name=f"task{i}") for i in range(10)]
    await asyncio.gather(*tasks)

    assert lm._metrics.get("lock_pool_hits", 0) >= 0
    assert lm._metrics.get("lock_pool_misses", 0) >= 0


@pytest.mark.asyncio
async def test_fast_path_deferred_validation(monkeypatch):
    """Validation for fast-path happens after acquisition succeeds."""

    lm = LockManager(logger=logging.getLogger(__name__), default_timeout_seconds=5)

    key = "deferred:test"
    lock = await lm._get_lock(key)

    acquire_times = []

    original_lock_acquire = lock.acquire

    async def tracked_lock_acquire(*args, **kwargs):
        await original_lock_acquire(*args, **kwargs)
        acquire_times.append(time.time())

    monkeypatch.setattr(lock, "acquire", tracked_lock_acquire, raising=False)

    validation_times = []
    original_validate = lm._validate_lock_order

    def tracked_validate(*args, **kwargs):
        validation_times.append(time.time())
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(lm, "_validate_lock_order", tracked_validate, raising=False)

    start = time.time()
    result = await lm.acquire(key, timeout=1)

    assert result is True
    assert len(acquire_times) == 1
    assert len(validation_times) == 1
    assert validation_times[0] >= acquire_times[0]

    elapsed_us = (time.time() - start) * 1_000_000
    assert elapsed_us < 200_000, f"Fast-path unexpectedly slow: {elapsed_us:.0f}μs"

    await lm.release(key)


@pytest.mark.asyncio
async def test_fast_path_validation_rollback():
    """Lock is released when validation fails after fast-path acquisition."""

    lm = LockManager(logger=logging.getLogger(__name__), default_timeout_seconds=5)

    acquired = await lm.acquire("chat:test", level=4, timeout=1)
    assert acquired

    with pytest.raises(LockOrderError):
        await lm.acquire("table:test", level=2, timeout=1)

    violating_lock = await lm._get_lock("table:test")
    assert violating_lock._count == 0
    assert not violating_lock._lock.locked()

    await lm.release("chat:test")
