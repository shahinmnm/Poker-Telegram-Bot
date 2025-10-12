"""Tests for the stale user cleanup background job behaviour."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.background_jobs import StaleUserCleanupJob


async def _wait_for_condition(predicate, timeout=1.0, interval=0.01):
    """Utility helper to await a predicate with timeout handling."""

    end_time = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() >= end_time:
            raise TimeoutError("Condition not met within timeout")
        await asyncio.sleep(interval)


def _build_model_mock() -> MagicMock:
    model = MagicMock()
    model._logger = MagicMock()
    model._table_manager = MagicMock()
    model._table_manager.load_game = AsyncMock(return_value=(MagicMock(), None))
    model._table_manager.save_game = AsyncMock(return_value=None)
    model._prune_ready_seats = AsyncMock(return_value=[])
    return model


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_max_failures():
    """The cleanup job should open the circuit after repeated failures."""

    model = _build_model_mock()
    model._table_manager.get_active_game_ids = AsyncMock(
        side_effect=Exception("Redis down")
    )

    job = StaleUserCleanupJob(model, interval_seconds=0)
    job._max_failures = 3

    await job.start()

    await _wait_for_condition(lambda: job._circuit_open is True)

    assert job._circuit_open is True
    assert job._running is False

    await job.stop()


@pytest.mark.asyncio
async def test_circuit_breaker_reset():
    """Manual reset should allow the job to start again after failure."""

    model = _build_model_mock()
    model._table_manager.get_active_game_ids = AsyncMock(return_value=[])

    job = StaleUserCleanupJob(model, interval_seconds=0)
    job._circuit_open = True
    job._consecutive_failures = 5

    await job.start()
    assert job._running is False

    job.reset_circuit_breaker()
    assert job._circuit_open is False
    assert job._consecutive_failures == 0

    await job.start()
    assert job._running is True

    await asyncio.sleep(0)
    await job.stop()
