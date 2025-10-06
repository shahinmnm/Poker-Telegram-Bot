"""Optimized async database client with caching and batching primitives."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Optional, Sequence

import asyncpg

from .cache_manager import MultiLayerCache


@dataclass(slots=True)
class CachePolicy:
    """Policy describing how query results should be cached."""

    ttl_seconds: int = 300
    category: str = "db"
    enabled: bool = True


class OptimizedDatabaseClient:
    """Async database client with connection pooling and smart caching."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 25,
        max_size: int = 50,
        cache: Optional[MultiLayerCache] = None,
        cache_policy: Optional[CachePolicy] = None,
        logger: Optional[Any] = None,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None
        self._cache = cache
        self._cache_policy = cache_policy or CachePolicy()
        self._logger = logger
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the asyncpg connection pool if required."""

        if self._pool is not None:
            return

        async with self._lock:
            if self._pool is None:
                if self._logger:
                    self._logger.info(
                        "db.connect",
                        extra={"dsn": self._dsn, "min_size": self._min_size, "max_size": self._max_size},
                    )
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    command_timeout=30,
                )

    async def close(self) -> None:
        """Dispose of the pool."""

        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        """Borrow a connection from the pool."""

        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as connection:
            yield connection

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Transaction]:
        """Provide a transaction scope using the connection pool."""

        async with self.connection() as connection:
            async with connection.transaction():
                yield connection

    # ------------------------------------------------------------------
    # Query helpers with caching
    # ------------------------------------------------------------------

    async def fetch(self, query: str, *args: Any, cache_ttl: Optional[int] = None) -> list[dict[str, Any]]:
        result = await self._execute_and_cache("fetch", query, args, cache_ttl)
        assert isinstance(result, list)
        return result

    async def fetchrow(self, query: str, *args: Any, cache_ttl: Optional[int] = None) -> Optional[dict[str, Any]]:
        result = await self._execute_and_cache("fetchrow", query, args, cache_ttl)
        return result if result else None

    async def fetchval(self, query: str, *args: Any, cache_ttl: Optional[int] = None) -> Any:
        return await self._execute_and_cache("fetchval", query, args, cache_ttl)

    async def execute(self, query: str, *args: Any) -> str:
        return await self._execute_without_cache("execute", query, args)

    async def executemany(self, query: str, args_iter: Iterable[Sequence[Any]]) -> None:
        await self._execute_without_cache("executemany", query, args_iter)

    async def batch_execute(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        """Execute multiple statements within a single transaction."""

        results: list[Any] = []
        async with self.transaction() as connection:
            for sql, params in statements:
                results.append(await connection.execute(sql, *params))
        return results

    # ------------------------------------------------------------------
    # Cache-aware execution
    # ------------------------------------------------------------------

    async def _execute_and_cache(
        self,
        op: str,
        query: str,
        args: Sequence[Any],
        cache_ttl: Optional[int],
    ) -> Any:
        should_cache = self._should_cache(query)
        ttl = cache_ttl or self._cache_policy.ttl_seconds
        category = self._detect_category(query)
        cache_key = self._build_cache_key(category, query, args)

        if should_cache and self._cache and self._cache_policy.enabled:
            cached = await self._cache.get(cache_key, category=category)
            if cached is not None:
                return cached

        records = await self._execute(op, query, args)
        payload = self._normalise_records(op, records)

        if should_cache and self._cache and self._cache_policy.enabled:
            await self._cache.set(cache_key, payload, ttl=ttl, category=category)

        return payload

    async def _execute_without_cache(
        self,
        op: str,
        query: str,
        args: Iterable[Sequence[Any]] | Sequence[Any],
    ) -> Any:
        result = await self._execute(op, query, args)
        await self.invalidate_for_mutation(query)
        return result

    async def _execute(self, op: str, query: str, args: Any) -> Any:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None

        async with self._pool.acquire() as connection:
            if op == "fetch":
                records = await connection.fetch(query, *args)
            elif op == "fetchrow":
                records = await connection.fetch(query, *args)
            elif op == "fetchval":
                records = await connection.fetch(query, *args)
            elif op == "execute":
                return await connection.execute(query, *args)
            elif op == "executemany":
                assert isinstance(args, Iterable)
                await connection.executemany(query, args)
                return None
            else:  # pragma: no cover - defensive
                raise ValueError(f"Unsupported operation: {op}")

        return records

    # ------------------------------------------------------------------
    # Cache invalidation strategies
    # ------------------------------------------------------------------

    async def invalidate_for_mutation(self, query: str) -> None:
        """Invalidate cache entries when a mutation occurs."""

        if not self._cache or not self._cache_policy.enabled:
            return

        lowered = query.strip().lower()
        affected_categories: set[str] = set()
        if lowered.startswith("update wallets") or lowered.startswith("insert into wallets"):
            affected_categories.add("wallet")
        if lowered.startswith("update player_stats") or lowered.startswith("insert into player_stats"):
            affected_categories.add("player_stats")
        if lowered.startswith("update game_history") or lowered.startswith("insert into game_history"):
            affected_categories.add("history")

        if not affected_categories:
            affected_categories.add(self._cache_policy.category)

        for category in affected_categories:
            pattern = f"{category}:*"
            await self._cache.invalidate(pattern, category=category)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _should_cache(self, query: str) -> bool:
        normalized = query.lstrip().lower()
        return normalized.startswith("select")

    def _detect_category(self, query: str) -> str:
        lowered = query.strip().lower()
        if "wallet" in lowered:
            return "wallet"
        if "player_stats" in lowered:
            return "player_stats"
        if "game_history" in lowered:
            return "history"
        return self._cache_policy.category

    def _build_cache_key(self, category: str, query: str, args: Sequence[Any]) -> str:
        digest = hashlib.sha256()
        payload = json.dumps({"query": query, "args": list(args)}, sort_keys=True, default=str)
        digest.update(payload.encode("utf-8"))
        return f"{category}:{digest.hexdigest()}"

    def _normalise_records(self, op: str, records: Any) -> Any:
        if op == "fetchval":
            if not records:
                return None
            first = records[0]
            if isinstance(first, asyncpg.Record):
                return next(iter(dict(first).values()), None)
            if isinstance(first, (list, tuple)):
                return first[0] if first else None
            return first

        if op == "fetchrow":
            if not records:
                return None
            record = records[0]
            if isinstance(record, asyncpg.Record):
                return dict(record)
            return dict(record)

        if isinstance(records, list):
            return [dict(record) for record in records]
        if isinstance(records, asyncpg.Record):
            return [dict(records)]
        if records is None:
            return []
        return [dict(row) for row in records]


__all__ = ["OptimizedDatabaseClient", "CachePolicy"]
