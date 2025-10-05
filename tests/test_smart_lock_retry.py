import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from pokerapp.lock_manager import LockManager


class TestSmartLockRetry:

    @pytest.mark.asyncio
    async def test_queue_depth_tracking(self):
        """Test Redis queue depth tracking functionality."""

        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 3

        lock_manager = LockManager(
            logger=logging.getLogger("test_queue"),
            enable_fine_grained_locks=True,
            redis_pool=mock_redis,
        )

        depth = await lock_manager.get_lock_queue_depth("test_lock")
        assert depth == 3
        mock_redis.llen.assert_awaited_once_with("lock_queue:test_lock")

    @pytest.mark.asyncio
    async def test_smart_retry_with_backoff(self):
        """Test exponential backoff with jitter in retry logic."""

        mock_redis = AsyncMock()
        mock_redis.set.side_effect = [False, False, True]
        mock_redis.llen.return_value = 2

        lock_manager = LockManager(
            logger=logging.getLogger("test_retry"),
            enable_fine_grained_locks=True,
            redis_pool=mock_redis,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            acquired = await lock_manager._acquire_lock_with_smart_retry(
                "test_lock", 10.0
            )

        assert acquired is True
        assert mock_redis.set.await_count == 3
        assert mock_sleep.await_count == 2
        assert mock_redis.lpush.await_count >= 1
        assert mock_redis.lrem.await_count >= 1

    @pytest.mark.asyncio
    async def test_queue_depth_threshold_abort(self):
        """Test early abort when queue depth exceeds threshold."""

        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 10

        lock_manager = LockManager(
            logger=logging.getLogger("test_abort"),
            enable_fine_grained_locks=True,
            redis_pool=mock_redis,
        )

        acquired = await lock_manager._acquire_lock_with_smart_retry(
            "congested_lock", 5.0
        )

        assert acquired is False
        mock_redis.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_queue_management(self):
        """Test queue management under concurrent access."""

        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        mock_redis.llen.return_value = 1

        lock_manager = LockManager(
            logger=logging.getLogger("test_concurrent"),
            enable_fine_grained_locks=True,
            redis_pool=mock_redis,
        )

        async def acquire_lock(task_name: str):
            return await lock_manager._acquire_lock_with_smart_retry(
                f"shared_lock_{task_name}", 10.0
            )

        results = await asyncio.gather(
            acquire_lock("task1"),
            acquire_lock("task2"),
            acquire_lock("task3"),
            acquire_lock("task4"),
            acquire_lock("task5"),
        )

        assert all(result is True for result in results)
        assert mock_redis.lpush.await_count == 5
        assert mock_redis.lrem.await_count == 5

    @pytest.mark.asyncio
    async def test_grace_buffer_timeout(self):
        """Test grace buffer prevents excessive waiting."""

        mock_redis = AsyncMock()
        mock_redis.set.return_value = False
        mock_redis.llen.return_value = 2

        lock_manager = LockManager(
            logger=logging.getLogger("test_grace"),
            enable_fine_grained_locks=True,
            redis_pool=mock_redis,
        )

        with patch.object(lock_manager, "_system_constants", {
            "lock_retry": {
                "max_attempts": 10,
                "backoff_delays_seconds": [1.0, 2.0, 4.0],
                "grace_buffer_seconds": 2.0,
            }
        }):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                acquired = await lock_manager._acquire_lock_with_smart_retry(
                    "timeout_lock", 5.0
                )

        assert acquired is False
        assert mock_redis.set.await_count < 10

    @pytest.mark.asyncio
    async def test_metrics_collection(self):
        """Test that retry metrics are properly collected."""

        mock_redis = AsyncMock()
        mock_redis.set.side_effect = [False, True]
        mock_redis.llen.return_value = 1

        lock_manager = LockManager(
            logger=logging.getLogger("test_metrics"),
            enable_fine_grained_locks=True,
            redis_pool=mock_redis,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            acquired = await lock_manager._acquire_lock_with_smart_retry(
                "metrics_lock", 10.0
            )

        assert acquired is True
        def metric_value(metric):
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

        attempt_metric = lock_manager.lock_retry_attempts.labels(
            lock_type=lock_manager._extract_lock_type("metrics_lock"),
            attempt_number="1",
        )
        success_metric = lock_manager.lock_retry_success.labels(
            lock_type=lock_manager._extract_lock_type("metrics_lock")
        )
        acquisition_metric = lock_manager.lock_acquisition_success.labels(
            lock_type=lock_manager._extract_lock_type("metrics_lock"),
            attempt_number="2",
        )

        assert metric_value(attempt_metric) > 0
        assert metric_value(success_metric) >= 0
        assert metric_value(acquisition_metric) > 0
