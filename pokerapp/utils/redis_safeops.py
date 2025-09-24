"""Reliable Redis operations with structured logging and retry support."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Union

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError, NoScriptError, ResponseError, TimeoutError

from pokerapp.logging_config import ContextJsonFormatter
from pokerapp.utils.logging_helpers import add_context

MetricsRecorder = Callable[[str, float, str], None]


class RedisSafeOps:
    """Wrap an ``aioredis`` client with retry/backoff behaviour.

    The helper provides a single :meth:`call` entry point that handles
    transient connection issues, applies exponential backoff and emits
    structured log entries compatible with :class:`ContextJsonFormatter`.  The
    public ``safe_*`` convenience methods cover the most common Redis
    operations used throughout the bot.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        *,
        logger: Optional[logging.Logger] = None,
        max_retries: int = 3,
        base_backoff: float = 0.5,
        backoff_factor: float = 2.0,
        timeout_seconds: float = 5.0,
        metrics_recorder: Optional[MetricsRecorder] = None,
    ) -> None:
        self._redis = redis_client
        base_logger = logger or logging.getLogger(__name__).getChild("RedisSafeOps")
        self._logger = add_context(base_logger)
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._backoff_factor = backoff_factor
        self._timeout_seconds = timeout_seconds
        self._metrics_recorder = metrics_recorder

        # Ensure logs follow the JSON structure even when a custom logger is
        # provided without handlers.  If the application already configured
        # logging we simply propagate to the parent handlers.
        underlying_logger = (
            self._logger.logger if isinstance(self._logger, logging.LoggerAdapter) else self._logger
        )
        if not underlying_logger.handlers and not underlying_logger.propagate:
            handler = logging.StreamHandler()
            handler.setFormatter(ContextJsonFormatter())
            underlying_logger.addHandler(handler)
            underlying_logger.setLevel(logging.INFO)

    async def call(
        self,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a Redis method with retry/backoff and structured logging."""

        attempt = 0
        delay = self._base_backoff
        last_exception: Optional[BaseException] = None
        start_time = time.monotonic()
        log_extra: Optional[Dict[str, Any]] = kwargs.pop("log_extra", None)

        while attempt <= self._max_retries:
            try:
                coroutine: Awaitable[Any] = getattr(self._redis, method)(*args, **kwargs)
                result = await asyncio.wait_for(coroutine, timeout=self._timeout_seconds)
                self._record_metrics(method, time.monotonic() - start_time, "success")
                return result
            except (ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
                last_exception = exc
                attempt += 1
                payload = {
                    "method": method,
                    "redis_args": self._truncate_args(args),
                    "attempt": attempt,
                    "max_attempts": self._max_retries + 1,
                    "error_type": exc.__class__.__name__,
                }
                if log_extra:
                    payload.update(log_extra)
                self._logger.warning("Redis connection issue", extra=payload)
                if attempt > self._max_retries:
                    break
                await asyncio.sleep(delay)
                delay *= self._backoff_factor
            except NoScriptError:
                self._record_metrics(method, time.monotonic() - start_time, "no_script")
                payload = {
                    "method": method,
                    "redis_args": self._truncate_args(args),
                }
                if log_extra:
                    payload.update(log_extra)
                self._logger.info("Redis script missing, reloading", extra=payload)
                raise
            except ResponseError as exc:
                last_exception = exc
                payload = {
                    "method": method,
                    "redis_args": self._truncate_args(args),
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                }
                if log_extra:
                    payload.update(log_extra)
                self._logger.error("Redis response error", extra=payload)
                break

        if last_exception is not None:
            self._record_metrics(method, time.monotonic() - start_time, "failure")
            raise last_exception

        # If we reach here there was no exception and no result (possible when
        # retries exhausted without assigning ``last_exception``).  Raise a
        # TimeoutError to signal the failure explicitly.
        raise TimeoutError("Redis operation timed out without explicit error")

    async def safe_get(
        self, key: str, *, log_extra: Optional[Dict[str, Any]] = None
    ) -> Optional[Union[bytes, str]]:
        return await self.call("get", key, log_extra=log_extra)

    async def safe_set(
        self,
        key: str,
        value: Any,
        expire: Optional[int] = None,
        *,
        log_extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        result = await self.call("set", key, value, ex=expire, log_extra=log_extra)
        return bool(result)

    async def safe_delete(
        self, *keys: str, log_extra: Optional[Dict[str, Any]] = None
    ) -> int:
        result = await self.call("delete", *keys, log_extra=log_extra)
        return int(result or 0)

    async def safe_lpush(
        self, key: str, *values: Any, log_extra: Optional[Dict[str, Any]] = None
    ) -> int:
        return int(await self.call("lpush", key, *values, log_extra=log_extra) or 0)

    async def safe_rpop(
        self, key: str, *, log_extra: Optional[Dict[str, Any]] = None
    ) -> Optional[Any]:
        return await self.call("rpop", key, log_extra=log_extra)

    async def safe_exists(
        self, key: str, *, log_extra: Optional[Dict[str, Any]] = None
    ) -> bool:
        return bool(await self.call("exists", key, log_extra=log_extra))

    async def safe_zadd(
        self,
        key: str,
        mapping: dict[str, Union[int, float]],
        *,
        log_extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        return int(
            await self.call("zadd", key, mapping, log_extra=log_extra) or 0
        )

    async def safe_zrangebyscore(
        self,
        key: str,
        min_score: Union[int, float, str],
        max_score: Union[int, float, str],
        *,
        log_extra: Optional[Dict[str, Any]] = None,
    ) -> Sequence[Any]:
        result = await self.call(
            "zrangebyscore", key, min_score, max_score, log_extra=log_extra
        )
        return result or []

    async def safe_zrem(
        self, key: str, *members: Any, log_extra: Optional[Dict[str, Any]] = None
    ) -> int:
        return int(
            await self.call("zrem", key, *members, log_extra=log_extra) or 0
        )

    async def safe_zpopmin(
        self, key: str, count: int, *, log_extra: Optional[Dict[str, Any]] = None
    ) -> Sequence[Any]:
        return await self.call("zpopmin", key, count, log_extra=log_extra)

    async def safe_hgetall(
        self, key: str, *, log_extra: Optional[Dict[str, Any]] = None
    ) -> dict[Any, Any]:
        result = await self.call("hgetall", key, log_extra=log_extra)
        return result or {}

    async def safe_hset(
        self,
        key: str,
        mapping: dict[str, Any],
        *,
        log_extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        return int(
            await self.call("hset", key, mapping=mapping, log_extra=log_extra) or 0
        )

    async def safe_expire(
        self, key: str, seconds: int, *, log_extra: Optional[Dict[str, Any]] = None
    ) -> bool:
        return bool(await self.call("expire", key, seconds, log_extra=log_extra))

    async def safe_mset(
        self, mapping: dict[str, Any], *, log_extra: Optional[Dict[str, Any]] = None
    ) -> bool:
        return bool(await self.call("mset", mapping=mapping, log_extra=log_extra))

    async def safe_smembers(
        self, key: str, *, log_extra: Optional[Dict[str, Any]] = None
    ) -> Sequence[Any]:
        result = await self.call("smembers", key, log_extra=log_extra)
        return result or []

    async def safe_sadd(
        self, key: str, *members: Any, log_extra: Optional[Dict[str, Any]] = None
    ) -> int:
        return int(
            await self.call("sadd", key, *members, log_extra=log_extra) or 0
        )

    def _record_metrics(self, method: str, elapsed: float, status: str) -> None:
        if self._metrics_recorder is None:
            return
        try:
            self._metrics_recorder(method, elapsed, status)
        except Exception:  # pragma: no cover - metric failures shouldn't bubble
            self._logger.debug(
                "Redis metrics recorder raised",
                extra={"method": method, "status": status},
            )

    @staticmethod
    def _truncate_args(args: Sequence[Any], max_length: int = 5) -> Sequence[Any]:
        if len(args) <= max_length:
            return args
        return tuple(list(args[: max_length - 1]) + ["<truncated>"])


__all__ = ["RedisSafeOps"]
