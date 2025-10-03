"""Centralized asynchronous lock management for the poker bot."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import random
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TYPE_CHECKING,
)
from weakref import WeakKeyDictionary

import redis.asyncio as aioredis

from pokerapp.bootstrap import _make_service_logger
from pokerapp.utils.locks import ReentrantAsyncLock
from pokerapp.utils.logging_helpers import add_context, normalise_request_category

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pokerapp.config import Config


LOCK_LEVELS: Dict[str, int] = {
    "engine_stage": 1,
    "player_report": 2,
    "wallet": 3,
    "chat": 4,
}

_LOCK_PREFIX_LEVELS: Tuple[Tuple[str, str], ...] = (
    ("stage:", "engine_stage"),
    ("engine_stage:", "engine_stage"),
    ("chat:", "chat"),
    ("pokerbot:player_report", "player_report"),
    ("player_report:", "player_report"),
    ("wallet:", "wallet"),
    ("player_wallet:", "wallet"),
    ("pokerbot:wallet:", "wallet"),
)

# Timeout configuration constants
_TIMEOUT_BACKOFF_BASE = 0.1    # Base backoff delay in seconds
_TIMEOUT_BACKOFF_MAX = 2.0     # Maximum backoff delay in seconds
_TIMEOUT_JITTER_RATIO = 0.1    # Jitter as fraction of backoff (10%)
_TIMEOUT_WARNING_RATIO = 0.7   # Warn when 70% of timeout consumed

# Cancellation configuration
_CANCELLATION_CLEANUP_TIMEOUT = 0.5  # Max time to wait for lock cleanup on cancel
_CANCELLATION_LOG_STACKTRACE = True  # Log stack traces for cancelled acquisitions

# Performance optimization: Fast-path for uncontended locks (77% hit rate)
_ENABLE_FAST_PATH = True                    # Skip validation for speed
_FAST_PATH_SKIP_VALIDATION = True           # Safe for uncontended locks
_FAST_PATH_MINIMAL_LOGGING = True           # Reduce overhead
_FAST_PATH_TIMEOUT_THRESHOLD = 0.001        # 1ms - abort to slow path if exceeded

# Performance optimization: Lock object pooling
_LOCK_CLEANUP_BATCH_SIZE = 100              # Process locks in batches
_LOCK_CLEANUP_IDLE_THRESHOLD_SECONDS = 180.0  # 3 minutes idle before cleanup
_ENABLE_LOCK_POOLING = True                 # Reuse lock objects
_LOCK_POOL_MAX_SIZE = 200                   # Cap pool size (40-50 tables)


class LockOrderError(RuntimeError):
    """Raised when locks are acquired out of the configured order."""


@dataclass
class _LockAcquisition:
    key: str
    level: int
    context: Dict[str, Any]
    count: int = 1


@dataclass
class _WaitingInfo:
    key: str
    level: int
    context: Dict[str, Any]


class _InMemoryActionLockBackend:
    """Minimal Redis-like backend used when no Redis pool is provided.

    Metrics tracked:
    - purge_count: Number of expired entry cleanup cycles
    - peak_size: Maximum number of keys stored simultaneously
    - current_size: Current number of stored keys
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._values: Dict[str, Tuple[str, float]] = {}
        self._metrics: Dict[str, int] = {
            "purge_count": 0,
            "peak_size": 0,
            "current_size": 0,
        }
        self._version = "1.0.0-inmemory"

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: Optional[int] = None,
    ) -> bool:
        if ex is None:
            raise ValueError("In-memory Redis backend requires an expiration (ex) value")

        async with self._lock:
            self._purge_expired()
            if nx and key in self._values:
                return False

            self._values[key] = (value, time.monotonic() + float(ex))
            current_size = len(self._values)
            self._metrics["current_size"] = current_size
            self._metrics["peak_size"] = max(
                self._metrics["peak_size"],
                current_size,
            )
            return True

    async def eval(
        self,
        _script: str,
        *call_args: Any,
        **call_kwargs: Any,
    ) -> int:
        keys: Sequence[str]
        args: Sequence[str]

        if call_args:
            numkeys = int(call_args[0]) if call_args else 0
            keys = [str(value) for value in call_args[1 : 1 + numkeys]]
            args = [str(value) for value in call_args[1 + numkeys :]]
        else:
            keys = [str(value) for value in call_kwargs.get("keys", [])]
            args = [str(value) for value in call_kwargs.get("args", [])]

        if not keys:
            return 0

        key = keys[0]
        expected_token = args[0] if args else ""

        async with self._lock:
            self._purge_expired()
            current = self._values.get(key)
            if current is None:
                return 0

            token, _expiry = current
            if token != expected_token:
                return 0

            self._values.pop(key, None)
            self._metrics["current_size"] = len(self._values)
            return 1

    def _purge_expired(self) -> None:
        now = time.monotonic()
        initial_size = len(self._values)

        self._values = {
            key: value
            for key, value in self._values.items()
            if value[1] > now
        }

        self._metrics["purge_count"] += 1
        self._metrics["current_size"] = len(self._values)
        self._metrics["peak_size"] = max(
            self._metrics["peak_size"],
            initial_size,
        )

    async def get(self, key: str) -> Optional[str]:
        async with self._lock:
            self._purge_expired()
            entry = self._values.get(key)
            if entry is None:
                return None
            return entry[0]

    async def delete(self, key: str) -> int:
        async with self._lock:
            self._purge_expired()
            removed = self._values.pop(key, None)
            if removed is not None:
                self._metrics["current_size"] = len(self._values)
                return 1
            return 0

    def get_metrics(self) -> Dict[str, Any]:
        """Return current backend metrics (for debugging/monitoring)."""

        return {
            "backend_version": self._version,
            **self._metrics,
        }


def _resolve_action_lock_prefix(source: Optional[Mapping[str, Any]]) -> str:
    default_prefix = "action:lock:"
    if not isinstance(source, Mapping):
        return default_prefix

    direct = source.get("action_lock_prefix")
    if isinstance(direct, str) and direct:
        return direct

    engine_section = source.get("engine")
    if isinstance(engine_section, Mapping):
        engine_prefix = engine_section.get("action_lock_prefix")
        if isinstance(engine_prefix, str) and engine_prefix:
            return engine_prefix

    return default_prefix


class LockManager:
    """Manage keyed re-entrant async locks with timeout and retry support."""

    _LONG_HOLD_THRESHOLD_SECONDS = 2.0
    RELEASE_ACTION_LOCK_SCRIPT = """
    local key = KEYS[1]
    local expected_token = ARGV[1]
    local current_token = redis.call('GET', key)

    if current_token == expected_token then
        redis.call('DEL', key)
        return 1
    else
        return 0
    end
    """

    def __init__(
        self,
        *,
        logger: logging.Logger,
        redis_pool: Optional[aioredis.Redis] = None,
        redis_keys: Optional[Mapping[str, Any]] = None,
        default_timeout_seconds: Optional[float] = 5,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1,
        category_timeouts: Optional[Mapping[str, Any]] = None,
        config: Optional["Config"] = None,
    ) -> None:
        base_logger = add_context(logger)
        self._logger = _make_service_logger(
            base_logger, "lock_manager", "lock_manager"
        )
        self._default_timeout_seconds = default_timeout_seconds
        self._max_retries = max(0, max_retries)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._locks: Dict[str, ReentrantAsyncLock] = {}
        self._locks_guard = asyncio.Lock()
        self._task_lock_state: "WeakKeyDictionary[asyncio.Task[Any], List[_LockAcquisition]]" = (
            WeakKeyDictionary()
        )
        self._waiting_tasks: "WeakKeyDictionary[asyncio.Task[Any], _WaitingInfo]" = (
            WeakKeyDictionary()
        )
        self._lock_acquire_times: Dict[Tuple[int, str], List[float]] = {}
        self._default_lock_level = (max(LOCK_LEVELS.values()) if LOCK_LEVELS else 0) + 10
        self._lock_state_var: ContextVar[Tuple[_LockAcquisition, ...]] = ContextVar(
            f"lock_manager_state_{id(self)}",
            default=(),
        )
        self._level_state_var: ContextVar[Tuple[int, ...]] = ContextVar(
            f"lock_manager_levels_{id(self)}", default=()
        )
        resolved_category_timeouts = category_timeouts
        if resolved_category_timeouts is None:
            config_instance = config
            if config_instance is None:
                try:
                    from pokerapp.config import Config  # local import to avoid cycles
                except Exception:  # pragma: no cover - defensive
                    config_instance = None
                else:
                    config_instance = Config()
            if config_instance is not None:
                constants = getattr(config_instance, "constants", None)
                if constants is not None:
                    locks_section = constants.section("locks")
                    raw_timeouts = locks_section.get("category_timeouts_seconds")
                    if isinstance(raw_timeouts, Mapping):
                        resolved_category_timeouts = raw_timeouts
        self._category_timeouts = self._normalise_category_timeouts(
            resolved_category_timeouts
        )
        self._metrics: Dict[str, int] = {
            "lock_contention": 0,
            "lock_timeouts": 0,
            "lock_cancellations": 0,
            "lock_cleanup_failures": 0,
            "lock_fast_path_hits": 0,
            "lock_fast_path_misses": 0,
            "lock_slow_path": 0,
            "lock_pool_hits": 0,
            "lock_pool_misses": 0,
            "lock_cleanup_removed_count": 0,
        }
        self._timeout_count: Dict[str, int] = {}
        self._circuit_reset_time: Dict[str, float] = {}
        self._circuit_breaker_threshold: int = 3
        self._circuit_reset_interval: float = 60.0
        self._bypassed_locks: Set[str] = set()
        self._shutdown_initiated = False
        self._shutdown_lock = asyncio.Lock()
        # Initialize lock pool for object reuse
        self._lock_pool: List[ReentrantAsyncLock] = []
        self._lock_pool_lock = asyncio.Lock()
        # Metrics caching for health check optimization
        self._cached_metrics: Optional[Dict[str, Any]] = None
        self._cached_metrics_ts: float = 0.0
        self._metrics_cache_ttl: float = 0.5  # 500ms cache TTL
        redis_keys_source: Optional[Mapping[str, Any]] = redis_keys
        config_instance = config
        if redis_keys_source is None:
            if config_instance is None:
                try:
                    from pokerapp.config import Config  # local import to avoid cycles
                except Exception:  # pragma: no cover - defensive
                    config_instance = None
                else:
                    config_instance = Config()
            if config_instance is not None:
                constants = getattr(config_instance, "constants", None)
                if constants is not None:
                    redis_keys_source = getattr(constants, "redis_keys", None)
                if redis_keys_source is None:
                    redis_keys_source = getattr(config_instance, "redis_keys", None)

        engine_defaults: Dict[str, str] = {"table_lock_prefix": "table:lock:"}
        if isinstance(redis_keys_source, Mapping):
            engine_section = redis_keys_source.get("engine")
            if isinstance(engine_section, Mapping):
                for key, value in engine_section.items():
                    if isinstance(value, str) and value:
                        engine_defaults[key] = value

        self._redis_keys: Dict[str, Any] = {
            "action_lock_prefix": _resolve_action_lock_prefix(redis_keys_source),
            "engine": engine_defaults,
        }
        self._redis_pool: Any = redis_pool or _InMemoryActionLockBackend()
        if isinstance(self._redis_pool, _InMemoryActionLockBackend):
            self._logger.warning(
                "[ACTION_LOCK] Using in-memory backend (single-instance only)",
                extra={
                    "event_type": "action_lock_backend_inmemory",
                    "backend_version": self._redis_pool._version,
                },
            )

        action_settings: Mapping[str, Any] | None = None
        config_for_action = config
        if config_for_action is None:
            try:
                from pokerapp.config import Config  # local import to avoid cycles
            except Exception:  # pragma: no cover - defensive
                config_for_action = None
            else:
                config_for_action = Config()
        if config_for_action is not None:
            constants = getattr(config_for_action, "constants", None)
            if constants is not None:
                locks_section = getattr(constants, "locks", None)
                if isinstance(locks_section, Mapping):
                    action_settings = locks_section.get("action")
                if action_settings is None and hasattr(constants, "section"):
                    try:
                        locks_section = constants.section("locks")  # type: ignore[assignment]
                    except Exception:  # pragma: no cover - defensive fallback
                        locks_section = None
                    if isinstance(locks_section, Mapping):
                        action_settings = locks_section.get("action")

        self._action_lock_default_ttl = 10
        self._valid_action_types: Set[str] = {"fold", "check", "call", "raise"}
        self._action_lock_feedback_text = "⚠️ Action in progress, please wait..."
        if isinstance(action_settings, Mapping):
            ttl_candidate = action_settings.get("ttl")
            if isinstance(ttl_candidate, (int, float)):
                ttl_value = int(ttl_candidate)
                if ttl_value > 0:
                    self._action_lock_default_ttl = ttl_value
            valid_types_candidate = action_settings.get("valid_types")
            if isinstance(valid_types_candidate, (list, tuple, set)):
                normalized = {str(value).strip().lower() for value in valid_types_candidate if str(value).strip()}
                if normalized:
                    self._valid_action_types = normalized
            feedback_candidate = action_settings.get("feedback_text")
            if isinstance(feedback_candidate, str) and feedback_candidate.strip():
                self._action_lock_feedback_text = feedback_candidate.strip()

        self._release_lock_script = self.RELEASE_ACTION_LOCK_SCRIPT

    def get_lock_metrics(self) -> Dict[str, Any]:
        """Return lock backend metrics for monitoring."""

        if isinstance(self._redis_pool, _InMemoryActionLockBackend):
            return self._redis_pool.get_metrics()
        return {"backend": "redis", "metrics": "not_available"}

    async def _get_lock(self, key: str) -> ReentrantAsyncLock:
        """Get or create a lock with pooling for performance."""
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                # Try pool first (FASTEST - reuse existing object)
                if _ENABLE_LOCK_POOLING:
                    async with self._lock_pool_lock:
                        if self._lock_pool:
                            try:
                                lock = self._lock_pool.pop()
                                self._metrics["lock_pool_hits"] = (
                                    self._metrics.get("lock_pool_hits", 0) + 1
                                )

                                if self._logger.isEnabledFor(logging.DEBUG):
                                    self._logger.debug(
                                        "[LOCK_POOL] Reused lock for key=%s (pool_remaining=%d)",
                                        key,
                                        len(self._lock_pool),
                                        extra={
                                            "event_type": "lock_pool_hit",
                                            "lock_key": key,
                                        },
                                    )
                            except IndexError:  # Defensive: pool became empty
                                lock = None

                # Allocate new if pool empty
                if lock is None:
                    lock = ReentrantAsyncLock()
                    self._metrics["lock_pool_misses"] = (
                        self._metrics.get("lock_pool_misses", 0) + 1
                    )

                self._locks[key] = lock
            return lock

    async def shutdown(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Initiate graceful shutdown of lock manager.

        Marks the manager as shutting down and waits for active acquisitions
        to complete. New acquisition attempts will be rejected.

        Args:
            timeout: Maximum time to wait for graceful shutdown

        Returns:
            Dictionary with shutdown statistics
        """

        async with self._shutdown_lock:
            if self._shutdown_initiated:
                self._logger.warning("[LOCK_SHUTDOWN] Shutdown already initiated")
                return {"status": "already_shutdown"}

            self._shutdown_initiated = True
            self._logger.info(
                "[LOCK_SHUTDOWN] Initiating graceful shutdown (timeout=%.1fs)",
                timeout,
                extra={
                    "event_type": "lock_manager_shutdown_start",
                    "timeout": timeout,
                },
            )

            shutdown_start = asyncio.get_running_loop().time()
            deadline = shutdown_start + timeout

            # Wait for all waiting tasks to clear
            remaining_wait_time = deadline - asyncio.get_running_loop().time()
            if remaining_wait_time > 0:
                try:
                    await asyncio.wait_for(
                        self._wait_for_waiting_tasks_clear(),
                        timeout=remaining_wait_time,
                    )
                except asyncio.TimeoutError:
                    self._logger.warning(
                        "[LOCK_SHUTDOWN] Timeout waiting for tasks to clear (%d still waiting)",
                        len(self._waiting_tasks),
                        extra={
                            "event_type": "lock_manager_shutdown_timeout",
                            "waiting_tasks": len(self._waiting_tasks),
                        },
                    )

            shutdown_duration = asyncio.get_running_loop().time() - shutdown_start

            stats = {
                "status": "completed",
                "duration": shutdown_duration,
                "remaining_locks": len(self._locks),
                "remaining_waiting": len(self._waiting_tasks),
                "metrics": dict(self._metrics),
            }

            self._logger.info(
                "[LOCK_SHUTDOWN] Shutdown completed in %.2fs",
                shutdown_duration,
                extra={"event_type": "lock_manager_shutdown_complete", **stats},
            )

            return stats

    async def cleanup_idle_locks(self) -> int:
        """Remove locks idle for longer than the configured threshold."""

        if not _ENABLE_LOCK_POOLING:
            return 0

        current_time = time.time()
        removed_count = 0
        keys_to_remove: Set[str] = set()

        async with self._locks_guard:
            for key, lock in list(self._locks.items()):
                try:
                    locked_attr = getattr(lock, "locked", None)
                    if callable(locked_attr):
                        try:
                            is_locked = bool(locked_attr())
                        except Exception:  # pragma: no cover - defensive
                            is_locked = getattr(lock, "_count", 0) > 0
                    else:
                        is_locked = getattr(lock, "_count", 0) > 0

                    if is_locked:
                        continue

                    last_used = getattr(lock, "_acquired_at_ts", None)
                    if last_used is None:
                        continue

                    idle_duration = current_time - last_used
                    if idle_duration < _LOCK_CLEANUP_IDLE_THRESHOLD_SECONDS:
                        continue

                    if key in keys_to_remove:
                        continue

                    keys_to_remove.add(key)

                    if len(keys_to_remove) >= _LOCK_CLEANUP_BATCH_SIZE:
                        removed_count += await self._process_lock_cleanup_batch(keys_to_remove)
                        keys_to_remove = set()

                except Exception as exc:  # pragma: no cover - defensive
                    self._metrics["lock_cleanup_failures"] = (
                        self._metrics.get("lock_cleanup_failures", 0) + 1
                    )
                    self._logger.warning(
                        "[LOCK_CLEANUP] Error evaluating key=%s: %s",
                        key,
                        exc,
                        extra={"event_type": "lock_cleanup_error", "lock_key": key},
                    )

            if keys_to_remove:
                removed_count += await self._process_lock_cleanup_batch(keys_to_remove)

        if removed_count > 0:
            self._metrics["lock_cleanup_removed_count"] = (
                self._metrics.get("lock_cleanup_removed_count", 0) + removed_count
            )
            self._logger.info(
                "Cleaned up %d idle locks",
                removed_count,
                extra={
                    "event_type": "lock_cleanup",
                    "removed_count": removed_count,
                },
            )

        if removed_count > 0 and hasattr(self, "_cached_metrics"):
            self._cached_metrics = None
            self._cached_metrics_ts = 0.0

        return removed_count

    async def _process_lock_cleanup_batch(self, keys: Set[str]) -> int:
        """Process a batch of lock removals while holding the locks guard."""

        removed_count = 0
        for batch_key in list(keys):
            removed_lock = self._locks.pop(batch_key, None)
            if removed_lock is None:
                continue

            removed_count += 1

            if len(self._lock_pool) >= _LOCK_POOL_MAX_SIZE:
                continue

            async with self._lock_pool_lock:
                if len(self._lock_pool) >= _LOCK_POOL_MAX_SIZE:
                    continue

                for attr in (
                    "_acquired_at_ts",
                    "_acquired_by_callsite",
                    "_acquired_by_function",
                    "_acquired_by_task",
                ):
                    if hasattr(removed_lock, attr):
                        try:
                            delattr(removed_lock, attr)
                        except AttributeError:
                            pass

                self._lock_pool.append(removed_lock)

        return removed_count

    @asynccontextmanager
    async def acquire_batch(
        self,
        keys: Sequence[str],
        timeout: Optional[float] = None,
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, bool]]:
        """Acquire multiple locks atomically in level order."""

        if not keys:
            yield {}
            return

        # Deduplicate while preserving order
        unique_keys = list(dict.fromkeys(keys))

        # Sort by level to prevent deadlocks (CRITICAL)
        key_levels = [(k, self._resolve_level(k, override=None)) for k in unique_keys]
        sorted_keys = [k for k, _ in sorted(key_levels, key=lambda x: x[1])]

        # Calculate per-lock timeout (equal distribution)
        per_lock_timeout: Optional[float]
        if timeout is not None and len(sorted_keys) > 0:
            per_lock_timeout = timeout / len(sorted_keys)
        else:
            per_lock_timeout = timeout

        # Track acquisition
        acquired_keys: List[str] = []
        results: Dict[str, bool] = {}
        batch_start = time.time()

        try:
            # Acquire in level order
            for key in sorted_keys:
                success = await self.acquire(
                    key,
                    timeout=per_lock_timeout,
                    context=context,
                    timeout_log_level=logging.DEBUG,
                )
                results[key] = success

                if success:
                    acquired_keys.append(key)
                else:
                    # FAST FAIL: Stop on first failure
                    elapsed = time.time() - batch_start
                    self._logger.warning(
                        "[LOCK_BATCH] Failed key=%s after %.3fs (acquired %d/%d)",
                        key,
                        elapsed,
                        len(acquired_keys),
                        len(sorted_keys),
                        extra={
                            "event_type": "lock_batch_partial_failure",
                            "batch_keys": sorted_keys,
                            "acquired_keys": acquired_keys,
                            "failed_key": key,
                            "duration": elapsed,
                        },
                    )
                    break

            # Log success
            if len(acquired_keys) == len(sorted_keys):
                elapsed = time.time() - batch_start
                self._logger.debug(
                    "[LOCK_BATCH] Acquired %d locks in %.3fs: %s",
                    len(sorted_keys),
                    elapsed,
                    sorted_keys,
                    extra={
                        "event_type": "lock_batch_success",
                        "batch_keys": sorted_keys,
                        "duration": elapsed,
                    },
                )

            # Yield results to caller
            yield results

        finally:
            # Release in REVERSE order (prevent deadlocks)
            release_start = time.time()
            release_errors = 0

            for key in reversed(acquired_keys):
                try:
                    self.release(key, context=context)
                except Exception as e:  # pragma: no cover
                    release_errors += 1
                    self._logger.error(
                        "[LOCK_BATCH] Release failed for key=%s: %s",
                        key,
                        e,
                        extra={
                            "event_type": "lock_batch_release_error",
                            "lock_key": key,
                        },
                    )

            release_duration = time.time() - release_start
            if release_errors > 0 or release_duration > 0.1:
                self._logger.warning(
                    "[LOCK_BATCH] Released %d locks in %.3fs (%d errors)",
                    len(acquired_keys),
                    release_duration,
                    release_errors,
                    extra={
                        "event_type": "lock_batch_release_complete",
                        "released_count": len(acquired_keys),
                        "errors": release_errors,
                        "duration": release_duration,
                    },
                )

    async def _wait_for_waiting_tasks_clear(self) -> None:
        """Wait for all waiting tasks to complete or be cancelled."""

        while self._waiting_tasks:
            await asyncio.sleep(0.1)

    def _monotonic_time(self) -> float:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return time.monotonic()
        return loop.time()

    def _reset_circuit_state(self, key: str) -> None:
        if key in self._timeout_count:
            self._timeout_count[key] = 0
        self._circuit_reset_time.pop(key, None)
        self._bypassed_locks.discard(key)

    def _record_timeout(self, key: str) -> None:
        count = self._timeout_count.get(key, 0) + 1
        self._timeout_count[key] = count
        if count >= self._circuit_breaker_threshold:
            if key not in self._circuit_reset_time:
                self._circuit_reset_time[key] = self._monotonic_time()
                self._logger.error(
                    "[CIRCUIT_BREAKER] Lock %s exceeded timeout threshold; circuit opened",
                    key,
                    extra={
                        "event_type": "lock_circuit_open",
                        "lock_key": key,
                        "timeout_count": count,
                    },
                )

    def _is_circuit_broken(self, key: str) -> bool:
        count = self._timeout_count.get(key, 0)
        if count < self._circuit_breaker_threshold:
            return False

        reset_time = self._circuit_reset_time.get(key)
        if reset_time is not None:
            elapsed = self._monotonic_time() - reset_time
            if elapsed >= self._circuit_reset_interval:
                self._logger.info(
                    "[CIRCUIT_BREAKER] Resetting circuit for lock %s after %.1fs",
                    key,
                    elapsed,
                    extra={
                        "event_type": "lock_circuit_reset",
                        "lock_key": key,
                        "elapsed": elapsed,
                    },
                )
                self._reset_circuit_state(key)
                return False

        if key not in self._bypassed_locks:
            self._logger.warning(
                "[CIRCUIT_BREAKER] Circuit open for lock %s (timeouts=%d)",
                key,
                count,
                extra={
                    "event_type": "lock_circuit_open_check",
                    "lock_key": key,
                    "timeout_count": count,
                },
            )
        return True

    def _normalise_category_timeouts(
        self, source: Optional[Mapping[str, Any]]
    ) -> Dict[str, float]:
        if not source:
            return {}
        resolved: Dict[str, float] = {}
        for raw_key, raw_value in source.items():
            if raw_key is None:
                continue
            key = str(raw_key)
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError):
                continue
            if numeric <= 0:
                continue
            resolved[key] = numeric
        return resolved

    def _get_current_acquisitions(self) -> List[_LockAcquisition]:
        return list(self._lock_state_var.get())

    def _set_current_acquisitions(self, acquisitions: List[_LockAcquisition]) -> None:
        self._lock_state_var.set(tuple(acquisitions))
        self._set_current_levels([item.level for item in acquisitions])
        task = asyncio.current_task()
        if task is None:
            return
        if acquisitions:
            self._task_lock_state[task] = acquisitions
        else:
            self._task_lock_state.pop(task, None)

    def _get_current_levels(self) -> List[int]:
        return list(self._level_state_var.get())

    def _set_current_levels(self, levels: Sequence[int]) -> None:
        self._level_state_var.set(tuple(levels))

    def _extract_context_from_key(self, key: str) -> Dict[str, Any]:
        category, _, remainder = key.partition(":")
        payload: Dict[str, Any] = {
            "lock_category": category or key,
            "lock_key": key,
            "lock_name": key,
        }
        if category in {"stage", "engine_stage"} and remainder:
            chat_candidate, *_ = remainder.split(":", 1)
            try:
                payload.setdefault("chat_id", int(chat_candidate))
            except ValueError:
                payload.setdefault("chat_id", chat_candidate)
        return payload

    def _build_context_payload(
        self,
        key: str,
        level: int,
        *,
        additional: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self._extract_context_from_key(key)
        if additional:
            payload.update(dict(additional))
        payload.setdefault("chat_id", payload.get("chat_id"))
        payload.setdefault("game_id", payload.get("game_id"))
        payload.setdefault("lock_key", key)
        payload.setdefault("lock_name", key)
        payload.setdefault("lock_level", level)
        return payload

    async def _record_long_hold_context(
        self,
        *,
        lock_key: str,
        game: Optional[Any],
        elapsed: float,
        stacktrace: str,
    ) -> None:
        """Record information about a long-held lock for offline analysis."""

        try:
            context_payload = self._build_context_payload(
                lock_key,
                self._resolve_level(lock_key, override=None),
                additional={
                    "game_id": getattr(game, "id", None) if game else None,
                    "elapsed_seconds": elapsed,
                    "stacktrace": stacktrace,
                },
            )
            self._logger.warning(
                "[LOCK_DIAG] LONG HOLD context recorded: key=%s elapsed=%.2fs",
                lock_key,
                elapsed,
                extra=context_payload,
            )
            if hasattr(self, "_redis"):
                import json

                await self._redis.set(
                    f"diag:lock:{lock_key}",
                    json.dumps(context_payload, ensure_ascii=False),
                )
        except Exception:
            self._logger.exception("[LOCK_DIAG] Failed recording long-hold context")

    def _format_lock_identity(
        self, key: str, level: int, context: Mapping[str, Any]
    ) -> str:
        return (
            "Lock '%s' (level=%s, chat_id=%s, game_id=%s)"
            % (
                key,
                level,
                context.get("chat_id"),
                context.get("game_id"),
            )
        )

    async def acquire(
        self,
        key: str,
        timeout: Optional[float] = None,
        *,
        context: Optional[Mapping[str, Any]] = None,
        level: Optional[int] = None,
        timeout_log_level: Optional[int] = logging.WARNING,
        failure_log_level: Optional[int] = logging.ERROR,
    ) -> bool:
        """Attempt to acquire the lock identified by ``key``."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("LockManager.acquire requires an active asyncio task")

        # Reject new acquisitions during shutdown
        if self._shutdown_initiated:
            self._logger.warning(
                "[LOCK_SHUTDOWN] Rejecting acquisition attempt for key=%s (shutdown in progress)",
                key,
                extra={
                    "event_type": "lock_acquire_rejected_shutdown",
                    "lock_key": key,
                },
            )
            return False

        call_site, call_function = self._resolve_call_site()
        acquire_start_ts = time.time()
        resolved_level = self._resolve_level(key, override=level)
        context_payload = self._build_context_payload(
            key, resolved_level, additional=context
        )

        if self._is_circuit_broken(key):
            bypass_extra = self._log_extra(
                context_payload,
                event_type="lock_circuit_bypass",
                lock_key=key,
                lock_level=resolved_level,
                timeout_count=self._timeout_count.get(key, 0),
            )
            self._logger.error(
                "[CIRCUIT_BREAKER] Bypassing acquisition for %s%s",
                key,
                self._format_context(context_payload),
                extra=bypass_extra,
            )
            self._bypassed_locks.add(key)
            return True

        if _ENABLE_FAST_PATH:
            lock = await self._get_lock(key)

            fast_path_context = {
                "lock_key": key,
                "lock_level": resolved_level,
                "chat_id": context.get("chat_id") if context else None,
            }

            try:
                await asyncio.wait_for(
                    lock.acquire(), timeout=_FAST_PATH_TIMEOUT_THRESHOLD
                )

                current_acquisitions = self._get_current_acquisitions()
                full_context = self._build_context_payload(
                    key, resolved_level, additional=context
                )

                try:
                    self._validate_lock_order(
                        current_acquisitions, key, resolved_level, full_context
                    )
                except LockOrderError as order_err:
                    lock.release()
                    self._logger.error(
                        "[FAST_PATH] Lock order violation on key=%s, released lock",
                        key,
                        exc_info=True,
                        extra=self._log_extra(
                            full_context,
                            event_type="lock_fast_path_order_violation",
                            lock_key=key,
                        ),
                    )
                    raise order_err

                self._metrics["lock_fast_path_hits"] = (
                    self._metrics.get("lock_fast_path_hits", 0) + 1
                )

                setattr(lock, "_acquired_at_ts", time.time())
                setattr(lock, "_acquired_by_callsite", call_site)
                setattr(lock, "_acquired_by_function", call_function)
                setattr(lock, "_acquired_by_task", self._describe_task(task))

                self._record_acquired(key, resolved_level, full_context)

                if task is not None:
                    acquire_key = (id(task), key)
                    acquire_times = self._lock_acquire_times.setdefault(
                        acquire_key, []
                    )
                    acquire_times.append(time.time())

                elapsed_us = (time.time() - acquire_start_ts) * 1_000_000
                elapsed_seconds = elapsed_us / 1_000_000
                lock_identity = self._format_lock_identity(
                    key, resolved_level, full_context
                )
                info_extra = self._log_extra(
                    full_context,
                    event_type="lock_acquired",
                    lock_key=key,
                    lock_level=resolved_level,
                    attempts=1,
                    attempt_duration=elapsed_seconds,
                    call_site=call_site,
                    call_site_function=call_function,
                )
                self._logger.info(
                    "%s acquired quickly in %.3fs%s",
                    lock_identity,
                    elapsed_seconds,
                    self._format_context(full_context),
                    extra=info_extra,
                )

                self._logger.debug(
                    "[FAST_PATH] Acquired key=%s in %.1fμs",
                    key,
                    elapsed_us,
                    extra=self._log_extra(
                        full_context,
                        event_type="lock_fast_path_hit",
                        lock_key=key,
                        latency_us=elapsed_us,
                    ),
                )

                self._reset_circuit_state(key)
                return True

            except asyncio.TimeoutError:
                self._metrics["lock_fast_path_misses"] = (
                    self._metrics.get("lock_fast_path_misses", 0) + 1
                )
                self._logger.debug(
                    "[FAST_PATH] Timeout on key=%s, using slow path",
                    key,
                    extra=self._log_extra(
                        fast_path_context,
                        event_type="lock_fast_path_miss",
                        lock_key=key,
                    ),
                )
            except Exception:  # pragma: no cover - defensive fallback
                self._logger.debug(
                    "[FAST_PATH] Error acquiring key=%s; falling back",
                    key,
                    exc_info=True,
                    extra=self._log_extra(
                        fast_path_context,
                        event_type="lock_fast_path_error",
                        lock_key=key,
                    ),
                )

            self._metrics["lock_slow_path"] = (
                self._metrics.get("lock_slow_path", 0) + 1
            )
            acquire_start_ts = time.time()

        # 🐌 SLOW PATH: Full validation continues below (existing code)

        lock = await self._get_lock(key)
        trace_start_extra = self._log_extra(
            context_payload,
            event_type="lock_trace_acquire_start",
            lock_key=key,
            lock_level=resolved_level,
            call_site=call_site,
            call_site_function=call_function,
            task=self._describe_task(task),
            acquire_start_ts=acquire_start_ts,
        )
        self._logger.debug(
            "[LOCK_TRACE] START acquire key=%s from=%s (%s) task=%s",
            key,
            call_site,
            call_function,
            self._describe_task(task),
            extra=trace_start_extra,
        )
        current_acquisitions = self._get_current_acquisitions()
        # Check for re-entrant acquisition of the same lock key
        for existing in current_acquisitions:
            if existing.key == key:
                existing.count += 1
                self._logger.debug(
                    "[LOCK_TRACE] RE-ENTRANT acquire key=%s count=%d from=%s (%s) task=%s",
                    key,
                    existing.count,
                    call_site,
                    call_function,
                    self._describe_task(task),
                    extra=self._log_extra(
                        context_payload,
                        event_type="lock_reentrant_acquire",
                        lock_key=key,
                        lock_level=resolved_level,
                        reentrant_count=existing.count,
                        call_site=call_site,
                        call_site_function=call_function,
                    ),
                )
                self._reset_circuit_state(key)
                return True

        self._validate_lock_order(current_acquisitions, key, resolved_level, context_payload)
        acquisition_order = self._get_current_levels()
        acquiring_extra = self._log_extra(
            context_payload,
            event_type="lock_acquiring",
            lock_key=key,
            lock_level=resolved_level,
            acquisition_order=acquisition_order,
            call_site=call_site,
            call_site_function=call_function,
        )
        acquiring_extra.setdefault("lock_name", context_payload.get("lock_name", key))
        acquiring_extra.setdefault("chat_id", context_payload.get("chat_id"))
        acquiring_extra.setdefault("lock_level", resolved_level)
        acquiring_extra["order"] = acquisition_order
        self._logger.info("Acquiring lock", extra=acquiring_extra)

        total_timeout = self._resolve_timeout(key, timeout)
        deadline: Optional[float]
        loop = asyncio.get_running_loop()
        if total_timeout is None:
            deadline = None
        else:
            deadline = loop.time() + max(0.0, total_timeout)

        attempts = self._max_retries + 1
        attempt_timings: List[Dict[str, Any]] = []  # Track timing for diagnostics

        for attempt in range(attempts):
            attempt_start = loop.time()
            attempt_timeout: Optional[float]
            if deadline is None:
                attempt_timeout = None
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    # Timeout budget exhausted before this attempt
                    self._logger.debug(
                        "[LOCK_TIMEOUT] Timeout budget exhausted before attempt %d for key=%s",
                        attempt + 1,
                        key,
                        extra=self._log_extra(
                            context_payload,
                            event_type="lock_timeout_budget_exhausted",
                            lock_key=key,
                            lock_level=resolved_level,
                            attempt=attempt,
                            attempt_timings=attempt_timings,
                        ),
                    )
                    break

                # Use adaptive timeout calculation instead of linear distribution
                attempt_timeout = self._calculate_attempt_timeout(
                    attempt, attempts, remaining
                )

                if attempt_timeout <= 0:
                    self._logger.debug(
                        "[LOCK_TIMEOUT] Calculated timeout <= 0 for attempt %d, key=%s",
                        attempt + 1,
                        key,
                        extra=self._log_extra(
                            context_payload,
                            event_type="lock_timeout_invalid",
                            lock_key=key,
                            lock_level=resolved_level,
                            attempt=attempt,
                            calculated_timeout=attempt_timeout,
                        ),
                    )
                    break

            self._register_waiting(task, key, resolved_level, context_payload)
            lock_identity = self._format_lock_identity(
                key, resolved_level, context_payload
            )
            lock_acquired = False
            try:
                owner = getattr(lock, "_owner", None)
                if owner is not None and owner is not task:
                    self._metrics["lock_contention"] += 1

                if attempt_timeout is None:
                    await lock.acquire()
                else:
                    # Monitor acquisition progress for early warning
                    acquisition_start = loop.time()
                    try:
                        await asyncio.wait_for(lock.acquire(), timeout=attempt_timeout)
                    except asyncio.TimeoutError:
                        # Record failed attempt timing for diagnostics
                        attempt_duration = loop.time() - acquisition_start
                        attempt_timings.append({
                            "attempt": attempt,
                            "duration": attempt_duration,
                            "timeout": attempt_timeout,
                            "result": "timeout",
                        })
                        raise
                    else:
                        # Record successful attempt timing
                        attempt_duration = loop.time() - acquisition_start
                        attempt_timings.append({
                            "attempt": attempt,
                            "duration": attempt_duration,
                            "timeout": attempt_timeout,
                            "result": "success",
                        })

                    # Warn if acquisition took longer than expected (>70% of timeout)
                    if attempt_duration > attempt_timeout * _TIMEOUT_WARNING_RATIO:
                        self._logger.warning(
                            "[TIMEOUT_WARNING] Lock '%s' acquisition took %.2fs (%.0f%% of %.2fs timeout) on attempt %d",
                            key,
                            attempt_duration,
                            (attempt_duration / attempt_timeout) * 100,
                            attempt_timeout,
                            attempt + 1,
                            extra=self._log_extra(
                                context_payload,
                                event_type="lock_timeout_warning",
                                lock_key=key,
                                lock_level=resolved_level,
                                attempt=attempt + 1,
                                attempt_duration=attempt_duration,
                                attempt_timeout=attempt_timeout,
                                timeout_ratio=attempt_duration / attempt_timeout,
                            ),
                        )
                lock_acquired = True
                setattr(lock, "_acquired_at_ts", time.time())
                setattr(lock, "_acquired_by_callsite", call_site)
                setattr(lock, "_acquired_by_function", call_function)
                setattr(lock, "_acquired_by_task", self._describe_task(task))
                elapsed = loop.time() - attempt_start
                self._record_acquired(key, resolved_level, context_payload)
                if task is not None:
                    acquire_key = (id(task), key)
                    acquire_times = self._lock_acquire_times.setdefault(acquire_key, [])
                    acquire_times.append(loop.time())
                trace_acquired_extra = self._log_extra(
                    context_payload,
                    event_type="lock_trace_acquired",
                    lock_key=key,
                    lock_level=resolved_level,
                    call_site=call_site,
                    call_site_function=call_function,
                    task=self._describe_task(task),
                    wait_duration=elapsed,
                    total_duration=time.time() - acquire_start_ts,
                )
                self._logger.debug(
                    "[LOCK_TRACE] ACQUIRED key=%s by=%s in %.3fs (waited=%.3fs) from=%s (%s)",
                    key,
                    self._describe_task(task),
                    time.time() - acquire_start_ts,
                    elapsed,
                    call_site,
                    call_function,
                    extra=trace_acquired_extra,
                )
                if attempt == 0 and elapsed < 0.1:
                    self._logger.info(
                        "%s acquired quickly in %.3fs%s",
                        lock_identity,
                        elapsed,
                        self._format_context(context_payload),
                        extra=self._log_extra(
                            context_payload,
                            event_type="lock_acquired",
                            lock_key=key,
                            lock_level=resolved_level,
                            attempts=attempt + 1,
                            attempt_duration=elapsed,
                            call_site=call_site,
                            call_site_function=call_function,
                        ),
                    )
                else:
                    self._logger.info(
                        "%s acquired after %d attempt(s) in %.3fs%s",
                        lock_identity,
                        attempt + 1,
                        elapsed,
                        self._format_context(context_payload),
                        extra=self._log_extra(
                            context_payload,
                            event_type="lock_acquired",
                            lock_key=key,
                            lock_level=resolved_level,
                            attempts=attempt + 1,
                            attempt_duration=elapsed,
                            call_site=call_site,
                            call_site_function=call_function,
                        ),
                    )
                self._reset_circuit_state(key)
                return True
            except asyncio.TimeoutError:
                self._metrics["lock_timeouts"] += 1
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - loop.time())
                diagnostics = self._lock_diagnostics(key)
                diagnostic_context = dict(context_payload)
                diagnostic_context.update(diagnostics)
                stage_label_parts = ["acquire_timeout", key, f"attempt={attempt + 1}"]
                chat_id = diagnostic_context.get("chat_id")
                if chat_id is not None:
                    stage_label_parts.append(f"chat={chat_id}")
                game_id = diagnostic_context.get("game_id")
                if game_id is not None:
                    stage_label_parts.append(f"game={game_id}")
                stage_label = ":".join(str(part) for part in stage_label_parts if part)
                if timeout_log_level is not None:
                    effective_timeout_level = max(timeout_log_level, logging.WARNING)
                    self._log_lock_snapshot_on_timeout(
                        stage_label,
                        level=effective_timeout_level,
                        minimum_level=effective_timeout_level,
                        extra={
                            "lock_key": key,
                            "lock_level": resolved_level,
                            "chat_id": diagnostic_context.get("chat_id"),
                            "game_id": diagnostic_context.get("game_id"),
                            "attempt": attempt + 1,
                        },
                    )
                    owner_suffix = ""
                    holders = diagnostics.get("held_by_tasks")
                    if holders:
                        owner_suffix = f"; held_by={', '.join(holders)}"
                    self._logger.log(
                        effective_timeout_level,
                        "Timeout acquiring %s on attempt %d (remaining %.3fs)%s%s",
                        lock_identity,
                        attempt + 1,
                        remaining if remaining is not None else float("inf"),
                        owner_suffix,
                        self._format_context(diagnostic_context),
                        extra=self._log_extra(
                            diagnostic_context,
                            event_type="lock_timeout",
                            lock_key=key,
                            lock_level=resolved_level,
                            attempts=attempt + 1,
                            remaining_time=remaining,
                            held_by_tasks=diagnostics.get("held_by_tasks"),
                            waiting_tasks=diagnostics.get("waiting_tasks"),
                        ),
                    )

                # Apply exponential backoff before retry (if not last attempt)
                if attempt < attempts - 1:  # Not the last attempt
                    backoff_delay = self._calculate_backoff_delay(attempt + 1)

                    # Check if we have time budget for backoff
                    if deadline is not None:
                        remaining_after_backoff = deadline - loop.time() - backoff_delay
                        if remaining_after_backoff < 0:
                            # Skip backoff if it would exceed deadline
                            self._logger.debug(
                                "[LOCK_RETRY] Skipping backoff delay (%.2fs) - insufficient time remaining",
                                backoff_delay,
                                extra=self._log_extra(
                                    context_payload,
                                    event_type="lock_backoff_skipped",
                                    lock_key=key,
                                    lock_level=resolved_level,
                                    attempt=attempt + 1,
                                    backoff_delay=backoff_delay,
                                ),
                            )
                        else:
                            # Apply backoff delay
                            self._logger.debug(
                                "[LOCK_RETRY] Backing off %.2fs before retry %d for key=%s",
                                backoff_delay,
                                attempt + 2,
                                key,
                                extra=self._log_extra(
                                    context_payload,
                                    event_type="lock_backoff",
                                    lock_key=key,
                                    lock_level=resolved_level,
                                    attempt=attempt + 1,
                                    backoff_delay=backoff_delay,
                                ),
                            )
                            await asyncio.sleep(backoff_delay)
                    else:
                        # No deadline, always apply backoff
                        self._logger.debug(
                            "[LOCK_RETRY] Backing off %.2fs before retry %d for key=%s",
                            backoff_delay,
                            attempt + 2,
                            key,
                            extra=self._log_extra(
                                context_payload,
                                event_type="lock_backoff",
                                lock_key=key,
                                lock_level=resolved_level,
                                attempt=attempt + 1,
                                backoff_delay=backoff_delay,
                            ),
                        )
                        await asyncio.sleep(backoff_delay)
            except asyncio.CancelledError:
                self._metrics["lock_cancellations"] += 1

                # Capture stack trace if enabled
                stacktrace = None
                if _CANCELLATION_LOG_STACKTRACE:
                    import traceback

                    stacktrace = "".join(traceback.format_stack())

                # Log cancellation with full context
                cancellation_extra = self._log_extra(
                    context_payload,
                    event_type="lock_cancelled",
                    lock_key=key,
                    lock_level=resolved_level,
                    attempts=attempt + 1,
                    lock_acquired=lock_acquired,
                    shutdown_initiated=self._shutdown_initiated,
                )

                if stacktrace:
                    cancellation_extra["stacktrace"] = stacktrace

                self._logger.warning(
                    "[LOCK_CANCELLED] Lock acquisition for %s cancelled on attempt %d (acquired=%s, shutdown=%s)%s",
                    lock_identity,
                    attempt + 1,
                    lock_acquired,
                    self._shutdown_initiated,
                    self._format_context(context_payload),
                    extra=cancellation_extra,
                )

                # Perform cleanup with timeout protection
                try:
                    await asyncio.wait_for(
                        self._cleanup_after_cancellation(
                            lock, key, task, lock_acquired, context_payload
                        ),
                        timeout=_CANCELLATION_CLEANUP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self._metrics["lock_cleanup_failures"] += 1
                    self._logger.error(
                        "[LOCK_CLEANUP] Cleanup timeout (%.1fs) for cancelled acquisition of key=%s",
                        _CANCELLATION_CLEANUP_TIMEOUT,
                        key,
                        extra=self._log_extra(
                            context_payload,
                            event_type="lock_cleanup_timeout",
                            lock_key=key,
                            lock_level=resolved_level,
                            cleanup_timeout=_CANCELLATION_CLEANUP_TIMEOUT,
                        ),
                    )
                except Exception as cleanup_error:  # pragma: no cover - defensive
                    self._metrics["lock_cleanup_failures"] += 1
                    self._logger.exception(
                        "[LOCK_CLEANUP] Unexpected error during cleanup for key=%s: %s",
                        key,
                        cleanup_error,
                        extra=self._log_extra(
                            context_payload,
                            event_type="lock_cleanup_unexpected_error",
                            lock_key=key,
                            lock_level=resolved_level,
                            error=str(cleanup_error),
                        ),
                    )

                # Re-raise cancellation after cleanup
                raise
            except Exception:
                self._logger.exception(
                    "Error acquiring %s%s",
                    lock_identity,
                    self._format_context(context_payload),
                    extra=self._log_extra(
                        context_payload,
                        event_type="lock_acquire_error",
                        lock_key=key,
                        lock_level=resolved_level,
                        attempts=attempt + 1,
                    ),
                )
                if (
                    lock_acquired
                    and getattr(lock, "_owner", None) is task
                    and lock.locked()
                ):
                    try:
                        lock.release()
                        self._logger.debug(
                            "Released lock %s after acquisition error", lock_identity
                        )
                    except Exception:
                        self._logger.exception(
                            "Failed to release %s after acquisition error%s",
                            lock_identity,
                            self._format_context(context_payload),
                            extra=self._log_extra(
                                context_payload,
                                event_type="lock_release_error_after_acquire",
                                lock_key=key,
                                lock_level=resolved_level,
                                attempts=attempt + 1,
                            ),
                        )
                raise
            finally:
                self._unregister_waiting(task, key)

        diagnostics = self._lock_diagnostics(key)
        failure_context = dict(context_payload)
        failure_context.update(diagnostics)
        lock_identity = self._format_lock_identity(
            key, resolved_level, failure_context
        )
        stage_parts = ["acquire_failure", key]
        chat_id = failure_context.get("chat_id")
        if chat_id is not None:
            stage_parts.append(f"chat={chat_id}")
        game_id = failure_context.get("game_id")
        if game_id is not None:
            stage_parts.append(f"game={game_id}")
        stage_label = ":".join(str(part) for part in stage_parts if part)
        if failure_log_level is not None:
            effective_failure_level = max(failure_log_level, logging.WARNING)
            self._log_lock_snapshot_on_timeout(
                stage_label,
                level=effective_failure_level,
                minimum_level=effective_failure_level,
                extra={
                    "lock_key": key,
                    "lock_level": resolved_level,
                    "chat_id": failure_context.get("chat_id"),
                    "game_id": failure_context.get("game_id"),
                },
            )
            owner_suffix = ""
            holders = diagnostics.get("held_by_tasks")
            if holders:
                owner_suffix = f"; held_by={', '.join(holders)}"
            self._logger.log(
                effective_failure_level,
                "Failed to acquire %s after %d attempts%s%s",
                lock_identity,
                attempts,
                owner_suffix,
                self._format_context(failure_context),
                extra=self._log_extra(
                    failure_context,
                    event_type="lock_failure",
                    lock_key=key,
                    lock_level=resolved_level,
                    attempts=attempts,
                    attempt_timings=attempt_timings,
                    held_by_tasks=diagnostics.get("held_by_tasks"),
                    waiting_tasks=diagnostics.get("waiting_tasks"),
                ),
            )
        self._record_timeout(key)
        return False

    @asynccontextmanager
    async def guard(
        self,
        key: str,
        *,
        timeout: Optional[float] = None,
        context: Optional[Mapping[str, Any]] = None,
        context_extra: Optional[Mapping[str, Any]] = None,
        level: Optional[int] = None,
        timeout_log_level: Optional[int] = logging.WARNING,
        failure_log_level: Optional[int] = logging.ERROR,
    ) -> AsyncIterator[None]:
        combined_context: Dict[str, Any] = dict(context or {})
        if context_extra:
            combined_context.update(context_extra)
        acquired = await self.acquire(
            key,
            timeout=timeout,
            context=combined_context,
            level=level,
            timeout_log_level=timeout_log_level,
            failure_log_level=failure_log_level,
        )
        if not acquired:
            resolved_level = self._resolve_level(key, override=level)
            failure_context = self._build_context_payload(
                key,
                resolved_level,
                additional=combined_context,
            )
            failure_identity = self._format_lock_identity(
                key, resolved_level, failure_context
            )
            message = f"Timeout acquiring {failure_identity}"
            guard_failure_stage_parts = ["guard_failure", key]
            chat_id = failure_context.get("chat_id")
            if chat_id is not None:
                guard_failure_stage_parts.append(f"chat={chat_id}")
            game_id = failure_context.get("game_id")
            if game_id is not None:
                guard_failure_stage_parts.append(f"game={game_id}")
            guard_failure_stage = ":".join(
                str(part) for part in guard_failure_stage_parts if part
            )
            failure_snapshot_extra = dict(failure_context)
            failure_snapshot_extra.setdefault("lock_key", key)
            failure_snapshot_extra.setdefault("lock_level", resolved_level)
            if failure_log_level is not None:
                effective_failure_level = max(failure_log_level, logging.WARNING)
                self._log_lock_snapshot_on_timeout(
                    guard_failure_stage,
                    level=effective_failure_level,
                    minimum_level=effective_failure_level,
                    extra=failure_snapshot_extra,
                )
            guard_stage_parts = ["guard_timeout", key]
            if chat_id is not None:
                guard_stage_parts.append(f"chat={chat_id}")
            if game_id is not None:
                guard_stage_parts.append(f"game={game_id}")
            guard_stage = ":".join(str(part) for part in guard_stage_parts if part)
            self._log_lock_snapshot_on_timeout(
                guard_stage,
                level=logging.WARNING,
                extra={
                    "chat_id": failure_context.get("chat_id"),
                    "game_id": failure_context.get("game_id"),
                    "lock_key": key,
                    "lock_level": failure_context.get("lock_level"),
                },
            )
            self._logger.warning(
                "%s%s",
                message,
                self._format_context(dict(failure_context)),
                extra=self._log_extra(
                    dict(failure_context),
                    event_type="lock_guard_timeout",
                    lock_key=key,
                    lock_level=failure_context.get("lock_level"),
                ),
            )
            raise TimeoutError(message)
        resolved_level = self._resolve_level(key, override=level)
        guard_context = self._build_context_payload(
            key, resolved_level, additional=combined_context
        )
        acquisition_order = self._get_current_levels()
        guard_log_context = dict(guard_context)
        guard_log_context["acquisition_order"] = acquisition_order
        self._logger.debug(
            "Guard acquired lock '%s' (level=%s) with order %s for chat %s%s",
            key,
            resolved_level,
            acquisition_order,
            guard_context.get("chat_id"),
            self._format_context(guard_log_context),
            extra=self._log_extra(
                guard_log_context,
                event_type="lock_guard_acquired",
                lock_key=key,
                lock_level=resolved_level,
                acquisition_order=acquisition_order,
            ),
        )
        try:
            yield
        finally:
            # Release must complete even if cancelled during critical section
            try:
                self.release(key, context=combined_context)
            except Exception as release_error:  # pragma: no cover - defensive
                self._logger.error(
                    "[LOCK_GUARD] Failed to release lock in finally block for key=%s: %s",
                    key,
                    release_error,
                    extra=self._log_extra(
                        combined_context,
                        event_type="lock_guard_release_failed",
                        lock_key=key,
                        error=str(release_error),
                    ),
                )
                # Don't suppress the original exception if release fails
                raise

    def trace_guard(
        self,
        key: str,
        *,
        timeout: Optional[float] = None,
        context: Optional[Mapping[str, Any]] = None,
        context_extra: Optional[Mapping[str, Any]] = None,
        level: Optional[int] = None,
        timeout_log_level: Optional[int] = logging.WARNING,
        failure_log_level: Optional[int] = logging.ERROR,
    ) -> AsyncIterator[None]:
        """Alias for :meth:`guard` used by traced lock helpers."""

        return self.guard(
            key,
            timeout=timeout,
            context=context,
            context_extra=context_extra,
            level=level,
            timeout_log_level=timeout_log_level,
            failure_log_level=failure_log_level,
        )

    def release(
        self, key: str, context: Optional[Mapping[str, Any]] = None
    ) -> None:
        release_site, release_function = self._resolve_call_site()

        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        lock = self._locks.get(key)
        if key in self._bypassed_locks:
            self._bypassed_locks.discard(key)
            self._logger.debug(
                "[CIRCUIT_BREAKER] Release ignored for bypassed lock %s",
                key,
                extra={
                    "event_type": "lock_circuit_release",
                    "lock_key": key,
                },
            )
            return
        base_context = dict(context or {})
        if lock is None:
            resolved_level = self._resolve_level(key, override=None)
            unknown_context = self._build_context_payload(
                key, resolved_level, additional=base_context
            )
            trace_unknown_extra = self._log_extra(
                unknown_context,
                event_type="lock_trace_release_unknown",
                lock_key=key,
                lock_level=resolved_level,
                release_site=release_site,
                release_function=release_function,
                task=self._describe_task(task) if task else None,
            )
            self._logger.debug(
                "[LOCK_TRACE] RELEASE unknown key=%s by=%s from=%s (%s)",
                key,
                self._describe_task(task) if task else None,
                release_site,
                release_function,
                extra=trace_unknown_extra,
            )
            self._logger.debug(
                "Release requested for unknown %s%s",
                self._format_lock_identity(key, resolved_level, unknown_context),
                self._format_context(unknown_context),
                extra=self._log_extra(
                    unknown_context,
                    event_type="lock_release_unknown",
                    lock_key=key,
                    lock_level=unknown_context.get("lock_level"),
                    release_function=release_function,
                ),
            )
            return

        record_entry: Optional[Tuple[int, _LockAcquisition]] = None
        if task is not None:
            record_entry = self._find_lock_record(task, key)
            if record_entry is not None and not base_context:
                base_context = dict(record_entry[1].context)

        release_level = (
            record_entry[1].level
            if record_entry is not None
            else self._resolve_level(key, override=None)
        )
        context_payload = self._build_context_payload(
            key, release_level, additional=base_context
        )

        release_context_override: Optional[Dict[str, Any]] = None
        release_index: Optional[int] = None
        current_acquisitions = self._get_current_acquisitions()
        for i, acq in enumerate(current_acquisitions):
            if acq.key != key:
                continue
            if acq.count > 1:
                acq.count -= 1
                self._logger.debug(
                    "[LOCK_TRACE] RE-ENTRANT release key=%s count=%d (still held) from=%s (%s) task=%s",
                    key,
                    acq.count,
                    release_site,
                    release_function,
                    self._describe_task(task),
                    extra=self._log_extra(
                        context_payload,
                        event_type="lock_reentrant_release",
                        lock_key=key,
                        lock_level=release_level,
                        reentrant_count=acq.count,
                        call_site=release_site,
                        call_site_function=release_function,
                    ),
                )
                self._set_current_acquisitions(current_acquisitions)
                return

            release_context_override = self._build_context_payload(
                key, release_level, additional=acq.context
            )
            release_index = i
            break

        if release_context_override is not None:
            context_payload = release_context_override

        acquire_key: Optional[Tuple[int, str]] = None
        holding_duration: Optional[float] = None
        if task is not None:
            acquire_key = (id(task), key)
            acquire_times = self._lock_acquire_times.get(acquire_key)
            if acquire_times:
                current_time = (
                    running_loop.time() if running_loop is not None else time.monotonic()
                )
                holding_duration = max(0.0, current_time - acquire_times[-1])

        held_duration: Optional[float] = None
        acquired_by = getattr(lock, "_acquired_by_callsite", None)
        acquired_function = getattr(lock, "_acquired_by_function", None)
        acquired_task = getattr(lock, "_acquired_by_task", None)
        acquired_at_ts = getattr(lock, "_acquired_at_ts", None)
        if isinstance(acquired_at_ts, (int, float)):
            held_duration = max(0.0, time.time() - acquired_at_ts)

        context_suffix = self._format_context(context_payload)
        trace_release_extra = self._log_extra(
            context_payload,
            event_type="lock_trace_release",
            lock_key=key,
            lock_level=context_payload.get("lock_level"),
            release_site=release_site,
            release_function=release_function,
            acquired_from=acquired_by,
            acquired_function=acquired_function,
            held_duration=held_duration,
            holding_duration=holding_duration,
            task=self._describe_task(task) if task else None,
            acquired_task=acquired_task,
        )
        self._logger.debug(
            "[LOCK_TRACE] RELEASE lock_key=%s held_for=%.3fs from=%s (%s)%s",
            key,
            holding_duration if holding_duration is not None else -1.0,
            release_site,
            release_function,
            context_suffix,
            extra=trace_release_extra,
        )

        if (
            holding_duration is not None
            and holding_duration > self._LONG_HOLD_THRESHOLD_SECONDS
        ):
            long_hold_extra = self._log_extra(
                context_payload,
                event_type="lock_long_hold_release",
                lock_key=key,
                lock_level=context_payload.get("lock_level"),
                holding_duration=holding_duration,
                release_site=release_site,
                release_function=release_function,
                task=self._describe_task(task) if task else None,
            )
            self._logger.warning(
                "[LOCK_TRACE] LONG HOLD on release lock_key=%s held_for=%.3fs from=%s (%s)%s",
                key,
                holding_duration,
                release_site,
                release_function,
                context_suffix,
                extra=long_hold_extra,
            )
            snapshot = self.detect_deadlock()
            snapshot_extra = self._log_extra(
                context_payload,
                event_type="lock_snapshot_long_hold",
                lock_key=key,
                lock_level=context_payload.get("lock_level"),
                holding_duration=holding_duration,
                release_site=release_site,
                release_function=release_function,
                task=self._describe_task(task) if task else None,
            )
            snapshot_json = json.dumps(snapshot, ensure_ascii=False, default=str)
            self._logger.warning(
                "[LOCK_TRACE] SNAPSHOT long hold lock_key=%s snapshot=%s from=%s (%s)%s",
                key,
                snapshot_json,
                release_site,
                release_function,
                context_suffix,
                extra=snapshot_extra,
            )
        try:
            lock.release()
        except RuntimeError:
            self._logger.exception(
                "Failed to release %s due to ownership mismatch%s",
                self._format_lock_identity(key, release_level, context_payload),
                self._format_context(context_payload),
                extra=self._log_extra(
                    context_payload,
                    event_type="lock_release_error",
                    lock_key=key,
                    lock_level=context_payload.get("lock_level"),
                    release_site=release_site,
                    release_function=release_function,
                ),
            )
            raise
        else:
            if release_index is not None:
                current_acquisitions.pop(release_index)
                self._set_current_acquisitions(current_acquisitions)
            if release_context_override is not None:
                context_payload = release_context_override
            if acquire_key is not None:
                acquire_times = self._lock_acquire_times.get(acquire_key)
                if acquire_times:
                    acquire_times.pop()
                    if not acquire_times:
                        self._lock_acquire_times.pop(acquire_key, None)
            self._logger.info(
                "%s released%s",
                self._format_lock_identity(
                    key, context_payload.get("lock_level", release_level), context_payload
                ),
                self._format_context(context_payload),
                extra=self._log_extra(
                    context_payload,
                    event_type="lock_released",
                    lock_key=key,
                    lock_level=context_payload.get("lock_level"),
                    release_site=release_site,
                    release_function=release_function,
                ),
            )

    def detect_deadlock(self) -> Dict[str, Any]:
        """Return a snapshot of held and waiting locks and potential cycles."""

        snapshot: Dict[str, Any] = {"tasks": [], "waiting": [], "cycles": []}

        task_states = list(self._task_lock_state.items())
        waiting_states = list(self._waiting_tasks.items())

        for task, acquisitions in task_states:
            snapshot["tasks"].append(
                {
                    "task": self._describe_task(task),
                    "locks": [
                        {
                            "key": item.key,
                            "level": item.level,
                            "count": item.count,
                            "context": dict(item.context),
                        }
                        for item in acquisitions
                    ],
                }
            )

        dependency_graph: Dict[asyncio.Task[Any], Set[asyncio.Task[Any]]] = {}
        for task, wait in waiting_states:
            lock = self._locks.get(wait.key)
            owner: Optional[asyncio.Task[Any]] = None
            if lock is not None:
                owner = getattr(lock, "_owner", None)

            waiting_entry = {
                "task": self._describe_task(task),
                "key": wait.key,
                "level": wait.level,
                "context": dict(wait.context),
            }
            if owner is not None:
                waiting_entry["held_by"] = self._describe_task(owner)
            snapshot["waiting"].append(waiting_entry)

            if owner is not None and owner is not task:
                dependency_graph.setdefault(task, set()).add(owner)

        snapshot["cycles"] = self._detect_cycles(dependency_graph)
        return snapshot

    def _resolve_lock_category(self, key: str) -> Optional[str]:
        for prefix, category in _LOCK_PREFIX_LEVELS:
            if key.startswith(prefix):
                return category
        return None

    def _resolve_level(self, key: str, *, override: Optional[int]) -> int:
        if override is not None:
            return override
        category = self._resolve_lock_category(key)
        if category is None:
            return self._default_lock_level
        return LOCK_LEVELS.get(category, self._default_lock_level)

    def _resolve_timeout(
        self, key: str, override: Optional[float]
    ) -> Optional[float]:
        if override is not None:
            if isinstance(override, (int, float)) and (
                override < 0 or math.isinf(override)
            ):
                return None
            return override
        category = self._resolve_lock_category(key)
        category_timeout = None
        if category is not None:
            category_timeout = self._category_timeouts.get(category)
        if category_timeout is not None:
            return category_timeout
        return self._default_timeout_seconds

    def _calculate_attempt_timeout(
        self,
        attempt: int,
        total_attempts: int,
        remaining: float,
    ) -> float:
        """Calculate timeout for a specific retry attempt using adaptive strategy.

        Strategy:
        - First attempt: 30% of average time (fail fast on immediate contention)
        - Last attempt: All remaining time (give final chance)
        - Middle attempts: Gradually increasing allocation (50% to 100% progression)

        Args:
            attempt: Current attempt number (0-indexed)
            total_attempts: Total number of attempts allowed
            remaining: Remaining time budget in seconds

        Returns:
            Timeout in seconds for this attempt
        """

        if remaining <= 0:
            return 0.0

        if total_attempts <= 1:
            return remaining

        remaining_attempts = total_attempts - attempt
        if remaining_attempts <= 0:
            return remaining

        # Base allocation: equal distribution of remaining time
        base_timeout = remaining / remaining_attempts

        if attempt == 0:
            # First attempt: fail fast (30% of base allocation)
            return base_timeout * 0.3
        elif attempt == total_attempts - 1:
            # Last attempt: use all remaining time
            return remaining
        else:
            # Middle attempts: gradually increase from 50% to 100% of base
            progress = attempt / (total_attempts - 1)
            multiplier = 0.5 + 0.5 * progress
            return base_timeout * multiplier

    def _calculate_backoff_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay with jitter.

        Uses exponential progression: base * (2 ^ attempt)
        Adds random jitter to prevent thundering herd effects.

        Args:
            attempt: Retry attempt number (0-indexed)

        Returns:
            Delay in seconds before next retry
        """

        if attempt == 0:
            return 0.0  # No delay before first retry

        # Exponential backoff: 0.1s, 0.2s, 0.4s, 0.8s, 1.6s (capped at 2.0s)
        backoff = _TIMEOUT_BACKOFF_BASE * (2 ** (attempt - 1))
        backoff = min(backoff, _TIMEOUT_BACKOFF_MAX)

        # Add jitter (±10%) to prevent synchronized retries
        jitter = backoff * _TIMEOUT_JITTER_RATIO * random.random()

        return backoff + jitter

    async def _cleanup_after_cancellation(
        self,
        lock: ReentrantAsyncLock,
        key: str,
        task: "asyncio.Task[Any]",
        lock_acquired: bool,
        context: Mapping[str, Any],
    ) -> None:
        """Perform cleanup after lock acquisition is cancelled.

        Ensures that:
        1. Waiting state is properly unregistered
        2. Partially acquired locks are released
        3. Cleanup failures are logged for diagnostics

        Args:
            lock: The lock object being acquired
            key: Lock key identifier
            task: Task that was cancelled
            lock_acquired: Whether lock was successfully acquired before cancellation
            context: Context payload for logging
        """

        cleanup_errors: List[str] = []

        # Unregister from waiting tasks
        try:
            self._unregister_waiting(task, key)
        except Exception as e:  # pragma: no cover - defensive
            error_msg = f"Failed to unregister waiting task: {e}"
            cleanup_errors.append(error_msg)
            self._logger.error(
                "[LOCK_CLEANUP] %s for key=%s",
                error_msg,
                key,
                extra=self._log_extra(
                    context,
                    event_type="lock_cleanup_unregister_failed",
                    lock_key=key,
                    error=str(e),
                ),
            )

        # Release lock if it was acquired
        if lock_acquired and getattr(lock, "_owner", None) is task and lock.locked():
            try:
                lock.release()
                self._logger.debug(
                    "[LOCK_CLEANUP] Released lock after cancellation: key=%s",
                    key,
                    extra=self._log_extra(
                        context,
                        event_type="lock_cleanup_release_success",
                        lock_key=key,
                    ),
                )
            except Exception as e:  # pragma: no cover - defensive
                error_msg = f"Failed to release lock: {e}"
                cleanup_errors.append(error_msg)
                self._metrics["lock_cleanup_failures"] += 1
                self._logger.error(
                    "[LOCK_CLEANUP] %s for key=%s",
                    error_msg,
                    key,
                    extra=self._log_extra(
                        context,
                        event_type="lock_cleanup_release_failed",
                        lock_key=key,
                        error=str(e),
                    ),
                )

        # Log summary if any errors occurred
        if cleanup_errors:
            self._logger.warning(
                "[LOCK_CLEANUP] Cleanup completed with %d error(s) for key=%s: %s",
                len(cleanup_errors),
                key,
                "; ".join(cleanup_errors),
                extra=self._log_extra(
                    context,
                    event_type="lock_cleanup_completed_with_errors",
                    lock_key=key,
                    error_count=len(cleanup_errors),
                    errors=cleanup_errors,
                ),
            )

    def _validate_lock_order(
        self,
        current_acquisitions: List[_LockAcquisition],
        new_key: str,
        new_level: int,
        context: Mapping[str, Any],
    ) -> None:
        """Validate that acquiring new_key respects hierarchical lock ordering.

        Raises:
            LockOrderError: If the new lock would violate ordering constraints.
        """

        if not current_acquisitions:
            # No locks held, no ordering constraint
            return

        # Check if any currently held lock has a higher level than the new lock
        held_levels = [acq.level for acq in current_acquisitions]
        max_held_level = max(held_levels)

        if new_level < max_held_level:
            # Attempting to acquire a lower-level lock while holding a higher-level lock
            # This can cause AA deadlock if another task acquires in opposite order

            # Find the acquisition with the highest level
            violating_acquisition = max(
                current_acquisitions, key=lambda acq: acq.level
            )

            # Build detailed error message
            held_keys = [acq.key for acq in current_acquisitions]
            held_levels_str = ", ".join(
                f"{acq.key}(L{acq.level})" for acq in current_acquisitions
            )

            error_message = (
                f"Lock order violation: Attempting to acquire '{new_key}' (level {new_level}) "
                f"while holding higher-level lock '{violating_acquisition.key}' (level {violating_acquisition.level}). "
                f"Currently held locks: [{held_levels_str}]. "
                "Locks must be acquired in ascending level order to prevent deadlock."
            )

            # Log the violation with full context
            task = asyncio.current_task()
            self._logger.error(
                "[LOCK_ORDER_VIOLATION] %s task=%s",
                error_message,
                self._describe_task(task),
                extra=self._log_extra(
                    context,
                    event_type="lock_order_violation",
                    lock_key=new_key,
                    lock_level=new_level,
                    held_keys=held_keys,
                    held_levels=held_levels,
                    max_held_level=max_held_level,
                    violating_key=violating_acquisition.key,
                    violating_level=violating_acquisition.level,
                ),
            )

            raise LockOrderError(error_message)

        # Additional check: Warn if acquiring the same level (potential design smell)
        if new_level == max_held_level:
            held_same_level = [
                acq.key for acq in current_acquisitions if acq.level == new_level
            ]
            if held_same_level and new_key not in held_same_level:
                self._logger.warning(
                    "[LOCK_ORDER_WARNING] Acquiring lock '%s' at same level (%d) as held locks %s. "
                    "Consider using different levels if these locks protect different resources.",
                    new_key,
                    new_level,
                    held_same_level,
                    extra=self._log_extra(
                        context,
                        event_type="lock_order_same_level",
                        lock_key=new_key,
                        lock_level=new_level,
                        held_keys_same_level=held_same_level,
                    ),
                )

    def _record_acquired(
        self,
        key: str,
        level: int,
        context: Dict[str, Any],
    ) -> None:
        acquisitions = self._get_current_acquisitions()
        if acquisitions and acquisitions[-1].key == key:
            acquisitions[-1].count += 1
            self._set_current_acquisitions(acquisitions)
            return
        acquisitions.append(
            _LockAcquisition(key=key, level=level, context=dict(context), count=1)
        )
        self._set_current_acquisitions(acquisitions)

    def _find_lock_record(
        self, task: asyncio.Task[Any], key: str
    ) -> Optional[Tuple[int, _LockAcquisition]]:
        acquisitions = self._task_lock_state.get(task)
        if not acquisitions:
            return None
        for index in range(len(acquisitions) - 1, -1, -1):
            record = acquisitions[index]
            if record.key == key:
                return index, record
        return None

    def _register_waiting(
        self,
        task: asyncio.Task[Any],
        key: str,
        level: int,
        context: Dict[str, Any],
    ) -> None:
        self._waiting_tasks[task] = _WaitingInfo(key=key, level=level, context=dict(context))

    def _unregister_waiting(self, task: asyncio.Task[Any], key: str) -> None:
        info = self._waiting_tasks.get(task)
        if info is not None and info.key == key:
            self._waiting_tasks.pop(task, None)

    def _detect_cycles(
        self, graph: Dict[asyncio.Task[Any], Set[asyncio.Task[Any]]]
    ) -> List[List[str]]:
        cycles: List[List[str]] = []
        visited: Set[asyncio.Task[Any]] = set()
        stack: List[asyncio.Task[Any]] = []
        on_stack: Set[asyncio.Task[Any]] = set()

        def dfs(node: asyncio.Task[Any]) -> None:
            visited.add(node)
            stack.append(node)
            on_stack.add(node)
            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    dfs(neighbor)
                elif neighbor in on_stack:
                    cycle_nodes = stack[stack.index(neighbor) :] + [neighbor]
                    cycle_repr = [self._describe_task(item) for item in cycle_nodes]
                    if cycle_repr not in cycles:
                        cycles.append(cycle_repr)
            stack.pop()
            on_stack.remove(node)

        for node in graph:
            if node not in visited:
                dfs(node)

        return cycles

    def _lock_diagnostics(self, key: str) -> Dict[str, Any]:
        diagnostics: Dict[str, Any] = {}
        try:
            snapshot = self.detect_deadlock()
        except Exception:  # pragma: no cover - defensive logging path
            self._logger.exception(
                "Failed to capture lock diagnostics for %s", key
            )
            return diagnostics

        holders: List[str] = []
        for task_entry in snapshot.get("tasks", []):
            locks = task_entry.get("locks") or []
            if any(lock.get("key") == key for lock in locks):
                holders.append(task_entry.get("task", "unknown"))

        waiters: List[str] = [
            item.get("task", "unknown")
            for item in snapshot.get("waiting", [])
            if item.get("key") == key
        ]

        if holders:
            diagnostics["held_by_tasks"] = holders
        if waiters:
            diagnostics["waiting_tasks"] = waiters

        return diagnostics

    def _log_lock_snapshot_on_timeout(
        self,
        stage: str,
        *,
        level: int = logging.WARNING,
        minimum_level: int = logging.WARNING,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "stage": stage,
            "event_type": "lock_snapshot",
        }
        if extra:
            payload.update(extra)

        try:
            snapshot = self.detect_deadlock()
        except Exception:  # pragma: no cover - defensive logging path
            self._logger.exception(
                "Failed to capture lock snapshot during %s", stage, extra=payload
            )
            return

        if not any(snapshot.get(key) for key in ("tasks", "waiting", "cycles")):
            # When there is no diagnostic information we downgrade to DEBUG to
            # avoid noisy warning logs that do not help with troubleshooting.
            self._logger.debug(
                "Lock snapshot (%s): %s",
                stage,
                json.dumps(snapshot, ensure_ascii=False, default=str),
                extra=payload,
            )
            return

        levels_to_consider = [logging.WARNING]
        if level is not None:
            levels_to_consider.append(level)
        if minimum_level is not None:
            levels_to_consider.append(minimum_level)
        effective_level = max(levels_to_consider)
        self._logger.log(
            effective_level,
            "Lock snapshot (%s): %s",
            stage,
            json.dumps(snapshot, ensure_ascii=False, default=str),
            extra=payload,
        )

    def _format_context(self, context: Dict[str, Any]) -> str:
        if not context:
            return ""
        parts = ", ".join(f"{key}={context[key]!r}" for key in sorted(context))
        return f" [context: {parts}]"

    @property
    def metrics(self) -> Dict[str, int]:
        return dict(self._metrics)

    def get_metrics(self, *, force_refresh: bool = False) -> Dict[str, Any]:
        """Return lock manager metrics with intelligent caching."""

        current_time = time.time()

        if (
            not force_refresh
            and self._cached_metrics is not None
            and (current_time - self._cached_metrics_ts) < self._metrics_cache_ttl
        ):
            return self._cached_metrics.copy()

        metrics = {
            "lock_contention": self._metrics["lock_contention"],
            "lock_timeouts": self._metrics["lock_timeouts"],
            "lock_cancellations": self._metrics.get("lock_cancellations", 0),
            "lock_cleanup_failures": self._metrics.get("lock_cleanup_failures", 0),
            "lock_pool_hits": self._metrics.get("lock_pool_hits", 0),
            "lock_pool_misses": self._metrics.get("lock_pool_misses", 0),
            "lock_fast_path_hits": self._metrics.get("lock_fast_path_hits", 0),
            "lock_slow_path": self._metrics.get("lock_slow_path", 0),
            "active_locks": len(self._locks),
            "waiting_tasks": len(self._waiting_tasks),
            "shutdown_initiated": self._shutdown_initiated,
            "pool_size": len(self._lock_pool),
            "pool_hit_rate": self._compute_pool_hit_rate(),
            "fast_path_hit_rate": self._compute_fast_path_hit_rate(),
        }

        self._cached_metrics = metrics
        self._cached_metrics_ts = current_time

        return metrics.copy()

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus format for scraping."""

        metrics = self.get_metrics(force_refresh=True)

        lines = [
            "# HELP lock_manager_contention_total Number of lock contentions",
            "# TYPE lock_manager_contention_total counter",
            f"lock_manager_contention_total {metrics['lock_contention']}",
            "",
            "# HELP lock_manager_timeouts_total Lock acquisition timeouts",
            "# TYPE lock_manager_timeouts_total counter",
            f"lock_manager_timeouts_total {metrics['lock_timeouts']}",
            "",
            "# HELP lock_manager_cancellations_total Cancelled acquisitions",
            "# TYPE lock_manager_cancellations_total counter",
            f"lock_manager_cancellations_total {metrics['lock_cancellations']}",
            "",
            "# HELP lock_manager_active_locks Current active locks",
            "# TYPE lock_manager_active_locks gauge",
            f"lock_manager_active_locks {metrics['active_locks']}",
            "",
            "# HELP lock_manager_waiting_tasks Current waiting tasks",
            "# TYPE lock_manager_waiting_tasks gauge",
            f"lock_manager_waiting_tasks {metrics['waiting_tasks']}",
            "",
            "# HELP lock_manager_fast_path_hit_rate_percent Fast-path hit rate",
            "# TYPE lock_manager_fast_path_hit_rate_percent gauge",
            f"lock_manager_fast_path_hit_rate_percent {metrics['fast_path_hit_rate']:.2f}",
            "",
            "# HELP lock_manager_pool_hit_rate_percent Lock pool hit rate",
            "# TYPE lock_manager_pool_hit_rate_percent gauge",
            f"lock_manager_pool_hit_rate_percent {metrics['pool_hit_rate']:.2f}",
            "",
            "# HELP lock_manager_pool_size Current pooled locks",
            "# TYPE lock_manager_pool_size gauge",
            f"lock_manager_pool_size {metrics['pool_size']}",
        ]

        return "\n".join(lines) + "\n"

    def _compute_fast_path_hit_rate(self) -> float:
        """Calculate fast-path hit rate as percentage."""

        fast_hits = self._metrics.get("lock_fast_path_hits", 0)
        slow_path = self._metrics.get("lock_slow_path", 0)
        total = fast_hits + slow_path
        return (fast_hits / total * 100.0) if total > 0 else 0.0

    def _compute_pool_hit_rate(self) -> float:
        """Calculate lock pool hit rate as percentage."""

        hits = self._metrics.get("lock_pool_hits", 0)
        misses = self._metrics.get("lock_pool_misses", 0)
        total = hits + misses
        return (hits / total * 100.0) if total > 0 else 0.0

    def _log_extra(
        self,
        context: Mapping[str, Any],
        *,
        event_type: str,
        request_category: Any | None = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "game_id": context.get("game_id"),
            "chat_id": context.get("chat_id"),
            "user_id": context.get("user_id"),
            "request_category": normalise_request_category(
                request_category if request_category is not None else context.get("request_category")
            ),
            "event_type": event_type,
            "lock_context": dict(context) if context else {},
        }
        lock_name = context.get("lock_name") or context.get("lock_key")
        if lock_name is not None:
            payload.setdefault("lock_name", lock_name)
        payload.update(extra)
        return payload

    def _describe_task(self, task: asyncio.Task[Any]) -> str:
        name = task.get_name()
        return f"{name}#{id(task):x}"

    def _resolve_call_site(self) -> Tuple[str, str]:
        call_site = "unknown"
        function_name = "unknown"
        frame = inspect.currentframe()
        try:
            if frame is not None:
                outer_frames = inspect.getouterframes(frame, 3)
                target = None
                if len(outer_frames) >= 3:
                    target = outer_frames[2]
                elif len(outer_frames) >= 2:
                    target = outer_frames[1]
                if target is not None:
                    call_site = f"{target.filename}:{target.lineno}"
                    if getattr(target, "function", None):
                        function_name = str(target.function)
                del outer_frames
        except Exception:
            call_site = "unknown"
            function_name = "unknown"
        finally:
            del frame
        return call_site, function_name

    def _make_action_lock_key(
        self,
        chat_id: int,
        user_id: int,
        action_identifier: Optional[str] = None,
    ) -> str:
        base = (
            self._redis_keys["action_lock_prefix"]
            + f"{int(chat_id)}:{int(user_id)}"
        )
        if action_identifier:
            return f"{base}:{str(action_identifier)}"
        return base

    def _make_table_lock_key(self, chat_id: int, operation: str) -> str:
        engine_keys = self._redis_keys.get("engine", {})
        prefix = engine_keys.get("table_lock_prefix", "table:lock:")
        try:
            normalized_chat = int(chat_id)
        except (TypeError, ValueError):
            normalized_chat = chat_id
        return f"{prefix}{normalized_chat}:{operation}"

    async def _execute_release_lock_script(self, redis_key: str, token: str) -> int:
        try:
            try:
                return await self._redis_pool.eval(
                    self._release_lock_script,
                    keys=[redis_key],
                    args=[token],
                )
            except TypeError:
                return await self._redis_pool.eval(
                    self._release_lock_script,
                    1,
                    redis_key,
                    token,
                )
        except ModuleNotFoundError:
            current_value = await self._redis_pool.get(redis_key)
            if isinstance(current_value, bytes):
                current_value = current_value.decode()
            if current_value == token:
                deleted = await self._redis_pool.delete(redis_key)
                return 1 if deleted else 0
            return 0

    async def acquire_table_lock(
        self,
        *,
        chat_id: int,
        operation: str,
        timeout_seconds: int = 5,
    ) -> Optional[str]:
        if timeout_seconds <= 0:
            timeout_seconds = 1

        ttl_seconds = int(timeout_seconds)
        if ttl_seconds <= 0:
            ttl_seconds = 1

        lock_key = self._make_table_lock_key(chat_id, operation)
        token = str(uuid.uuid4())

        try:
            acquired = await self._redis_pool.set(
                lock_key,
                token,
                nx=True,
                ex=ttl_seconds,
            )
        except aioredis.ConnectionError as exc:
            self._logger.error(
                "[TABLE_LOCK] Failed to acquire distributed lock (redis error)",
                extra={
                    "event_type": "table_lock_acquire_error",
                    "chat_id": chat_id,
                    "operation": operation,
                    "timeout_seconds": timeout_seconds,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return None

        if acquired:
            self._logger.debug(
                "Table lock acquired",
                extra={
                    "event_type": "table_lock_acquired",
                    "chat_id": chat_id,
                    "operation": operation,
                    "token_prefix": token[:8],
                    "ttl_seconds": ttl_seconds,
                },
            )
            return token

        self._logger.debug(
            "Table lock already held",
            extra={
                "event_type": "table_lock_contention",
                "chat_id": chat_id,
                "operation": operation,
            },
        )
        return None

    async def release_table_lock(
        self,
        chat_id: int,
        token: str,
        operation: str = "join",
    ) -> bool:
        """Release a table-level lock using token validation.

        Args:
            chat_id: Chat ID to release lock for
            token: Lock token from acquire_table_lock
            operation: Lock operation type ("join" or "leave")

        Returns:
            True if successfully released, False otherwise
        """

        lock_key = self._make_table_lock_key(chat_id, operation)

        try:
            result = await self._execute_release_lock_script(lock_key, token)
        except aioredis.ConnectionError as exc:
            self._logger.error(
                "[TABLE_LOCK] Failed to release distributed lock (redis error)",
                extra={
                    "event_type": "table_lock_release_error",
                    "chat_id": chat_id,
                    "operation": operation,
                    "token_prefix": token[:8],
                    "error": str(exc),
                },
                exc_info=True,
            )
            return False

        if result == 1:
            self._logger.debug(
                "Table lock release",
                extra={
                    "event_type": "table_lock_released",
                    "chat_id": chat_id,
                    "token_prefix": token[:8],
                },
            )
            return True

        self._logger.debug(
            "Table lock release failed",
            extra={
                "event_type": "table_lock_release_failed",
                "chat_id": chat_id,
                "token_prefix": token[:8],
            },
        )
        return False

    async def acquire_action_lock(
        self,
        chat_id: int,
        user_id: int,
        action_type_or_timeout: Optional[object] = None,
        *,
        action_type: Optional[str] = None,
        action_data: Optional[str] = None,
        ttl: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Optional[str]:
        """Acquire a short-lived distributed lock for a player's action.

        Args:
            chat_id: Telegram chat identifier.
            user_id: Telegram user identifier.
            action_type_or_timeout: Optional positional parameter used either as
                an action type (preferred) or a legacy timeout override.
            action_type: Explicit action type ("fold", "check", "call", "raise").
            action_data: Legacy payload identifier used in earlier releases.
            ttl: Explicit TTL in seconds for the distributed lock.
            timeout_seconds: Backwards-compatible TTL override.
        """

        resolved_action_type: Optional[str] = action_type
        resolved_timeout: Optional[float] = None

        if isinstance(action_type_or_timeout, str) and resolved_action_type is None:
            resolved_action_type = action_type_or_timeout.strip().lower()
        elif isinstance(action_type_or_timeout, (int, float)) and ttl is None and timeout_seconds is None:
            resolved_timeout = float(action_type_or_timeout)

        if ttl is not None and ttl <= 0:
            ttl = 1

        if timeout_seconds is not None and timeout_seconds <= 0:
            timeout_seconds = 1

        if resolved_timeout is None:
            resolved_timeout = timeout_seconds

        ttl_seconds = (
            int(ttl)
            if ttl is not None
            else (
                int(resolved_timeout)
                if resolved_timeout is not None
                else self._action_lock_default_ttl
            )
        )
        if ttl_seconds <= 0:
            ttl_seconds = 1

        action_identifier: Optional[str]
        if resolved_action_type:
            action_identifier = resolved_action_type
        else:
            action_identifier = action_data

        redis_key = self._make_action_lock_key(chat_id, user_id, action_identifier)
        token = str(uuid.uuid4())

        try:
            acquired = await self._redis_pool.set(
                redis_key,
                token,
                nx=True,
                ex=ttl_seconds,
            )
        except aioredis.ConnectionError as exc:
            self._logger.error(
                "[ACTION_LOCK] Failed to acquire distributed lock (redis error)",
                extra={
                    "event_type": "action_lock_acquire_error",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "action_type": resolved_action_type,
                    "ttl_seconds": ttl_seconds,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return None

        if acquired:
            self._logger.debug(
                "[ACTION_LOCK] Acquired distributed lock",
                extra={
                    "event_type": "action_lock_acquired",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "token_prefix": token[:8],
                    "ttl_seconds": ttl_seconds,
                    "action_identifier": action_identifier,
                },
            )
            return token

        self._logger.debug(
            "[ACTION_LOCK] Lock contention",
            extra={
                "event_type": "action_lock_contention",
                "chat_id": chat_id,
                "user_id": user_id,
                "action_identifier": action_identifier,
            },
        )
        return None

    async def release_action_lock(
        self,
        chat_id: int,
        user_id: int,
        token_or_action: Optional[str] = None,
        token: Optional[str] = None,
        *,
        action_type: Optional[str] = None,
        action_data: Optional[str] = None,
        lock_token: Optional[str] = None,
    ) -> bool:
        """Release an action lock using token validation.

        Supports both ``release_action_lock(chat_id, user_id, token)`` and the
        newer ``release_action_lock(chat_id, user_id, action_type, token)``
        signature.
        """

        if lock_token is not None and token is None:
            token = lock_token

        resolved_token: Optional[str]
        resolved_action_type: Optional[str] = action_type
        if token is None and token_or_action is not None:
            resolved_token = token_or_action
        else:
            resolved_token = token
            if (
                resolved_action_type is None
                and token_or_action is not None
                and isinstance(token_or_action, str)
            ):
                resolved_action_type = token_or_action.strip().lower()

        if resolved_token is None:
            raise ValueError(
                "Token must be provided when releasing an action lock."
            )

        action_identifier: Optional[str]
        if resolved_action_type:
            action_identifier = resolved_action_type
        else:
            action_identifier = action_data

        redis_key = self._make_action_lock_key(chat_id, user_id, action_identifier)

        try:
            result = await self._execute_release_lock_script(
                redis_key, resolved_token
            )
        except aioredis.ConnectionError as exc:
            self._logger.error(
                "[ACTION_LOCK] Failed to release distributed lock (redis error)",
                extra={
                    "event_type": "action_lock_release_error",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "action_identifier": action_identifier,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return False

        if result == 1:
            self._logger.debug(
                "[ACTION_LOCK] Released distributed lock",
                extra={
                    "event_type": "action_lock_released",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "token_prefix": resolved_token[:8],
                    "action_identifier": action_identifier,
                },
            )
            return True

        self._logger.warning(
            "[ACTION_LOCK] Release failed - token mismatch or lock expired",
            extra={
                "event_type": "action_lock_release_failed",
                "chat_id": chat_id,
                "user_id": user_id,
                "token_prefix": resolved_token[:8],
                "action_identifier": action_identifier,
            },
        )
        return False

    async def clear_all_locks(self) -> int:
        """Remove all tracked locks and return how many were cleared."""

        async with self._locks_guard:
            cleared = len(self._locks)
            self._locks.clear()
        return cleared

__all__ = ["LockManager", "LockOrderError"]
