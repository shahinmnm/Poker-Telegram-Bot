"""Caching helpers for high-throughput Telegram updates."""

from __future__ import annotations

import asyncio
import logging
import pickle
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, DefaultDict, Dict, Iterable, Optional, Tuple, TypeVar

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


class AdaptivePlayerReportCache(PlayerReportCache):
    """Adaptive cache that adjusts TTLs based on statistics events.

    The cache keeps the lightweight in-memory characteristics of
    :class:`PlayerReportCache` while adding support for event-aware time to live,
    optional persistence via :class:`~pokerapp.utils.redis_safeops.RedisSafeOps`
    and richer observability hooks.  When a statistics-changing event occurs
    (for example a finished hand or a claimed bonus) the producer calls
    :meth:`invalidate_on_event` which immediately removes any cached entry and
    stores the TTL that should be applied the next time the player report is
    generated.  Subsequent calls to :meth:`get_with_context` apply that TTL and
    record structured metrics/logging data so the behaviour can be monitored in
    production deployments.
    """

    def __init__(
        self,
        *,
        default_ttl: int = 120,
        bonus_ttl: int = 60,
        post_hand_ttl: int = 30,
        maxsize: int = 1024,
        logger_: Optional[logging.Logger] = None,
        persistent_store: Optional[RedisSafeOps] = None,
    ) -> None:
        # ``TTLCache`` does not allow per-entry TTLs so we configure it with the
        # maximum TTL we expect and rely on the adaptive layer below to expire
        # items earlier when necessary.
        super().__init__(
            ttl=max(default_ttl, bonus_ttl, post_hand_ttl),
            maxsize=maxsize,
            logger_=logger_,
        )
        self._default_ttl = max(default_ttl, 0)
        self._bonus_ttl = max(bonus_ttl, 0)
        self._post_hand_ttl = max(post_hand_ttl, 0)
        self._persistent_store = persistent_store
        self._expiry_map: Dict[int, float] = {}
        self._next_ttl: Dict[int, Tuple[Optional[str], int]] = {}
        self._event_metrics: DefaultDict[str, Dict[str, int]] = defaultdict(
            lambda: {"hits": 0, "misses": 0}
        )
        self._timer = time.monotonic

    async def get(
        self,
        user_id: int,
        loader: Callable[[], Awaitable[Optional[T]]],
    ) -> Optional[T]:
        return await self.get_with_context(user_id, loader)

    async def get_with_context(
        self,
        user_id: int,
        loader: Callable[[], Awaitable[Optional[T]]],
        *,
        event_type: Optional[str] = None,
    ) -> Optional[T]:
        ttl_event_type, ttl = self._resolve_context(user_id, event_type)
        metrics_key = ttl_event_type or "default"
        normalized_id = int(user_id)
        lock = self._get_lock(normalized_id)
        async with lock:
            if self._is_entry_valid(normalized_id):
                self._hits += 1
                self._event_metrics[metrics_key]["hits"] += 1
                self._logger.debug(
                    "AdaptivePlayerReportCache hit",
                    extra={
                        "user_id": normalized_id,
                        "event_type": ttl_event_type,
                        "ttl": ttl,
                        "source": "memory",
                    },
                )
                return self._cache[normalized_id]

            if self._persistent_store is not None:
                value = await self._load_from_persistent_store(
                    normalized_id, ttl, ttl_event_type
                )
                if value is not None:
                    self._hits += 1
                    self._event_metrics[metrics_key]["hits"] += 1
                    return value

            self._misses += 1
            self._event_metrics[metrics_key]["misses"] += 1
            self._logger.debug(
                "AdaptivePlayerReportCache miss",
                extra={
                    "user_id": normalized_id,
                    "event_type": ttl_event_type,
                    "ttl": ttl,
                },
            )
            value = await loader()
            if value is not None:
                self._store(normalized_id, value, ttl)
                await self._persist(normalized_id, value, ttl, ttl_event_type)
            return value

    def invalidate(self, user_id: int) -> None:
        normalized_id = int(user_id)
        self._next_ttl.pop(normalized_id, None)
        self._invalidate_internal(normalized_id, event_type=None)

    def invalidate_many(self, user_ids: Iterable[int]) -> None:
        for user_id in user_ids:
            self.invalidate(user_id)

    def invalidate_on_event(self, user_ids: Iterable[int], event_type: str) -> None:
        ttl = self._resolve_ttl(event_type)
        for user_id in user_ids:
            normalized_id = int(user_id)
            self._next_ttl[normalized_id] = (event_type, ttl)
            self._invalidate_internal(normalized_id, event_type=event_type)

    def metrics(self) -> Dict[str, Dict[str, int]]:
        snapshot = {key: dict(value) for key, value in self._event_metrics.items()}
        snapshot["overall"] = {"hits": self._hits, "misses": self._misses}
        return snapshot

    def _resolve_context(
        self, user_id: int, explicit_event: Optional[str]
    ) -> Tuple[Optional[str], int]:
        normalized_id = int(user_id)
        if explicit_event is not None:
            self._next_ttl.pop(normalized_id, None)
            return explicit_event, self._resolve_ttl(explicit_event)

        pending = self._next_ttl.pop(normalized_id, None)
        if pending is not None:
            return pending
        return None, self._default_ttl

    def _resolve_ttl(self, event_type: Optional[str]) -> int:
        if event_type == "bonus_claimed":
            return self._bonus_ttl or self._default_ttl
        if event_type == "hand_finished":
            return self._post_hand_ttl or self._default_ttl
        return self._default_ttl

    def _is_entry_valid(self, user_id: int) -> bool:
        if user_id not in self._cache:
            return False
        expiry = self._expiry_map.get(user_id)
        if expiry is None:
            return True
        if expiry <= self._timer():
            self._cache.pop(user_id, None)
            self._expiry_map.pop(user_id, None)
            return False
        return True

    def _store(self, user_id: int, value: T, ttl: int) -> None:
        self._cache[user_id] = value
        self._expiry_map[user_id] = self._timer() + max(ttl, 0)

    async def _load_from_persistent_store(
        self, user_id: int, ttl: int, event_type: Optional[str]
    ) -> Optional[T]:
        if self._persistent_store is None:
            return None
        try:
            payload = await self._persistent_store.safe_get(
                self._redis_key(user_id),
                log_extra={"user_id": user_id, "event_type": event_type},
            )
        except Exception:
            self._logger.exception(
                "Failed to fetch cache entry from persistent store",
                extra={"user_id": user_id, "event_type": event_type},
            )
            return None
        if not payload:
            return None
        try:
            value = self._deserialize(payload)
        except Exception:
            self._logger.exception(
                "Failed to deserialize player report cache entry",
                extra={"user_id": user_id, "event_type": event_type},
            )
            return None
        self._store(user_id, value, ttl)
        self._logger.debug(
            "AdaptivePlayerReportCache hit",
            extra={
                "user_id": user_id,
                "event_type": event_type,
                "ttl": ttl,
                "source": "persistent",
            },
        )
        return value

    async def _persist(
        self,
        user_id: int,
        value: T,
        ttl: int,
        event_type: Optional[str],
    ) -> None:
        if self._persistent_store is None:
            return
        try:
            payload = self._serialize(value)
        except Exception:
            self._logger.exception(
                "Failed to serialize player report cache entry",
                extra={"user_id": user_id, "event_type": event_type},
            )
            return
        try:
            await self._persistent_store.safe_set(
                self._redis_key(user_id),
                payload,
                expire=max(ttl, 0) or None,
                log_extra={"user_id": user_id, "event_type": event_type},
            )
        except Exception:
            self._logger.exception(
                "Failed to persist cache entry in Redis",
                extra={"user_id": user_id, "event_type": event_type},
            )

    def _invalidate_internal(self, user_id: int, *, event_type: Optional[str]) -> None:
        removed = self._cache.pop(user_id, None) is not None
        self._expiry_map.pop(user_id, None)
        log_payload: Dict[str, Any] = {"user_id": user_id, "event_type": event_type}
        if removed:
            level = self._logger.info if event_type else self._logger.debug
            message = (
                (
                    f"Player report invalidated due to event {event_type}"
                    if event_type
                    else "AdaptivePlayerReportCache invalidate"
                )
            )
            level(message, extra=log_payload)
        elif event_type:
            self._logger.info(
                f"Player report invalidated due to event {event_type}",
                extra=log_payload,
            )
        else:
            self._logger.debug(
                "AdaptivePlayerReportCache invalidate",
                extra=log_payload,
            )
        if self._persistent_store is not None:
            self._schedule_persistent_delete(user_id, event_type)

    def _schedule_persistent_delete(
        self, user_id: int, event_type: Optional[str]
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _delete() -> None:
            try:
                await self._persistent_store.safe_delete(
                    self._redis_key(user_id),
                    log_extra={"user_id": user_id, "event_type": event_type},
                )
            except Exception:
                self._logger.exception(
                    "Failed to delete cache entry in Redis",
                    extra={"user_id": user_id, "event_type": event_type},
                )

        loop.create_task(_delete())

    @staticmethod
    def _redis_key(user_id: int) -> str:
        return f"stats:{int(user_id)}"

    @staticmethod
    def _serialize(value: T) -> bytes:
        return pickle.dumps(value)

    @staticmethod
    def _deserialize(payload: Any) -> T:
        if isinstance(payload, bytes):
            return pickle.loads(payload)
        if isinstance(payload, str):
            return pickle.loads(payload.encode("latin1"))
        raise TypeError(f"Unsupported payload type for deserialization: {type(payload)!r}")
