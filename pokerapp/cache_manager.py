"""Multi-layer caching utilities for the poker bot."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Generic, Optional, TypeVar

import redis.asyncio as redis


T = TypeVar("T")


@dataclass(slots=True)
class CacheConfig:
    """Configuration controlling cache behaviour."""

    l1_ttl_seconds: int = 60
    l1_max_size: int = 1024
    l2_ttl_seconds: int = 300
    enable_l1: bool = True
    enable_l2: bool = True
    key_prefix: str = "poker:cache:"
    default_ttl_seconds: int = 300


class CacheEntry(Generic[T]):
    """Representation of an item stored inside the in-memory cache."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: T, ttl: float) -> None:
        self.value = value
        self.expires_at = time.time() + ttl

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class MultiLayerCache:
    """Three tier cache composed of an LRU memory cache and Redis."""

    def __init__(
        self,
        redis_client: redis.Redis,
        config: Optional[CacheConfig] = None,
        logger: Optional[Any] = None,
    ) -> None:
        self.redis = redis_client
        self.config = config or CacheConfig()
        self.logger = logger

        self._l1_cache: Dict[str, CacheEntry[Any]] = {}
        self._l1_access_order: list[str] = []
        self._metrics: Dict[str, int | float] = {
            "l1_hits": 0,
            "l1_misses": 0,
            "l2_hits": 0,
            "l2_misses": 0,
            "db_hits": 0,
            "invalidations": 0,
        }

    async def get(self, key: str, *, category: str = "default") -> Optional[Any]:
        """Retrieve a value using the configured cache hierarchy."""

        if self.config.enable_l1:
            value = self._get_from_l1(key)
            if value is not None:
                self._metrics["l1_hits"] += 1
                return value
            self._metrics["l1_misses"] += 1

        if self.config.enable_l2:
            value = await self._get_from_l2(key)
            if value is not None:
                self._metrics["l2_hits"] += 1
                if self.config.enable_l1:
                    self._set_to_l1(key, value)
                return value
            self._metrics["l2_misses"] += 1

        return None

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: Optional[int] = None,
        category: str = "default",
    ) -> None:
        """Persist a value across the configured cache tiers."""

        ttl = ttl or self.config.default_ttl_seconds

        if self.config.enable_l1:
            self._set_to_l1(key, value, ttl=ttl)

        if self.config.enable_l2:
            await self._set_to_l2(key, value, ttl=ttl)

        if self.logger:
            self.logger.debug(
                "cache.set",
                extra={
                    "category": category,
                    "key": key,
                    "ttl": ttl,
                },
            )

    async def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], Any],
        *,
        category: str = "default",
        ttl: Optional[int] = None,
    ) -> Any:
        """Return a cached value, or compute and persist it if missing."""

        cached = await self.get(key, category=category)
        if cached is not None:
            return cached

        self._metrics["db_hits"] += 1

        if asyncio.iscoroutinefunction(compute_fn):
            value = await compute_fn()  # type: ignore[func-returns-value]
        else:
            value = compute_fn()

        await self.set(key, value, ttl=ttl, category=category)
        return value

    async def invalidate(self, pattern: str, *, category: str = "default") -> int:
        """Invalidate cached entries matching the supplied glob pattern."""

        removed = 0

        if self.config.enable_l1:
            keys = [key for key in self._l1_cache if self._matches_pattern(key, pattern)]
            for key in keys:
                self._remove_from_l1(key)
            removed += len(keys)

        if self.config.enable_l2:
            full_pattern = f"{self.config.key_prefix}{pattern}"
            cursor = b"0"
            while cursor:
                cursor, keys = await self.redis.scan(cursor=cursor, match=full_pattern, count=100)
                if keys:
                    await self.redis.delete(*keys)
                    removed += len(keys)
                if cursor in (0, b"0"):
                    break

        self._metrics["invalidations"] += 1

        if self.logger:
            self.logger.debug(
                "cache.invalidate",
                extra={
                    "category": category,
                    "pattern": pattern,
                    "removed": removed,
                },
            )

        return removed

    def get_metrics(self) -> Dict[str, int | float]:
        """Return cache hit statistics for observability dashboards."""

        total_lookups = self._metrics["l1_hits"] + self._metrics["l1_misses"]
        l1_hit_rate = (self._metrics["l1_hits"] / total_lookups * 100) if total_lookups else 0.0
        l2_denominator = self._metrics["l1_misses"] or 1
        l2_hit_rate = self._metrics["l2_hits"] / l2_denominator * 100
        overall_hit_rate = (
            (self._metrics["l1_hits"] + self._metrics["l2_hits"]) / total_lookups * 100
            if total_lookups
            else 0.0
        )

        return {
            **self._metrics,
            "l1_hit_rate": round(l1_hit_rate, 2),
            "l2_hit_rate": round(l2_hit_rate, 2),
            "overall_hit_rate": round(overall_hit_rate, 2),
            "l1_size": len(self._l1_cache),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_from_l1(self, key: str) -> Optional[Any]:
        entry = self._l1_cache.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            self._remove_from_l1(key)
            return None
        if key in self._l1_access_order:
            self._l1_access_order.remove(key)
        self._l1_access_order.append(key)
        return entry.value

    def _set_to_l1(self, key: str, value: Any, *, ttl: Optional[int] = None) -> None:
        if len(self._l1_cache) >= self.config.l1_max_size and self._l1_access_order:
            oldest_key = self._l1_access_order.pop(0)
            self._l1_cache.pop(oldest_key, None)
        self._l1_cache[key] = CacheEntry(value=value, ttl=float(ttl or self.config.l1_ttl_seconds))
        if key in self._l1_access_order:
            self._l1_access_order.remove(key)
        self._l1_access_order.append(key)

    def _remove_from_l1(self, key: str) -> None:
        self._l1_cache.pop(key, None)
        if key in self._l1_access_order:
            self._l1_access_order.remove(key)

    async def _get_from_l2(self, key: str) -> Optional[Any]:
        try:
            raw = await self.redis.get(f"{self.config.key_prefix}{key}")
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # pragma: no cover - defensive logging
            if self.logger:
                self.logger.warning("cache.l2_get_failed", extra={"error": str(exc), "key": key})
            return None

    async def _set_to_l2(self, key: str, value: Any, *, ttl: int) -> None:
        try:
            await self.redis.setex(
                f"{self.config.key_prefix}{key}",
                ttl,
                json.dumps(value, default=str),
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            if self.logger:
                self.logger.warning("cache.l2_set_failed", extra={"error": str(exc), "key": key})

    @staticmethod
    def _matches_pattern(key: str, pattern: str) -> bool:
        if "*" not in pattern:
            return key == pattern
        from fnmatch import fnmatch

        return fnmatch(key, pattern)


__all__ = ["CacheConfig", "MultiLayerCache"]
