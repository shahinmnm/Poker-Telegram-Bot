"""Metrics collector for fine-grained lock rollout monitoring."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from psycopg_pool import AsyncConnectionPool

from pokerapp.feature_flags import FeatureFlagManager


@dataclass
class RolloutMetrics:
    """Track rollout health metrics."""

    chat_id: int
    lock_wait_times: List[float] = field(default_factory=list)
    lock_hold_times: List[float] = field(default_factory=list)
    lock_errors: int = 0
    action_durations: List[float] = field(default_factory=list)
    action_successes: int = 0
    action_failures: int = 0
    window_start: float = field(default_factory=time.time)

    def is_healthy(self) -> bool:
        """Check if metrics indicate healthy rollout."""

        total_actions = self.action_successes + self.action_failures
        if total_actions > 0:
            error_rate = self.action_failures / total_actions
            if error_rate > 0.05:
                return False

        total_locks = len(self.lock_wait_times) + self.lock_errors
        if total_locks > 0:
            lock_error_rate = self.lock_errors / total_locks
            if lock_error_rate > 0.01:
                return False

        if self.action_durations:
            avg_duration = sum(self.action_durations) / len(self.action_durations)
            if avg_duration > 0.2:
                return False

        return True

    def reset(self) -> None:
        """Reset metrics for new window."""

        self.lock_wait_times.clear()
        self.lock_hold_times.clear()
        self.lock_errors = 0
        self.action_durations.clear()
        self.action_successes = 0
        self.action_failures = 0
        self.window_start = time.time()


class RolloutMonitor:
    """Monitor rollout health and trigger rollbacks."""

    def __init__(
        self,
        *,
        db_pool: AsyncConnectionPool,
        feature_flags: FeatureFlagManager,
        logger: logging.Logger,
        window_seconds: int = 60,
        unhealthy_threshold: int = 3,
    ):
        self._db_pool = db_pool
        self._feature_flags = feature_flags
        self._logger = logger
        self._window_seconds = window_seconds
        self._unhealthy_threshold = unhealthy_threshold

        self._metrics: Dict[int, RolloutMetrics] = {}
        self._unhealthy_windows: Dict[int, int] = defaultdict(int)
        self._monitor_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start background monitoring task."""

        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop monitoring task."""

        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

    def iter_metrics(self) -> List[RolloutMetrics]:
        """Return a snapshot of the current per-chat metrics."""

        return list(self._metrics.values())

    def record_lock_metrics(
        self,
        chat_id: int,
        wait_time: float,
        hold_time: float,
        success: bool,
    ) -> None:
        """Record lock acquisition metrics."""

        metrics = self._metrics.setdefault(chat_id, RolloutMetrics(chat_id=chat_id))

        if success:
            metrics.lock_wait_times.append(wait_time)
            metrics.lock_hold_times.append(hold_time)
        else:
            metrics.lock_errors += 1

    def record_action_metrics(
        self,
        chat_id: int,
        duration: float,
        success: bool,
    ) -> None:
        """Record player action metrics."""

        metrics = self._metrics.setdefault(chat_id, RolloutMetrics(chat_id=chat_id))
        metrics.action_durations.append(duration)

        if success:
            metrics.action_successes += 1
        else:
            metrics.action_failures += 1

    async def _monitor_loop(self) -> None:
        """Background task to check metrics and trigger rollbacks."""

        while True:
            try:
                await asyncio.sleep(self._window_seconds)
                await self._check_health()
            except asyncio.CancelledError:
                break
            except Exception:  # pragma: no cover - background safeguard
                self._logger.exception("Error in rollout monitor loop")

    async def _check_health(self) -> None:
        """Check all chat metrics and trigger rollback if needed."""

        unhealthy_chats = []

        for chat_id, metrics in list(self._metrics.items()):
            age = time.time() - metrics.window_start
            if age < self._window_seconds:
                continue

            if not metrics.is_healthy():
                self._unhealthy_windows[chat_id] += 1

                self._logger.warning(
                    "Unhealthy rollout metrics detected",
                    extra={
                        "chat_id": chat_id,
                        "consecutive_unhealthy_windows": self._unhealthy_windows[chat_id],
                        "error_rate": (
                            metrics.action_failures
                            / max(1, metrics.action_successes + metrics.action_failures)
                        ),
                        "lock_error_rate": (
                            metrics.lock_errors
                            / max(1, len(metrics.lock_wait_times) + metrics.lock_errors)
                        ),
                    },
                )

                if self._unhealthy_windows[chat_id] >= self._unhealthy_threshold:
                    unhealthy_chats.append(chat_id)
            else:
                self._unhealthy_windows[chat_id] = 0

            metrics.reset()

        if unhealthy_chats:
            await self._trigger_rollback(unhealthy_chats)

    async def _trigger_rollback(self, unhealthy_chats: List[int]) -> None:
        """Rollback fine-grained locks by reducing percentage."""

        self._logger.critical(
            "TRIGGERING AUTOMATIC ROLLBACK",
            extra={
                "reason": "unhealthy_metrics",
                "affected_chats": len(unhealthy_chats),
                "sample_chat_ids": unhealthy_chats[:5],
            },
        )

        current_percentage = self._feature_flags.rollout_percentage
        new_percentage = max(0, current_percentage // 2)

        async with self._db_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE system_config
                   SET value = jsonb_set(
                       value,
                       '{lock_manager,rollout_percentage}',
                       to_jsonb($1::int)
                   )
                 WHERE key = 'system_constants'
                """,
                (new_percentage,),
            )

        await self._feature_flags.reload_config()

        self._logger.critical(
            "Rollback completed",
            extra={
                "old_percentage": current_percentage,
                "new_percentage": new_percentage,
            },
        )


__all__ = ["RolloutMetrics", "RolloutMonitor"]
