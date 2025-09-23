"""Caching helpers for high-throughput Telegram updates."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable, Optional, Tuple, TypeVar

from cachetools import TTLCache

from pokerapp.utils.redis_safeops import RedisSafeOps


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessagePayload:
    """Minimal representation of a Telegram message payload."""

    text: Optional[str]
    markup_hash: Optional[str]
    parse_mode: Optional[str]


class MessageStateCache:
    """Track the last payload sent for a ``(chat_id, message_id)`` pair.

    The cache stores the most recently successful payload for each message so that
    subsequent edits can be short-circuited when nothing has actually changed. A
    small TTL prevents the structure from growing without bounds while still
    keeping enough history to coalesce bursts of updates.
    """

    def __init__(
        self,
        *,
        maxsize: int = 2048,
        ttl: int = 900,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._cache: TTLCache[Tuple[int, int], MessagePayload] = TTLCache(
            maxsize=maxsize, ttl=ttl
        )
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._logger = logger_ or logger.getChild("message_state")

    @staticmethod
    def _key(chat_id: int, message_id: int) -> Tuple[int, int]:
        return int(chat_id), int(message_id)

    async def matches(self, chat_id: int, message_id: int, payload: MessagePayload) -> bool:
        """Return ``True`` when ``payload`` matches the cached value."""

        key = self._key(chat_id, message_id)
        async with self._lock:
            cached = self._cache.get(key)
            if cached == payload:
                self._hits += 1
                self._logger.debug(
                    "MessageStateCache hit",
                    extra={"chat_id": chat_id, "message_id": message_id},
                )
                return True
            self._misses += 1
            self._logger.debug(
                "MessageStateCache miss",
                extra={"chat_id": chat_id, "message_id": message_id},
            )
            return False

    async def update(self, chat_id: int, message_id: int, payload: MessagePayload) -> None:
        """Persist ``payload`` for the given chat/message pair."""

        key = self._key(chat_id, message_id)
        async with self._lock:
            self._cache[key] = payload
            self._logger.debug(
                "MessageStateCache update",
                extra={"chat_id": chat_id, "message_id": message_id},
            )

    async def forget(self, chat_id: int, message_id: int) -> None:
        """Remove a cached entry when the message is deleted."""

        key = self._key(chat_id, message_id)
        async with self._lock:
            if key in self._cache:
                self._cache.pop(key, None)
                self._logger.debug(
                    "MessageStateCache invalidate",
                    extra={"chat_id": chat_id, "message_id": message_id},
                )

    @property
    def stats(self) -> Dict[str, int]:
        """Expose hit/miss counters for debugging."""

        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}


T = TypeVar("T")


class PlayerReportCache:
    """Cache expensive statistics queries on a per-player basis."""

    def __init__(
        self,
        *,
        ttl: int = 120,
        maxsize: int = 1024,
        logger_: Optional[logging.Logger] = None,
        redis_ops: Optional[RedisSafeOps] = None,
    ) -> None:
        self._cache: TTLCache[int, T] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._locks: Dict[int, asyncio.Lock] = {}
        self._hits = 0
        self._misses = 0
        self._logger = logger_ or logger.getChild("player_report")
        self._redis_ops = redis_ops

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def get(
        self,
        user_id: int,
        loader: Callable[[], Awaitable[Optional[T]]],
    ) -> Optional[T]:
        """Return a cached report or populate it using ``loader``."""

        normalized_id = int(user_id)
        lock = self._get_lock(normalized_id)
        async with lock:
            if normalized_id in self._cache:
                self._hits += 1
                value = self._cache[normalized_id]
                self._logger.debug(
                    "PlayerReportCache hit", extra={"user_id": normalized_id}
                )
                return value
            self._misses += 1
            self._logger.debug(
                "PlayerReportCache miss", extra={"user_id": normalized_id}
            )
            value = await loader()
            if value is not None:
                self._cache[normalized_id] = value
            return value

    def invalidate(self, user_id: int) -> None:
        normalized_id = int(user_id)
        if normalized_id in self._cache:
            self._cache.pop(normalized_id, None)
            self._logger.debug(
                "PlayerReportCache invalidate", extra={"user_id": normalized_id}
            )

    def invalidate_many(self, user_ids: Iterable[int]) -> None:
        for user_id in user_ids:
            self.invalidate(user_id)

    @property
    def stats(self) -> Dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}
