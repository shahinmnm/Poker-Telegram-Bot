import asyncio
import logging
from typing import Dict
from unittest.mock import AsyncMock, patch

import pytest

from pokerapp.lock_manager import LockManager


def _make_lock_manager(mock_redis: AsyncMock, logger_name: str = "lock-test") -> LockManager:
    return LockManager(
        logger=logging.getLogger(logger_name),
        enable_fine_grained_locks=True,
        redis_pool=mock_redis,
    )


def _smart_retry_config(overrides: Dict[str, float | int | bool | list]) -> Dict[str, object]:
    base: Dict[str, object] = {
        "max_attempts": 3,
        "initial_backoff_seconds": 0.1,
        "max_backoff_seconds": 0.4,
        "enable_jitter": False,
        "queue_depth_threshold": 5,
        "estimated_wait_threshold_seconds": 30.0,
        "queue_wait_multiplier": 0.0,
        "grace_buffer_seconds": 10.0,
    }
    base.update(overrides)
    return {"lock_retry": base}


class TestSmartLockRetry:
    @pytest.mark.asyncio
    async def test_queue_depth_tracking(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 3

        lock_manager = _make_lock_manager(mock_redis, "queue-depth")

        depth = await lock_manager.get_lock_queue_depth("test_lock")
        assert depth == 3

        prefix = lock_manager._redis_keys["lock_queue_prefix"]
        mock_redis.llen.assert_awaited_once_with(f"{prefix}test_lock")

    @pytest.mark.asyncio
    async def test_smart_retry_with_backoff(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.set.side_effect = [False, False, True]
        mock_redis.llen.side_effect = [0, 0, 0]

        lock_manager = _make_lock_manager(mock_redis, "retry-backoff")
        lock_manager._system_constants = _smart_retry_config({})
        lock_manager.estimate_wait_time = AsyncMock(return_value=0.0)  # type: ignore[assignment]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            acquired = await lock_manager.acquire_with_smart_retry("test_lock", 5.0)

        assert acquired is True
        assert mock_redis.set.await_count == 3
        assert mock_sleep.await_count == 2
        assert mock_redis.lpush.await_count == 1
        assert mock_redis.lrem.await_count == 1

    @pytest.mark.asyncio
    async def test_smart_retry_with_queue_depth(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 10
        mock_redis.set.return_value = False

        lock_manager = _make_lock_manager(mock_redis, "queue-threshold")
        lock_manager._system_constants = _smart_retry_config(
            {
                "queue_depth_threshold": 2,
                "estimated_wait_threshold_seconds": 5.0,
            }
        )
        lock_manager.estimate_wait_time = AsyncMock(return_value=15.0)  # type: ignore[assignment]

        acquired = await lock_manager.acquire_with_smart_retry("congested_lock", 5.0)

        assert acquired is False
        mock_redis.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_queue_management(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        mock_redis.llen.return_value = 1

        lock_manager = _make_lock_manager(mock_redis, "queue-concurrent")
        lock_manager._system_constants = _smart_retry_config({})
        lock_manager.estimate_wait_time = AsyncMock(return_value=0.0)  # type: ignore[assignment]

        async def acquire_lock(task_name: str) -> bool:
            return await lock_manager.acquire_with_smart_retry(
                f"shared_lock_{task_name}", 5.0
            )

        results = await asyncio.gather(
            acquire_lock("task1"),
            acquire_lock("task2"),
            acquire_lock("task3"),
            acquire_lock("task4"),
            acquire_lock("task5"),
        )

        assert all(results)
        assert mock_redis.lpush.await_count == 5
        assert mock_redis.lrem.await_count == 5

    @pytest.mark.asyncio
    async def test_grace_buffer_timeout(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.set.return_value = False
        mock_redis.llen.return_value = 1

        lock_manager = _make_lock_manager(mock_redis, "grace-buffer")
        lock_manager._system_constants = _smart_retry_config(
            {
                "max_attempts": 6,
                "grace_buffer_seconds": 0.25,
                "initial_backoff_seconds": 0.1,
            }
        )
        lock_manager.estimate_wait_time = AsyncMock(return_value=0.0)  # type: ignore[assignment]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            acquired = await lock_manager.acquire_with_smart_retry(
                "timeout_lock", 5.0
            )

        assert acquired is False
        assert mock_redis.set.await_count < 6

    @pytest.mark.asyncio
    async def test_metrics_collection(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.set.side_effect = [False, True]
        mock_redis.llen.return_value = 1

        lock_manager = _make_lock_manager(mock_redis, "metrics")
        lock_manager._system_constants = _smart_retry_config({})
        lock_manager.estimate_wait_time = AsyncMock(return_value=0.0)  # type: ignore[assignment]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            acquired = await lock_manager.acquire_with_smart_retry("metrics_lock", 5.0)

        assert acquired is True

        def metric_value(metric: object) -> float:
            raw_value = getattr(metric, "_value", None)
            if raw_value is None:
                return 0.0
            getter = getattr(raw_value, "get", None)
            if callable(getter):
                return getter()
            inner_value = getattr(raw_value, "_value", None)
            getter = getattr(inner_value, "get", None)
            if callable(getter):
                return getter()
            for candidate in (inner_value, raw_value):
                try:
                    if candidate is not None:
                        return float(candidate)
                except (TypeError, ValueError):
                    continue
            return 0.0

        lock_type = lock_manager._extract_lock_type("metrics_lock")
        attempt_metric = lock_manager.lock_retry_attempts.labels(
            lock_type=lock_type, attempt_number="1"
        )
        success_metric = lock_manager.lock_retry_success.labels(lock_type=lock_type)
        acquisition_metric = lock_manager.lock_acquisition_success.labels(
            lock_type=lock_type, attempt_number="2"
        )

        assert metric_value(attempt_metric) > 0
        assert metric_value(success_metric) >= 0
        assert metric_value(acquisition_metric) > 0

    @pytest.mark.asyncio
    async def test_exponential_backoff_with_jitter(self) -> None:
        mock_redis = AsyncMock()
        lock_manager = _make_lock_manager(mock_redis, "backoff-jitter")
        lock_manager._system_constants = _smart_retry_config(
            {
                "enable_jitter": True,
                "jitter_range": [1.0, 1.0],
                "queue_wait_multiplier": 0.5,
            }
        )

        delay = await lock_manager._calculate_backoff_with_jitter(
            attempt=2,
            base_delay=0.1,
            queue_depth=3,
        )

        # Attempt=2 -> exponential (0.1 * 2**1) = 0.2, queue factor = min(3*0.5, 3) = 1.5
        # With jitter=1.0 -> delay = 0.2 * (1 + 1.5) = 0.5
        assert pytest.approx(delay, rel=1e-6) == 0.5

    @pytest.mark.asyncio
    async def test_should_retry_based_on_queue_threshold(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 6
        lock_manager = _make_lock_manager(mock_redis, "queue-decision")
        lock_manager._system_constants = _smart_retry_config(
            {
                "queue_depth_threshold": 4,
                "estimated_wait_threshold_seconds": 20.0,
            }
        )

        should_retry = await lock_manager._should_retry_based_on_queue(
            "lock:abc",
            attempt=2,
            queue_depth=6,
            estimated_wait=25.0,
        )

        assert should_retry is False

    @pytest.mark.asyncio
    async def test_queue_estimation_accuracy(self) -> None:
        mock_redis = AsyncMock()
        lock_manager = _make_lock_manager(mock_redis, "queue-estimate")

        with patch("random.uniform", return_value=1.0):
            estimate = await lock_manager.estimate_wait_time(3)

        # 3 operations * 6 seconds each capped by MAX_ESTIMATE (45 seconds)
        assert estimate == pytest.approx(18.0, rel=1e-6)
