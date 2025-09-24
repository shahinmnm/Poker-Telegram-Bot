"""Centralised messaging utilities for Telegram interactions.

This module provides the :class:`MessagingService` helper which serialises
all outgoing Telegram requests made by the bot.  The helper implements
per-message locking, content hashing and deduplication, as well as structured
logging so that the surrounding poker application can keep API usage within
Telegram's strict limits.

The public ``send_message``, ``edit_message_text`` and ``delete_message``
coroutines mirror the behaviour of the underlying Telegram client (aiogram or
python-telegram-bot).  Each method acquires an ``asyncio.Lock`` for the target
message, prevents repeated identical edits using a small in-memory cache, and
handles common ``400 Bad Request`` responses gracefully.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import datetime
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set, Tuple

from cachetools import TTLCache

try:  # pragma: no cover - prometheus_client optional
    from prometheus_client import Counter
except Exception:  # pragma: no cover - optional dependency missing
    Counter = None  # type: ignore[assignment]

from pokerapp.utils.time_utils import now_utc
from pokerapp.utils.debug_trace import trace_telegram_api_call
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics

try:  # pragma: no cover - aiogram is optional at runtime
    from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
except Exception:  # pragma: no cover - fallback for PTB-only deployments
    TelegramBadRequest = None  # type: ignore[assignment]
    TelegramRetryAfter = None  # type: ignore[assignment]

try:  # pragma: no cover - python-telegram-bot is optional during testing
    from telegram.error import BadRequest as PTBBadRequest, RetryAfter as PTBRetryAfter
except Exception:  # pragma: no cover
    PTBBadRequest = None  # type: ignore[assignment]
    PTBRetryAfter = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)
debug_trace_logger = logging.getLogger("pokerbot.debug_trace")

if Counter is not None:  # pragma: no branch - simple configuration
    _TELEGRAM_EVENT_COUNTER = Counter(
        "pokerbot_telegram_events_total",
        "Total number of MessagingService events emitted.",
        ("action", "method", "category"),
    )
else:  # pragma: no cover - dependency not installed during tests
    _TELEGRAM_EVENT_COUNTER = None


CacheKey = Tuple[int, int]
CacheEntryKey = Tuple[int, int, str]


@dataclass(slots=True)
class _EditPayload:
    """Payload information for a queued edit operation."""

    text: Optional[str]
    reply_markup: Any
    params: Dict[str, Any]
    force: bool
    request_category: RequestCategory
    content_hash: str
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _EditWaiter:
    """Track awaiting callers for a queued edit."""

    future: "asyncio.Future[Optional[int]]"
    category: RequestCategory
    content_hash: str
    superseded: bool = False
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _PendingEditState:
    """Mutable state for coalescing edits on a single message."""

    guard: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_payload: Optional[_EditPayload] = None
    waiters: List[_EditWaiter] = field(default_factory=list)
    update_event: asyncio.Event = field(default_factory=asyncio.Event)
    flush_task: Optional[asyncio.Task] = None
    first_update: float = 0.0
    last_update: float = 0.0
    last_flush: float = 0.0
    delete_waiters: List["asyncio.Future[bool]"] = field(default_factory=list)


class MessagingService:
    """Encapsulate all outgoing Telegram requests for the poker bot.

    Parameters
    ----------
    bot:
        The underlying Telegram client instance.  The object must expose
        ``send_message``, ``edit_message_text`` and ``delete_message``
        coroutines that follow the standard Telegram Bot API.
    cache_ttl:
        How long, in seconds, identical payload hashes should be remembered.
    cache_maxsize:
        Maximum number of message hashes stored in the cache.
    logger_:
        Optional custom :class:`logging.Logger` instance used for diagnostics.
    """

    #: Maximum time to keep coalescing edits for the same message.
    _COALESCE_WINDOW = 1.0
    #: Minimum quiet period before flushing a queued edit.
    _COALESCE_IDLE = 0.2
    #: Minimum delay applied to quiet chats (seconds).
    _MIN_DELAY = 0.02
    #: Maximum delay applied to very busy chats (seconds).
    _MAX_DELAY = 0.15
    #: Minimum delay between any two messages globally (seconds).
    _SAFE_GLOBAL_DELAY = 0.02
    #: RetryAfter exception classes recognised by the service.
    _RETRY_AFTER_EXCEPTIONS: Tuple[type, ...] = tuple(
        exc for exc in (TelegramRetryAfter, PTBRetryAfter) if exc is not None
    )

    def __init__(
        self,
        bot: Any,
        *,
        cache_ttl: int = 3,
        cache_maxsize: int = 500,
        logger_: logging.Logger,
        request_metrics: RequestMetrics,
        deleted_messages: Optional[Set[int]] = None,
        deleted_messages_lock: Optional[asyncio.Lock] = None,
        last_message_hash: Optional[Dict[int, str]] = None,
        last_message_hash_lock: Optional[asyncio.Lock] = None,
    ) -> None:
        self._bot = bot
        if logger_ is None:
            raise ValueError("logger_ dependency must be provided")
        if request_metrics is None:
            raise ValueError("request_metrics dependency must be provided")

        self._logger = logger_
        self._content_cache: TTLCache[CacheEntryKey, bool] = TTLCache(
            maxsize=cache_maxsize,
            ttl=cache_ttl,
        )
        self._cache_lock = asyncio.Lock()
        self._locks: Dict[CacheKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._metrics = request_metrics
        self._pending_edits: Dict[CacheKey, _PendingEditState] = {}
        self._last_edit_timestamp: Dict[CacheKey, datetime.datetime] = {}
        self._deleted_messages_ref = deleted_messages
        self._deleted_messages_lock = deleted_messages_lock
        self._last_message_hash_ref = last_message_hash
        self._last_message_hash_lock = last_message_hash_lock
        self._send_history_per_chat: defaultdict[int, deque[float]] = defaultdict(
            lambda: deque(maxlen=10)
        )
        self._global_last_send_time = 0.0
        self._last_edit_failures: Dict[CacheKey, str] = {}
        self._last_edit_failure_lock = asyncio.Lock()

    @staticmethod
    def _coerce_context_value(value: Any) -> Any:
        if isinstance(value, RequestCategory):
            return value.value
        return value

    def _merge_context(
        self,
        context: Optional[Mapping[str, Any]],
        **defaults: Any,
    ) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        if context:
            for key, value in context.items():
                if value is None:
                    continue
                merged[key] = self._coerce_context_value(value)
        for key, value in defaults.items():
            if value is None:
                continue
            merged[key] = self._coerce_context_value(value)
        if "service" not in merged:
            merged["service"] = "MessagingService"
        return merged

    def _record_event_metric(
        self,
        action: str,
        *,
        method: Optional[str],
        category: Optional[Any],
    ) -> None:
        if _TELEGRAM_EVENT_COUNTER is None:  # pragma: no cover - optional metrics
            return
        label_method = method or "unknown"
        if isinstance(category, RequestCategory):
            label_category = category.value
        elif isinstance(category, str):
            label_category = category
        elif category is None:
            label_category = "none"
        else:
            label_category = str(category)
        _TELEGRAM_EVENT_COUNTER.labels(
            action=action,
            method=label_method,
            category=label_category,
        ).inc()

    def _log_event(
        self,
        action: str,
        *,
        level: int = logging.INFO,
        context: Optional[Mapping[str, Any]] = None,
        include_debug_trace: bool = False,
    ) -> None:
        payload = self._merge_context(context, action=action)
        method = payload.get("method")
        category = payload.get("category")
        self._logger.log(level, action, extra=dict(payload))
        if include_debug_trace:
            debug_trace_logger.log(level, action, extra=dict(payload))
        self._record_event_metric(action, method=method, category=category)

    async def _throttle_send(
        self,
        chat_id: int,
        *,
        method: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Apply per-chat and global throttling before contacting Telegram."""

        chat_key = int(chat_id)
        now = time.monotonic()
        history = self._send_history_per_chat[chat_key]
        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            method=method,
            category="throttle",
        )

        if history:
            time_since_chat = now - history[-1]
        else:
            time_since_chat = float("inf")

        if len(history) > 1:
            speed = len(history) / (history[-1] - history[0] + 1e-6)
        else:
            speed = 0.0

        if speed < 2.0:
            per_chat_delay = self._MIN_DELAY
        elif speed < 4.0:
            per_chat_delay = (self._MIN_DELAY + self._MAX_DELAY) / 2
        else:
            per_chat_delay = self._MAX_DELAY

        if time_since_chat < per_chat_delay:
            delay_needed = per_chat_delay - time_since_chat
            throttle_context = self._merge_context(
                base_context,
                delay=delay_needed,
                speed=speed,
                throttle_scope="chat",
            )
            self._log_event(
                "THROTTLE_CHAT",
                context=throttle_context,
                include_debug_trace=True,
            )
            await asyncio.sleep(delay_needed)

        now = time.monotonic()
        time_since_global = now - self._global_last_send_time
        if time_since_global < self._SAFE_GLOBAL_DELAY:
            delay_needed = self._SAFE_GLOBAL_DELAY - time_since_global
            global_context = self._merge_context(
                base_context,
                delay=delay_needed,
                throttle_scope="global",
            )
            self._log_event(
                "THROTTLE_GLOBAL",
                context=global_context,
                include_debug_trace=True,
            )
            await asyncio.sleep(delay_needed)

    def _register_send_time(self, chat_id: int) -> None:
        """Record when the last Telegram API call was successfully made."""

        timestamp = time.monotonic()
        chat_key = int(chat_id)
        self._send_history_per_chat[chat_key].append(timestamp)
        self._global_last_send_time = timestamp

    async def _call_with_retry(
        self,
        *,
        chat_id: int,
        message_id: Optional[int],
        method: str,
        call: Callable[[], Awaitable[Any]],
        throttle: Callable[[], Awaitable[None]],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Execute a Telegram API call, retrying once if a RetryAfter occurs."""

        retry_classes = self._RETRY_AFTER_EXCEPTIONS
        if not retry_classes:
            return await call()

        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=message_id,
            method=method,
        )

        try:
            return await call()
        except retry_classes as exc:  # type: ignore[misc]
            delay = getattr(exc, "retry_after", None)
            if delay is None:
                raise
            retry_context = self._merge_context(
                base_context,
                retry_after=float(delay),
                error_type=type(exc).__name__,
                category=base_context.get("category") or "retry_after",
            )
            self._log_event(
                "RETRY_AFTER",
                level=logging.WARNING,
                context=retry_context,
                include_debug_trace=True,
            )
            await asyncio.sleep(float(delay))
            await throttle()
            return await call()
        except Exception as exc:
            error_context = self._merge_context(
                base_context,
                error_type=type(exc).__name__,
                category=base_context.get("category") or "api_error",
            )
            self._log_event(
                "API_ERROR",
                level=logging.ERROR,
                context=error_context,
                include_debug_trace=True,
            )
            raise

    async def send_message(
        self,
        *,
        chat_id: int,
        text: Optional[str],
        reply_markup: Any = None,
        request_category: RequestCategory = RequestCategory.GENERAL,
        context: Optional[Mapping[str, Any]] = None,
        **params: Any,
    ) -> Any:
        """Send a Telegram message and register its content hash.

        The method acquires a per-chat lock so that concurrent sends to the
        same chat remain ordered.  Once the Telegram API call succeeds the new
        ``message_id`` and its content hash are recorded, allowing future edits
        to be deduplicated.
        """

        if not await self._consume_budget(
            method="sendMessage",
            chat_id=chat_id,
            message_id=None,
            category=request_category,
        ):
            return None

        telegram_method = "sendMessage"
        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=None,
            category=request_category,
            method=telegram_method,
        )

        lock = await self._acquire_lock(chat_id, 0)
        async with lock:
            await self._throttle_send(
                chat_id,
                method=telegram_method,
                context=base_context,
            )
            trace_telegram_api_call(
                "sendMessage",
                chat_id=chat_id,
                message_id=None,
                text=text,
                reply_markup=reply_markup,
            )
            result = await self._call_with_retry(
                chat_id=chat_id,
                message_id=None,
                method=telegram_method,
                call=lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    **params,
                ),
                throttle=lambda: self._throttle_send(
                    chat_id,
                    method=telegram_method,
                    context=base_context,
                ),
                context=base_context,
            )
            self._register_send_time(chat_id)

            message_id = getattr(result, "message_id", None)
            if message_id is not None:
                content_hash = self._content_hash(text, reply_markup)
                await self._remember_content(chat_id, message_id, content_hash)
                if text is not None:
                    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
                    await self._set_last_text_hash(message_id, text_hash)
                self._log_api_call(
                    telegram_method,
                    context=self._merge_context(
                        base_context,
                        message_id=message_id,
                        content_hash=content_hash,
                    ),
                )
            else:
                self._log_api_call(
                    telegram_method,
                    context=self._merge_context(
                        base_context,
                        content_hash=self._content_hash(text, reply_markup),
                    ),
                )

            return result

    async def send_photo(
        self,
        *,
        chat_id: int,
        photo: Any,
        request_category: RequestCategory = RequestCategory.MEDIA,
        caption: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        **params: Any,
    ) -> Any:
        """Send a photo message while recording the request budget usage."""

        if not await self._consume_budget(
            method="sendPhoto",
            chat_id=chat_id,
            message_id=None,
            category=request_category,
        ):
            return None

        telegram_method = "sendPhoto"
        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=None,
            category=request_category,
            method=telegram_method,
        )

        lock = await self._acquire_lock(chat_id, 0)
        async with lock:
            await self._throttle_send(
                chat_id,
                method=telegram_method,
                context=base_context,
            )
            trace_telegram_api_call(
                "sendPhoto",
                chat_id=chat_id,
                message_id=None,
                text=caption,
            )
            result = await self._call_with_retry(
                chat_id=chat_id,
                message_id=None,
                method=telegram_method,
                call=lambda: self._bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    **params,
                ),
                throttle=lambda: self._throttle_send(
                    chat_id,
                    method=telegram_method,
                    context=base_context,
                ),
                context=base_context,
            )
            self._register_send_time(chat_id)

            message_id = getattr(result, "message_id", None)
            self._log_api_call(
                telegram_method,
                context=self._merge_context(
                    base_context,
                    message_id=message_id,
                    content_hash="-",
                ),
            )
            return result

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: Optional[str],
        reply_markup: Any = None,
        force: bool = False,
        request_category: RequestCategory = RequestCategory.GENERAL,
        context: Optional[Mapping[str, Any]] = None,
        **params: Any,
    ) -> Optional[int]:
        """Edit an existing Telegram message while avoiding duplicate edits."""

        if message_id is None:
            return None

        loop = asyncio.get_running_loop()
        content_hash = self._content_hash(text, reply_markup)
        telegram_method = "editMessageText"
        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=message_id,
            category=request_category,
            method=telegram_method,
        )

        payload = _EditPayload(
            text=text,
            reply_markup=reply_markup,
            params=dict(params),
            force=force,
            request_category=request_category,
            content_hash=content_hash,
            context=dict(base_context),
        )
        future: "asyncio.Future[Optional[int]]" = loop.create_future()
        waiter = _EditWaiter(
            future=future,
            category=request_category,
            content_hash=content_hash,
            context=dict(base_context),
        )

        await self._enqueue_edit(
            chat_id=chat_id,
            message_id=message_id,
            payload=payload,
            waiter=waiter,
        )

        return await future

    async def _enqueue_edit(
        self,
        *,
        chat_id: int,
        message_id: int,
        payload: _EditPayload,
        waiter: _EditWaiter,
    ) -> None:
        """Queue an edit request so multiple updates can be coalesced."""

        key = (int(chat_id), int(message_id))
        state = self._pending_edits.setdefault(key, _PendingEditState())

        async with state.guard:
            if state.pending_payload is None:
                if (
                    not payload.force
                    and await self._should_skip(chat_id, message_id, payload.content_hash)
                ):
                    await self._log_skip(
                        chat_id=chat_id,
                        message_id=message_id,
                        category=payload.request_category,
                        reason="hash_match",
                        context=payload.context,
                    )
                    if not waiter.future.done():
                        waiter.future.set_result(message_id)
                    return
                state.pending_payload = payload
                state.waiters.append(waiter)
                now = time.monotonic()
                state.first_update = now
                state.last_update = now
            else:
                pending_hash = state.pending_payload.content_hash
                if (
                    pending_hash == payload.content_hash
                    and not payload.force
                ):
                    # Join the existing pending edit without logging a skip.
                    state.waiters.append(waiter)
                    state.last_update = time.monotonic()
                else:
                    for existing in state.waiters:
                        if not existing.superseded:
                            existing.superseded = True
                            await self._log_skip(
                                chat_id=chat_id,
                                message_id=message_id,
                                category=existing.category,
                                reason="superseded",
                                context=existing.context,
                            )
                    state.pending_payload = payload
                    state.waiters.append(waiter)
                    state.last_update = time.monotonic()
            state.update_event.set()
            if state.flush_task is None or state.flush_task.done():
                state.flush_task = asyncio.create_task(
                    self._flush_pending_edits(chat_id, message_id, state)
                )

    async def _flush_pending_edits(
        self,
        chat_id: int,
        message_id: int,
        state: _PendingEditState,
    ) -> None:
        """Flush queued edits once the coalescing window expires."""

        key = (int(chat_id), int(message_id))
        while True:
            payload: Optional[_EditPayload] = None
            waiters: List[_EditWaiter] = []
            wait_timeout: Optional[float] = None
            notify_delete: List["asyncio.Future[bool]"] = []
            should_exit = False

            async with state.guard:
                if state.pending_payload is None:
                    should_exit = True
                    if state.delete_waiters:
                        notify_delete = list(state.delete_waiters)
                        state.delete_waiters.clear()
                    state.flush_task = None
                    state.update_event.clear()
                else:
                    if state.waiters:
                        retained_waiters: List[_EditWaiter] = []
                        for existing in state.waiters:
                            if existing.superseded:
                                if not existing.future.done():
                                    existing.future.set_result(message_id)
                            else:
                                retained_waiters.append(existing)
                        state.waiters = retained_waiters

                    now = time.monotonic()
                    idle = now - state.last_update
                    total = now - state.first_update
                    if idle < self._COALESCE_IDLE and total < self._COALESCE_WINDOW:
                        wait_timeout = min(
                            self._COALESCE_IDLE - idle,
                            self._COALESCE_WINDOW - total,
                        )
                        state.update_event.clear()
                    else:
                        payload = state.pending_payload
                        waiters = list(state.waiters)
                        state.pending_payload = None
                        state.waiters = []
                        state.first_update = 0.0
                        state.last_update = 0.0
                        state.update_event.clear()

            if should_exit:
                for future in notify_delete:
                    if not future.done():
                        future.set_result(True)
                return

            if wait_timeout is not None:
                try:
                    await asyncio.wait_for(state.update_event.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue
                else:
                    continue

            if payload is None:
                # Nothing ready yet; loop back to evaluate again.
                await asyncio.sleep(self._COALESCE_IDLE)
                continue

            try:
                result = await self._apply_edit(chat_id, message_id, payload)
            except Exception as exc:
                for waiter in waiters:
                    if not waiter.future.done():
                        waiter.future.set_exception(exc)
                raise
            else:
                for waiter in waiters:
                    if not waiter.future.done():
                        waiter.future.set_result(result)
            finally:
                timestamp = now_utc()
                self._last_edit_timestamp[key] = timestamp
                async with state.guard:
                    state.last_flush = time.monotonic()
                    if state.pending_payload is None and not state.waiters:
                        if state.delete_waiters:
                            notify_delete = list(state.delete_waiters)
                            state.delete_waiters.clear()
                        else:
                            notify_delete = []
                    else:
                        notify_delete = []
            for future in notify_delete:
                if not future.done():
                    future.set_result(True)

    async def _apply_edit(
        self,
        chat_id: int,
        message_id: int,
        payload: _EditPayload,
    ) -> Optional[int]:
        """Execute a single editMessageText call with deduplication."""

        content_hash = payload.content_hash
        if not payload.force and await self._should_skip(chat_id, message_id, content_hash):
            await self._log_skip(
                chat_id=chat_id,
                message_id=message_id,
                category=payload.request_category,
                reason="hash_match",
                context=payload.context,
            )
            return message_id

        if not await self._consume_budget(
            method="editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            category=payload.request_category,
        ):
            return message_id

        lock = await self._acquire_lock(chat_id, message_id)
        async with lock:
            if not payload.force and await self._should_skip(chat_id, message_id, content_hash):
                await self._log_skip(
                    chat_id=chat_id,
                    message_id=message_id,
                    category=payload.request_category,
                    reason="hash_match",
                    context=payload.context,
                )
                return message_id

            if await self._was_marked_deleted(message_id):
                await self._log_skip(
                    chat_id=chat_id,
                    message_id=message_id,
                    category=payload.request_category,
                    reason="deleted_before_send",
                    context=payload.context,
                )
                return message_id

            text_hash: Optional[str] = None
            if payload.text is not None:
                text_hash = hashlib.md5(payload.text.encode("utf-8")).hexdigest()

            if not payload.force and text_hash is not None:
                last_hash = await self._last_known_text_hash(message_id)
                if last_hash is not None and last_hash == text_hash:
                    await self._log_skip(
                        chat_id=chat_id,
                        message_id=message_id,
                        category=payload.request_category,
                        reason="text_hash_match",
                        context=payload.context,
                    )
                    return message_id

            try:
                await self._throttle_send(
                    chat_id,
                    method="editMessageText",
                    context=payload.context,
                )
                trace_telegram_api_call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=payload.text,
                    reply_markup=payload.reply_markup,
                )
                result = await self._call_with_retry(
                    chat_id=chat_id,
                    message_id=message_id,
                    method="editMessageText",
                    call=lambda: self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=payload.text,
                        reply_markup=payload.reply_markup,
                        **payload.params,
                    ),
                    throttle=lambda: self._throttle_send(
                        chat_id,
                        method="editMessageText",
                        context=payload.context,
                    ),
                    context=payload.context,
                )
                self._register_send_time(chat_id)
                await self._clear_edit_failure(chat_id, message_id)
            except Exception as exc:  # pragma: no cover - exception path
                handled = await self._handle_bad_request(
                    exc,
                    chat_id=chat_id,
                    message_id=message_id,
                    content_hash=content_hash,
                    category=payload.request_category,
                    context=payload.context,
                )
                if handled is not None:
                    return handled
                self._log_event(
                    "API_ERROR",
                    level=logging.ERROR,
                    context=self._merge_context(
                        payload.context,
                        error_type=type(exc).__name__,
                        category=payload.request_category,
                    ),
                    include_debug_trace=True,
                )
                raise

        await self._remember_content(chat_id, message_id, content_hash)
        if text_hash is not None:
            await self._set_last_text_hash(message_id, text_hash)
        self._log_api_call(
            "editMessageText",
            context=self._merge_context(
                payload.context,
                message_id=message_id,
                content_hash=content_hash,
            ),
        )

        if hasattr(result, "message_id"):
            return result.message_id  # type: ignore[return-value]
        if isinstance(result, int):
            return result
        return message_id

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: Any = None,
        force: bool = False,
        request_category: RequestCategory = RequestCategory.INLINE,
        context: Optional[Mapping[str, Any]] = None,
        **params: Any,
    ) -> bool:
        """Edit only the reply markup for a message if it changed."""

        if message_id is None:
            return False

        content_hash = self._content_hash(None, reply_markup)
        telegram_method = "editMessageReplyMarkup"
        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=message_id,
            category=request_category,
            method=telegram_method,
        )
        if not force and await self._should_skip(chat_id, message_id, content_hash):
            await self._log_skip(
                chat_id=chat_id,
                message_id=message_id,
                category=request_category,
                reason="hash_match",
                context=base_context,
            )
            return True

        if not await self._consume_budget(
            method=telegram_method,
            chat_id=chat_id,
            message_id=message_id,
            category=request_category,
        ):
            return True

        lock = await self._acquire_lock(chat_id, message_id)
        async with lock:
            if not force and await self._should_skip(chat_id, message_id, content_hash):
                await self._log_skip(
                    chat_id=chat_id,
                    message_id=message_id,
                    category=request_category,
                    reason="hash_match",
                    context=base_context,
                )
                return True

            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                    **params,
                )
                await self._clear_edit_failure(chat_id, message_id)
            except Exception as exc:  # pragma: no cover - exception path
                handled = await self._handle_bad_request(
                    exc,
                    chat_id=chat_id,
                    message_id=message_id,
                    content_hash=content_hash,
                    category=request_category,
                    context=base_context,
                )
                if handled is not None:
                    return bool(handled)
                self._log_event(
                    "API_ERROR",
                    level=logging.ERROR,
                    context=self._merge_context(
                        base_context,
                        error_type=type(exc).__name__,
                    ),
                    include_debug_trace=True,
                )
                raise

            await self._remember_content(chat_id, message_id, content_hash)
            self._log_api_call(
                telegram_method,
                context=self._merge_context(
                    base_context,
                    content_hash=content_hash,
                ),
            )
            return True

    async def delete_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        request_category: RequestCategory = RequestCategory.DELETE,
        context: Optional[Mapping[str, Any]] = None,
        **params: Any,
    ) -> bool:
        """Delete a message and clear its cached hash."""

        if message_id is None:
            return False

        telegram_method = "deleteMessage"
        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=message_id,
            category=request_category,
            method=telegram_method,
        )

        await self._mark_message_deleted(message_id)

        key = (int(chat_id), int(message_id))
        state = self._pending_edits.get(key)
        if state is not None:
            loop = asyncio.get_running_loop()
            waiter: "asyncio.Future[bool]" = loop.create_future()
            async with state.guard:
                if state.pending_payload is None and not state.waiters:
                    waiter.set_result(True)
                else:
                    state.delete_waiters.append(waiter)
                    state.update_event.set()
            if not waiter.done():
                await waiter

        if not await self._consume_budget(
            method=telegram_method,
            chat_id=chat_id,
            message_id=message_id,
            category=request_category,
        ):
            return False

        lock = await self._acquire_lock(chat_id, message_id)
        async with lock:
            result: Any = False
            deletion_successful = False
            try:
                trace_telegram_api_call(
                    telegram_method,
                    chat_id=chat_id,
                    message_id=message_id,
                )
                result = await self._bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    **params,
                )
                deletion_successful = bool(result)
            finally:
                await self._forget_content(chat_id, message_id)
                if deletion_successful:
                    await self._pop_last_text_hash(message_id)
                else:
                    await self._unmark_message_deleted(message_id)

        state = self._pending_edits.pop(key, None)
        if state is not None:
            if state.flush_task is not None and not state.flush_task.done():
                state.flush_task.cancel()
            state.pending_payload = None
            state.waiters.clear()
            state.update_event.set()
            for future in state.delete_waiters:
                if not future.done():
                    future.set_result(True)
            state.delete_waiters.clear()
            self._log_event(
                "PURGE_PENDING_EDITS",
                context=self._merge_context(
                    base_context,
                    category="cleanup",
                ),
                include_debug_trace=True,
            )
        self._last_edit_timestamp.pop(key, None)

        self._log_api_call(
            telegram_method,
            context=self._merge_context(
                base_context,
                content_hash="-",
            ),
        )

        await self._clear_edit_failure(chat_id, message_id)
        return bool(result)

    async def last_edit_timestamp(
        self, chat_id: int, message_id: int
    ) -> Optional[datetime.datetime]:
        """Return when the given message was last edited."""

        key = (int(chat_id), int(message_id))
        return self._last_edit_timestamp.get(key)

    async def remember_payload(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: Optional[str],
        reply_markup: Any,
    ) -> None:
        """Public helper for registering existing message content.

        Some parts of the application create messages outside of the service
        and later need deduplication for subsequent edits.  They can call this
        method to seed the content cache with the current state of the message.
        """

        if message_id is None:
            return
        await self._remember_content(
            chat_id,
            message_id,
            self._content_hash(text, reply_markup),
        )

    async def _should_skip(
        self,
        chat_id: int,
        message_id: int,
        content_hash: str,
    ) -> bool:
        cached = await self._get_cached_hash(chat_id, message_id, content_hash)
        return cached

    async def _handle_bad_request(
        self,
        exc: Exception,
        *,
        chat_id: int,
        message_id: int,
        content_hash: str,
        category: RequestCategory,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Optional[int]:
        if not self._is_bad_request(exc):
            return None

        message = self._normalise_exception_message(exc)
        base_context = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=message_id,
            category=category,
            error_type=type(exc).__name__,
            normalized_error=message,
        )
        await self._record_edit_failure(chat_id, message_id, message)
        if "message is not modified" in message:
            await self._remember_content(chat_id, message_id, content_hash)
            await self._log_skip(
                chat_id=chat_id,
                message_id=message_id,
                category=category,
                reason="hash_match",
                context=base_context,
            )
            return message_id

        if "message to edit not found" in message or "message can't be edited" in message:
            await self._forget_content(chat_id, message_id)
            await self._mark_message_deleted(message_id)
            self._log_event(
                "EDIT_FAILED",
                level=logging.WARNING,
                context=base_context,
                include_debug_trace=True,
            )
            return None

        if "message identifier is not specified" in message:
            return None

        return None

    @staticmethod
    def _is_bad_request(exc: Exception) -> bool:
        if TelegramBadRequest is not None and isinstance(exc, TelegramBadRequest):
            return True
        if PTBBadRequest is not None and isinstance(exc, PTBBadRequest):
            return True
        return False

    @staticmethod
    def _normalise_exception_message(exc: Exception) -> str:
        message = getattr(exc, "message", None)
        if not message:
            message = str(exc)
        return message.lower()

    async def _acquire_lock(self, chat_id: int, message_id: int) -> asyncio.Lock:
        key = (int(chat_id), int(message_id))
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def _remember_content(self, chat_id: int, message_id: int, content_hash: str) -> None:
        if message_id is None:
            return
        key = (int(chat_id), int(message_id), content_hash)
        async with self._cache_lock:
            self._content_cache[key] = True

    async def _forget_content(self, chat_id: int, message_id: int) -> None:
        prefix = (int(chat_id), int(message_id))
        async with self._cache_lock:
            keys_to_remove = [key for key in self._content_cache if key[:2] == prefix]
            for key in keys_to_remove:
                self._content_cache.pop(key, None)

    async def _get_cached_hash(
        self, chat_id: int, message_id: int, content_hash: str
    ) -> Optional[bool]:
        key = (int(chat_id), int(message_id), content_hash)
        async with self._cache_lock:
            return self._content_cache.get(key)

    async def _was_marked_deleted(self, message_id: int) -> bool:
        if self._deleted_messages_ref is None:
            return False
        target = int(message_id)
        lock = self._deleted_messages_lock
        if lock is None:
            return target in self._deleted_messages_ref
        async with lock:
            return target in self._deleted_messages_ref

    async def _mark_message_deleted(self, message_id: int) -> None:
        if self._deleted_messages_ref is None:
            return
        target = int(message_id)
        lock = self._deleted_messages_lock
        if lock is None:
            self._deleted_messages_ref.add(target)
            return
        async with lock:
            self._deleted_messages_ref.add(target)

    async def _unmark_message_deleted(self, message_id: int) -> None:
        if self._deleted_messages_ref is None:
            return
        target = int(message_id)
        lock = self._deleted_messages_lock
        if lock is None:
            self._deleted_messages_ref.discard(target)
            return
        async with lock:
            self._deleted_messages_ref.discard(target)

    async def _last_known_text_hash(self, message_id: int) -> Optional[str]:
        if self._last_message_hash_ref is None:
            return None
        target = int(message_id)
        lock = self._last_message_hash_lock
        if lock is None:
            return self._last_message_hash_ref.get(target)
        async with lock:
            return self._last_message_hash_ref.get(target)

    async def _set_last_text_hash(self, message_id: int, value: str) -> None:
        if self._last_message_hash_ref is None:
            return
        target = int(message_id)
        lock = self._last_message_hash_lock
        if lock is None:
            self._last_message_hash_ref[target] = value
            return
        async with lock:
            self._last_message_hash_ref[target] = value

    async def _pop_last_text_hash(self, message_id: int) -> None:
        if self._last_message_hash_ref is None:
            return
        target = int(message_id)
        lock = self._last_message_hash_lock
        if lock is None:
            self._last_message_hash_ref.pop(target, None)
            return
        async with lock:
            self._last_message_hash_ref.pop(target, None)

    async def _log_skip(
        self,
        *,
        chat_id: int,
        message_id: Optional[int],
        category: RequestCategory,
        reason: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload = self._merge_context(
            context,
            chat_id=chat_id,
            message_id=message_id,
            category=category,
            reason=reason,
        )
        self._log_event(
            "SKIP",
            context=payload,
            include_debug_trace=True,
        )
        if self._metrics is not None and message_id is not None:
            await self._metrics.record_skip(
                chat_id=chat_id,
                category=category,
            )

    @staticmethod
    def _content_hash(text: Optional[str], reply_markup: Any) -> str:
        serialized_markup = MessagingService._serialize_markup(reply_markup)
        text_component = text or ""
        payload = json.dumps(
            {"text": text_component, "reply_markup": serialized_markup},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_markup(markup: Any) -> Any:
        if markup is None:
            return None
        for attr in ("model_dump", "to_python", "to_dict"):
            serializer = getattr(markup, attr, None)
            if callable(serializer):
                try:
                    return serializer()
                except TypeError:
                    continue
        try:
            return json.loads(markup.model_dump_json())  # type: ignore[attr-defined]
        except Exception:
            pass
        if isinstance(markup, dict):
            return markup
        if isinstance(markup, (list, tuple)):
            return list(markup)
        return repr(markup)

    def _log_api_call(
        self,
        method: str,
        *,
        context: Mapping[str, Any],
    ) -> None:
        payload = self._merge_context(context, method=method)
        self._log_event(
            "API_CALL",
            context=payload,
            include_debug_trace=True,
        )

    async def _record_edit_failure(
        self, chat_id: int, message_id: int, message: str
    ) -> None:
        key = (int(chat_id), int(message_id))
        async with self._last_edit_failure_lock:
            self._last_edit_failures[key] = message

    async def _clear_edit_failure(self, chat_id: int, message_id: int) -> None:
        key = (int(chat_id), int(message_id))
        async with self._last_edit_failure_lock:
            self._last_edit_failures.pop(key, None)

    async def get_last_edit_error(self, chat_id: int, message_id: int) -> Optional[str]:
        key = (int(chat_id), int(message_id))
        async with self._last_edit_failure_lock:
            return self._last_edit_failures.get(key)

    async def _consume_budget(
        self,
        *,
        method: str,
        chat_id: int,
        message_id: Optional[int],
        category: RequestCategory,
    ) -> bool:
        if self._metrics is None:
            return True
        allowed = await self._metrics.consume(
            chat_id=chat_id,
            method=method,
            category=category,
            message_id=message_id,
        )
        if not allowed:
            self._logger.debug(
                "Skipping %s due to exhausted request budget",
                method,
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "category": category.value,
                },
            )
        return allowed


__all__ = ["MessagingService"]

