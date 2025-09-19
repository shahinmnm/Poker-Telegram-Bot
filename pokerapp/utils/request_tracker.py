import asyncio
import datetime as _dt
import logging
import math
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


logger = logging.getLogger(__name__)


@dataclass
class RequestStats:
    """Simple counter bucket for tracked Telegram API calls."""

    turn: int = 0
    stage: int = 0
    inline: int = 0
    countdown: int = 0

    def total(self) -> int:
        return self.turn + self.stage + self.inline + self.countdown

    def increment(self, category: str) -> None:
        if category == "turn":
            self.turn += 1
        elif category == "stage":
            self.stage += 1
        elif category == "inline":
            self.inline += 1
        elif category == "countdown":
            self.countdown += 1
        else:
            raise ValueError(f"Unknown request category: {category}")

    def decrement(self, category: str) -> None:
        if category == "turn":
            self.turn = max(self.turn - 1, 0)
        elif category == "stage":
            self.stage = max(self.stage - 1, 0)
        elif category == "inline":
            self.inline = max(self.inline - 1, 0)
        elif category == "countdown":
            self.countdown = max(self.countdown - 1, 0)
        else:
            raise ValueError(f"Unknown request category: {category}")

    def as_dict(self) -> Dict[str, int]:
        return {
            "turn": self.turn,
            "stage": self.stage,
            "inline": self.inline,
            "countdown": self.countdown,
            "total": self.total(),
        }


class RequestTracker:
    """Concurrency-safe accounting for message-related Telegram requests.

    A shared ``RequestTracker`` instance is responsible for ensuring that the
    sequence of calls tied to a single poker hand stays within a predefined
    budget.  Each tracked call category (turn prompts, street transitions,
    inline keyboard refreshes, and countdown updates) increments a counter. If
    the cumulative total for a ``(chat_id, round_id)`` pair would exceed the
    configured limit, ``try_consume`` refuses the reservation and callers must
    skip the Telegram request.
    """

    VERBOSE_ENV_VAR = "POKERBOT_REQUEST_TRACKER_VERBOSE"

    def __init__(self, *, limit: int = 10, info_threshold: Optional[float] = 0.75) -> None:
        self._limit = limit
        self._lock = asyncio.Lock()
        self._stats: Dict[Tuple[int, str], RequestStats] = {}
        self._history: Dict[Tuple[int, str], list] = {}
        if info_threshold is None or limit <= 0:
            self._info_threshold: Optional[int] = None
        else:
            if not 0 < info_threshold <= 1:
                raise ValueError("info_threshold must be between 0 and 1 when provided")
            self._info_threshold = min(limit, max(1, math.ceil(limit * info_threshold)))

    @staticmethod
    def _key(chat_id: int, round_id: str) -> Tuple[int, str]:
        return int(chat_id), round_id

    async def try_consume(self, chat_id: int, round_id: Optional[str], category: str) -> bool:
        """Attempt to reserve room in the per-round budget.

        Returns ``True`` when the request is accounted for, ``False`` when the
        limit would be exceeded.  When ``round_id`` is missing, the operation is
        treated as untracked and always allowed.
        """

        if not round_id:
            return True
        key = self._key(chat_id, round_id)
        async with self._lock:
            stats = self._stats.setdefault(key, RequestStats())
            prior_total = stats.total()
            if prior_total >= self._limit:
                logger.info(
                    "Request budget exhausted",
                    extra={
                        "chat_id": chat_id,
                        "round_id": round_id,
                        "category": category,
                        "limit": self._limit,
                        "stats": stats.as_dict(),
                    },
                )
                return False
            stats.increment(category)
            current_total = stats.total()
            stats_snapshot = stats.as_dict()
            self._history.setdefault(key, []).append(
                {
                    "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "category": category,
                    "stats": stats_snapshot,
                }
            )
            payload = {
                "chat_id": chat_id,
                "round_id": round_id,
                "category": category,
                "stats": stats_snapshot,
                "limit": self._limit,
            }
            if (
                self._info_threshold is not None
                and prior_total < self._info_threshold <= current_total
            ):
                logger.info(
                    "Telegram request usage nearing limit",
                    extra={**payload, "trigger": "threshold"},
                )
            if self._verbose_logging_enabled():
                logger.info(
                    "Telegram request reservation (verbose)",
                    extra={**payload, "trigger": "verbose"},
                )
            logger.debug(
                "Recorded Telegram request",
                extra=payload,
            )
            return True

    async def release(self, chat_id: int, round_id: Optional[str], category: str) -> None:
        """Undo a previously reserved request when no API call was made."""

        if not round_id:
            return
        key = self._key(chat_id, round_id)
        async with self._lock:
            stats = self._stats.get(key)
            if not stats:
                return
            stats.decrement(category)
            logger.debug(
                "Released Telegram request reservation",
                extra={
                    "chat_id": chat_id,
                    "round_id": round_id,
                    "category": category,
                    "stats": stats.as_dict(),
                },
            )

    async def snapshot(self, chat_id: int, round_id: Optional[str]) -> RequestStats:
        """Return a copy of the current statistics for inspection."""

        if not round_id:
            return RequestStats()
        key = self._key(chat_id, round_id)
        async with self._lock:
            stats = self._stats.get(key)
            if not stats:
                return RequestStats()
            return RequestStats(
                turn=stats.turn,
                stage=stats.stage,
                inline=stats.inline,
                countdown=stats.countdown,
            )

    async def reset(self, chat_id: int, round_id: Optional[str]) -> None:
        if not round_id:
            return
        key = self._key(chat_id, round_id)
        async with self._lock:
            self._stats.pop(key, None)
            self._history.pop(key, None)

    async def history(self, chat_id: int, round_id: Optional[str]) -> list:
        if not round_id:
            return []
        key = self._key(chat_id, round_id)
        async with self._lock:
            return list(self._history.get(key, []))

    @property
    def limit(self) -> int:
        return self._limit

    @classmethod
    def _verbose_logging_enabled(cls) -> bool:
        raw = os.getenv(cls.VERBOSE_ENV_VAR, "")
        return raw.strip().lower() in {"1", "true", "yes", "on"}
