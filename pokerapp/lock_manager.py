"""Centralized asynchronous lock management for the poker bot."""

from __future__ import annotations

import asyncio
import json
import logging
import math
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


class LockManager:
    """Manage keyed re-entrant async locks with timeout and retry support."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
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
        self._metrics: Dict[str, int] = {"lock_contention": 0, "lock_timeouts": 0}

    async def _get_lock(self, key: str) -> ReentrantAsyncLock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = ReentrantAsyncLock()
                self._locks[key] = lock
            return lock

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

        lock = await self._get_lock(key)
        resolved_level = self._resolve_level(key, override=level)
        context_payload = self._build_context_payload(
            key, resolved_level, additional=context
        )
        current_acquisitions = self._get_current_acquisitions()
        self._validate_lock_order(current_acquisitions, key, resolved_level, context_payload)
        acquisition_order = self._get_current_levels()
        acquiring_extra = self._log_extra(
            context_payload,
            event_type="lock_acquiring",
            lock_key=key,
            lock_level=resolved_level,
            acquisition_order=acquisition_order,
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
        for attempt in range(attempts):
            attempt_start = loop.time()
            attempt_timeout: Optional[float]
            if deadline is None:
                attempt_timeout = None
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                remaining_attempts = attempts - attempt
                attempt_timeout = remaining / remaining_attempts
                if attempt_timeout <= 0:
                    break

            self._register_waiting(task, key, resolved_level, context_payload)
            lock_identity = self._format_lock_identity(
                key, resolved_level, context_payload
            )
            try:
                owner = getattr(lock, "_owner", None)
                if owner is not None and owner is not task:
                    self._metrics["lock_contention"] += 1
                if attempt_timeout is None:
                    await lock.acquire()
                else:
                    await asyncio.wait_for(lock.acquire(), timeout=attempt_timeout)
                elapsed = loop.time() - attempt_start
                self._record_acquired(key, resolved_level, context_payload)
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
                        ),
                    )
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
            except asyncio.CancelledError:
                self._logger.warning(
                    "Lock acquisition for %s cancelled on attempt %d%s",
                    lock_identity,
                    attempt + 1,
                    self._format_context(context_payload),
                    extra=self._log_extra(
                        context_payload,
                        event_type="lock_cancelled",
                        lock_key=key,
                        lock_level=resolved_level,
                        attempts=attempt + 1,
                    ),
                )
                raise
            finally:
                self._unregister_waiting(task, key)

            if attempt < attempts - 1:
                backoff = self._retry_backoff_seconds * (2**attempt)
                if backoff > 0:
                    if deadline is None:
                        await asyncio.sleep(backoff)
                    else:
                        remaining_sleep = deadline - loop.time()
                        if remaining_sleep <= 0:
                            break
                        await asyncio.sleep(min(backoff, remaining_sleep))

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
                    held_by_tasks=diagnostics.get("held_by_tasks"),
                    waiting_tasks=diagnostics.get("waiting_tasks"),
                ),
            )
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
            self.release(key, context=combined_context)

    def release(
        self, key: str, context: Optional[Mapping[str, Any]] = None
    ) -> None:
        lock = self._locks.get(key)
        base_context = dict(context or {})
        if lock is None:
            resolved_level = self._resolve_level(key, override=None)
            unknown_context = self._build_context_payload(
                key, resolved_level, additional=base_context
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
                ),
            )
            return

        task = asyncio.current_task()
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
                ),
            )
            raise
        else:
            if task is not None and record_entry is not None:
                release_context = self._finalize_release(record_entry[0])
                if release_context:
                    context_payload = release_context
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

    def _validate_lock_order(
        self,
        acquisitions: List[_LockAcquisition],
        key: str,
        level: int,
        context: Dict[str, Any],
    ) -> None:
        if not acquisitions:
            return
        if any(item.key == key for item in acquisitions):
            # Re-entrant acquire; allow regardless of order.
            return
        held_levels = self._get_current_levels()
        highest_level = max(held_levels) if held_levels else None
        if highest_level is None or level >= highest_level:
            return
        held_contexts = [f"{item.key}(level={item.level})" for item in acquisitions]
        lock_identity = self._format_lock_identity(key, level, context)
        message = (
            "Lock ordering violation: attempting to acquire %s while holding %s"
        ) % (lock_identity, held_contexts)
        violation_context = dict(context)
        violation_context["acquisition_order"] = held_levels
        self._logger.error(
            "%s%s",
            message,
            self._format_context(violation_context),
            extra=self._log_extra(
                violation_context,
                event_type="lock_order_violation",
                lock_key=key,
                lock_level=level,
                acquisition_order=held_levels,
            ),
        )
        raise LockOrderError(message)

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

    def _finalize_release(self, index: int) -> Dict[str, Any]:
        acquisitions = self._get_current_acquisitions()
        if not acquisitions or not (0 <= index < len(acquisitions)):
            return {}
        record = acquisitions[index]
        record.count -= 1
        context = dict(record.context)
        if record.count <= 0:
            acquisitions.pop(index)
        self._set_current_acquisitions(acquisitions)
        return context

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

__all__ = ["LockManager", "LockOrderError"]
