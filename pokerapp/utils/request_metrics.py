"""Utilities for tracking and enforcing Telegram request budgets.

The :class:`RequestMetrics` helper centralises counting of outgoing Telegram
requests so the poker bot can stay below Telegram's flood limits even when
callbacks and background jobs trigger updates at the same time.  Individual
calls are labelled using :class:`RequestCategory` values which allows
fine-grained limits – for instance we maintain a strict cap on the combined
number of turn-message and stage-transition updates per hand.

The helper is intentionally asyncio-friendly: all public methods are coroutines
guarded by an internal lock to make sure concurrent writes from different
tasks remain consistent.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, DefaultDict, Dict, Iterable, Optional


logger = logging.getLogger(__name__)


class RequestCategory(str, Enum):
    """Categorise Telegram calls so we can enforce per-phase budgets."""

    GENERAL = "general"
    TURN = "turn"
    STAGE = "stage"
    ENGINE_CRITICAL = "engine_critical"
    START_GAME = "start_game"
    STAGE_PROGRESS = "stage_progress"
    COUNTDOWN = "countdown"
    INLINE = "inline"
    PHOTO = "photo"
    DELETE = "delete"
    ANCHOR = "anchor"
    MEDIA = "media"


@dataclass(slots=True)
class _CycleSnapshot:
    """Mutable snapshot of the current request counts for a chat/game."""

    cycle_token: Optional[str]
    counts: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    total: int = 0
    # Keep a rolling log of the most recent calls for debugging/analytics.
    recent_calls: Deque[str] = field(default_factory=lambda: deque(maxlen=50))
    skipped: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))


class RequestMetrics:
    """Record outgoing Telegram calls and enforce per-hand request budgets."""

    #: How many turn/stage requests are allowed per hand.
    _TURN_STAGE_LIMIT = 10
    #: Combined key used internally to keep the aggregate counter in sync.
    _TURN_STAGE_COMBINED_KEY = "turn_stage_total"

    def __init__(self, *, logger_: Optional[logging.Logger] = None) -> None:
        self._logger = logger_ or logger.getChild("metrics")
        self._lock = asyncio.Lock()
        self._cycles: Dict[int, _CycleSnapshot] = {}

    async def start_cycle(self, chat_id: int, cycle_token: str) -> None:
        """Begin counting a new game cycle for ``chat_id``."""

        async with self._lock:
            self._cycles[chat_id] = _CycleSnapshot(cycle_token=cycle_token)
            self._logger.info(
                "Request cycle started",
                extra={"chat_id": chat_id, "cycle_token": cycle_token},
            )

    async def end_cycle(self, chat_id: int, *, cycle_token: Optional[str] = None) -> None:
        """Stop tracking the active cycle for ``chat_id`` if it matches."""

        async with self._lock:
            snapshot = self._cycles.get(chat_id)
            if snapshot is None:
                return
            if cycle_token is not None and snapshot.cycle_token != cycle_token:
                return
            self._logger.info(
                "Request cycle finished",
                extra={
                    "chat_id": chat_id,
                    "cycle_token": snapshot.cycle_token,
                    "counts": dict(snapshot.counts),
                    "total": snapshot.total,
                    "skipped": dict(snapshot.skipped),
                    "before_after_table": self._build_cycle_summary(snapshot),
                },
            )
            self._cycles.pop(chat_id, None)

    async def consume(
        self,
        *,
        chat_id: int,
        method: str,
        category: RequestCategory,
        message_id: Optional[int],
    ) -> bool:
        """Register a Telegram API call.

        Returns ``True`` if the call is within the configured budget.  When the
        combined turn/stage limit is exceeded the method returns ``False`` so
        the caller can skip the API request entirely.
        """

        async with self._lock:
            snapshot = self._cycles.setdefault(chat_id, _CycleSnapshot(cycle_token=None))

            if category in (RequestCategory.TURN, RequestCategory.STAGE):
                combined = snapshot.counts[self._TURN_STAGE_COMBINED_KEY]
                if combined >= self._TURN_STAGE_LIMIT:
                    self._logger.warning(
                        "Request budget exhausted; skipping %s",
                        method,
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "category": category.value,
                            "combined_total": combined,
                        },
                    )
                    return False
                snapshot.counts[self._TURN_STAGE_COMBINED_KEY] = combined + 1

            snapshot.counts[category.value] += 1
            snapshot.total += 1
            snapshot.recent_calls.append(
                f"{method}:{category.value}@{message_id if message_id is not None else '-'}"
            )

            self._logger.debug(
                "Recorded Telegram call",
                extra={
                    "chat_id": chat_id,
                    "method": method,
                    "category": category.value,
                    "message_id": message_id,
                    "cycle_token": snapshot.cycle_token,
                    "counts": dict(snapshot.counts),
                    "total": snapshot.total,
                },
            )

            return True

    async def record_skip(
        self,
        *,
        chat_id: int,
        category: RequestCategory,
    ) -> None:
        """Record that a potential request was skipped due to optimisation."""

        async with self._lock:
            snapshot = self._cycles.setdefault(chat_id, _CycleSnapshot(cycle_token=None))
            snapshot.skipped[category.value] += 1
            self._logger.debug(
                "Recorded skipped Telegram call",
                extra={
                    "chat_id": chat_id,
                    "category": category.value,
                    "cycle_token": snapshot.cycle_token,
                    "skipped_counts": dict(snapshot.skipped),
                },
            )

    async def snapshot(self, chat_id: int) -> Dict[str, int]:
        """Return a copy of the counters for ``chat_id``."""

        async with self._lock:
            snapshot = self._cycles.get(chat_id)
            if snapshot is None:
                return {}
            return dict(snapshot.counts)

    async def recent(self, chat_id: int) -> Iterable[str]:
        """Yield the recorded recent call descriptors for ``chat_id``."""

        async with self._lock:
            snapshot = self._cycles.get(chat_id)
            if snapshot is None:
                return []
            return list(snapshot.recent_calls)

    def record_fine_grained_lock(
        self,
        *,
        lock_type: str,
        chat_id: int,
        duration_ms: float,
        wait_time_ms: float,
        success: bool,
    ) -> None:
        """Record metrics for fine-grained lock operations."""

        self._logger.info(
            "Fine-grained lock operation",
            extra={
                "category": "lock_metrics",
                "lock_type": lock_type,
                "chat_id": chat_id,
                "duration_ms": duration_ms,
                "wait_time_ms": wait_time_ms,
                "success": success,
                "metric_type": "fine_grained_lock",
            },
        )

    def _build_cycle_summary(self, snapshot: _CycleSnapshot) -> Iterable[Dict[str, int]]:
        """Return a table comparing raw vs optimised request counts."""

        summary: Dict[str, Dict[str, int]] = {}
        for category, after in snapshot.counts.items():
            entry = summary.setdefault(category, {"after": 0, "skipped": 0})
            entry["after"] = after
        for category, skipped in snapshot.skipped.items():
            entry = summary.setdefault(category, {"after": 0, "skipped": 0})
            entry["skipped"] = skipped

        rows = []
        for category, data in sorted(summary.items()):
            before = data.get("after", 0) + data.get("skipped", 0)
            rows.append(
                {
                    "category": category,
                    "before": before,
                    "after": data.get("after", 0),
                    "skipped": data.get("skipped", 0),
                }
            )
        return rows


__all__ = ["RequestMetrics", "RequestCategory"]

