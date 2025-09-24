"""Centralized asynchronous lock management for the poker bot."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Set, Tuple
from weakref import WeakKeyDictionary

from pokerapp.utils.locks import ReentrantAsyncLock
from pokerapp.utils.logging_helpers import add_context, normalise_request_category


LOCK_LEVELS: Dict[str, int] = {
    "global": 0,
    "stage": 10,
    "table": 20,
    "player": 30,
    "hand": 40,
}


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
    ) -> None:
        self._logger = add_context(logger)
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

    async def _get_lock(self, key: str) -> ReentrantAsyncLock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = ReentrantAsyncLock()
                self._locks[key] = lock
            return lock

    async def acquire(
        self,
        key: str,
        timeout: Optional[float] = None,
        *,
        context: Optional[Mapping[str, Any]] = None,
        level: Optional[int] = None,
    ) -> bool:
        """Attempt to acquire the lock identified by ``key``."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("LockManager.acquire requires an active asyncio task")

        lock = await self._get_lock(key)
        resolved_level = self._resolve_level(key, override=level)
        context_payload = dict(context or {})
        self._validate_lock_order(task, key, resolved_level, context_payload)

        total_timeout = self._default_timeout_seconds if timeout is None else timeout
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
            try:
                if attempt_timeout is None:
                    await lock.acquire()
                else:
                    await asyncio.wait_for(lock.acquire(), timeout=attempt_timeout)
                elapsed = loop.time() - attempt_start
                self._record_acquired(task, key, resolved_level, context_payload)
                if attempt == 0 and elapsed < 0.1:
                    self._logger.info(
                        "Lock '%s' acquired quickly in %.3fs%s",
                        key,
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
                        "Lock '%s' acquired after %d attempt(s) in %.3fs%s",
                        key,
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
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - loop.time())
                self._logger.warning(
                    "Timeout acquiring lock '%s' on attempt %d (remaining %.3fs)%s",
                    key,
                    attempt + 1,
                    remaining if remaining is not None else float("inf"),
                    self._format_context(context_payload),
                    extra=self._log_extra(
                        context_payload,
                        event_type="lock_timeout",
                        lock_key=key,
                        lock_level=resolved_level,
                        attempts=attempt + 1,
                        remaining_time=remaining,
                    ),
                )
            except asyncio.CancelledError:
                self._logger.warning(
                    "Lock acquisition for '%s' cancelled on attempt %d%s",
                    key,
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

        self._logger.error(
            "Failed to acquire lock '%s' after %d attempts%s",
            key,
            attempts,
            self._format_context(context_payload),
            extra=self._log_extra(
                context_payload,
                event_type="lock_failure",
                lock_key=key,
                lock_level=resolved_level,
                attempts=attempts,
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
        level: Optional[int] = None,
    ) -> AsyncIterator[None]:
        acquired = await self.acquire(key, timeout=timeout, context=context, level=level)
        if not acquired:
            message = f"Timeout acquiring lock '{key}'"
            self._logger.warning(
                "%s%s",
                message,
                self._format_context(dict(context or {})),
                extra=self._log_extra(
                    dict(context or {}),
                    event_type="lock_guard_timeout",
                    lock_key=key,
                    lock_level=level,
                ),
            )
            raise TimeoutError(message)
        try:
            yield
        finally:
            self.release(key, context=context)

    def release(
        self, key: str, context: Optional[Mapping[str, Any]] = None
    ) -> None:
        lock = self._locks.get(key)
        context_payload = dict(context or {})
        if lock is None:
            self._logger.debug(
                "Release requested for unknown lock '%s'%s",
                key,
                self._format_context(context_payload),
                extra=self._log_extra(
                    context_payload,
                    event_type="lock_release_unknown",
                    lock_key=key,
                    lock_level=None,
                ),
            )
            return

        task = asyncio.current_task()
        record_entry: Optional[Tuple[int, _LockAcquisition]] = None
        if task is not None:
            record_entry = self._find_lock_record(task, key)
            if record_entry is not None and not context_payload:
                context_payload = dict(record_entry[1].context)
        try:
            lock.release()
        except RuntimeError:
            self._logger.exception(
                "Failed to release lock '%s' due to ownership mismatch%s",
                key,
                self._format_context(context_payload),
                extra=self._log_extra(
                    context_payload,
                    event_type="lock_release_error",
                    lock_key=key,
                    lock_level=None,
                ),
            )
            raise
        else:
            if task is not None and record_entry is not None:
                context_payload = self._finalize_release(task, record_entry[0])
            self._logger.info(
                "Lock '%s' released%s",
                key,
                self._format_context(context_payload),
                extra=self._log_extra(
                    context_payload,
                    event_type="lock_released",
                    lock_key=key,
                    lock_level=None,
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

    def _resolve_level(self, key: str, *, override: Optional[int]) -> int:
        if override is not None:
            return override
        category = key.split(":", 1)[0]
        return LOCK_LEVELS.get(category, self._default_lock_level)

    def _validate_lock_order(
        self,
        task: asyncio.Task[Any],
        key: str,
        level: int,
        context: Dict[str, Any],
    ) -> None:
        acquisitions = self._task_lock_state.get(task)
        if not acquisitions:
            return
        if any(item.key == key for item in acquisitions):
            # Re-entrant acquire; allow regardless of order.
            return
        highest = max(acquisitions, key=lambda item: item.level)
        if level < highest.level:
            held_contexts = [
                f"{item.key}(level={item.level})" for item in acquisitions
            ]
            message = (
                "Lock ordering violation: attempting to acquire '%s' (level=%d) while "
                "holding %s"
            ) % (key, level, held_contexts)
            self._logger.error(
                "%s%s",
                message,
                self._format_context(context),
                extra=self._log_extra(
                    context,
                    event_type="lock_order_violation",
                    lock_key=key,
                    lock_level=level,
                ),
            )
            raise RuntimeError(message)

    def _record_acquired(
        self,
        task: asyncio.Task[Any],
        key: str,
        level: int,
        context: Dict[str, Any],
    ) -> None:
        acquisitions = self._task_lock_state.get(task)
        if acquisitions is None:
            acquisitions = []
            self._task_lock_state[task] = acquisitions
        if acquisitions and acquisitions[-1].key == key:
            acquisitions[-1].count += 1
            return
        acquisitions.append(
            _LockAcquisition(key=key, level=level, context=dict(context), count=1)
        )

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

    def _finalize_release(
        self, task: asyncio.Task[Any], index: int
    ) -> Dict[str, Any]:
        acquisitions = self._task_lock_state.get(task)
        if not acquisitions or not (0 <= index < len(acquisitions)):
            return {}
        record = acquisitions[index]
        record.count -= 1
        context = dict(record.context)
        if record.count <= 0:
            acquisitions.pop(index)
        if acquisitions:
            self._task_lock_state[task] = acquisitions
        else:
            self._task_lock_state.pop(task, None)
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

    def _format_context(self, context: Dict[str, Any]) -> str:
        if not context:
            return ""
        parts = ", ".join(f"{key}={context[key]!r}" for key in sorted(context))
        return f" [context: {parts}]"

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
        payload.update(extra)
        return payload

    def _describe_task(self, task: asyncio.Task[Any]) -> str:
        name = task.get_name()
        return f"{name}#{id(task):x}"

__all__ = ["LockManager"]
