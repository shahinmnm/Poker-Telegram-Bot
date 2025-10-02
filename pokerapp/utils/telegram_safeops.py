"""Robust wrappers around Telegram messaging operations.

This module centralises retry and backoff behaviour for Telegram API calls.
It augments the existing :class:`PokerBotViewer` helpers by retrying common
transient failures (``RetryAfter``, ``TimedOut``, ``NetworkError``) with
configurable exponential backoff while emitting structured logs for
observability.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional, TypeVar

from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

from pokerapp.entities import ChatId, MessageId
from pokerapp.utils.request_metrics import RequestCategory
from cachetools import LRUCache

T = TypeVar("T")


class TelegramSafeOps:
    """Execute Telegram operations with retry and structured logging."""

    _MIN_DELAY = 0.05

    _last_edit_cache: dict[tuple[ChatId, MessageId], str] = {}
    _cache_lock = asyncio.Lock()

    def __init__(
        self,
        view: Any,
        *,
        logger: logging.Logger,
        max_retries: int,
        base_delay: float,
        max_delay: float,
        backoff_multiplier: float,
    ) -> None:
        if view is None:
            raise ValueError("view dependency must be provided")
        if logger is None:
            raise ValueError("logger dependency must be provided")

        self._view = view
        self._logger = logger
        self._max_retries = max(0, int(max_retries))
        self._base_delay = max(float(base_delay), self._MIN_DELAY)
        self._max_delay = max(float(max_delay), self._base_delay)
        self._multiplier = max(float(backoff_multiplier), 1.0)
        self._edit_throttle: LRUCache[tuple[ChatId, MessageId], float] = LRUCache(
            maxsize=1024
        )
        self._throttle_lock = asyncio.Lock()

    async def edit_message_text(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        *,
        reply_markup: Optional[Any] = None,
        parse_mode: str = ParseMode.MARKDOWN,
        from_countdown: bool = False,
        log_context: Optional[str] = None,
        request_category: RequestCategory = RequestCategory.GENERAL,
        log_extra: Optional[Mapping[str, Any]] = None,
        current_game_id: Optional[str] = None,
    ) -> Optional[MessageId]:
        """Safely edit a message, retrying transient failures when required.

        Args:
            from_countdown: When ``True`` the countdown subsystem initiated the
                edit and we bypass throttling delays so the timer task cannot be
                starved behind other chat operations.  This flag is best-effort
                and preserves the existing retry and replacement behaviour.
        """

        cache_key: Optional[tuple[ChatId, MessageId]] = None

        if from_countdown:
            self._logger.debug(
                "Countdown edit bypassing message throttle",
                extra=self._build_extra(
                    chat_id=chat_id,
                    message_id=message_id,
                    operation="edit_message_text",
                    extra=log_extra,
                ),
            )

        if not message_id:
            return await self._send_message_return_id(
                chat_id,
                text,
                reply_markup=reply_markup,
                request_category=request_category,
            )

        cache_key = self._normalize_cache_key(chat_id, message_id)
        if cache_key is not None:
            async with self._cache_lock:
                cached_text = self._last_edit_cache.get(cache_key)
                if cached_text == text:
                    self._logger.debug(
                        "Skipping edit_message_text because content unchanged",
                        extra=self._build_extra(
                            chat_id=chat_id,
                            message_id=message_id,
                            operation="edit_message_text",
                            extra=log_extra,
                        ),
                    )
                    return message_id
            if not from_countdown:
                await self._apply_edit_throttle(
                    cache_key,
                    chat_id=chat_id,
                    message_id=message_id,
                )

        try:
            result = await self._execute(
                operation="edit_message_text",
                chat_id=chat_id,
                message_id=message_id,
                call=lambda: self._view.edit_message_text(  # type: ignore[misc]
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    request_category=request_category,
                    parse_mode=parse_mode,
                    suppress_exceptions=False,
                    current_game_id=current_game_id,
                ),
                log_extra=log_extra,
            )
        except BadRequest as exc:
            self._log_bad_request(
                chat_id, message_id, text, log_context, exc, extra=log_extra
            )
            result = None
        except TelegramError as exc:
            self._logger.error(
                "TelegramError when editing message; will send a replacement",
                extra=self._build_extra(
                    chat_id=chat_id,
                    message_id=message_id,
                    operation="edit_message_text",
                    context=log_context,
                    error_type=type(exc).__name__,
                    extra=log_extra,
                ),
            )
            result = None
        except Exception as exc:
            self._logger.error(
                "Unexpected error when editing message; will send a replacement",
                extra=self._build_extra(
                    chat_id=chat_id,
                    message_id=message_id,
                    operation="edit_message_text",
                    context=log_context,
                    error_type=type(exc).__name__,
                    extra=log_extra,
                ),
            )
            raise
        else:
            if result:
                if cache_key is not None:
                    async with self._cache_lock:
                        self._last_edit_cache[cache_key] = text
                    await self._touch_throttle(cache_key)
                return result

        new_id = await self._send_message_return_id(
            chat_id,
            text,
            reply_markup=reply_markup,
            request_category=request_category,
        )

        if new_id:
            new_cache_key = self._normalize_cache_key(chat_id, new_id)
            if cache_key is not None or new_cache_key is not None:
                async with self._cache_lock:
                    if cache_key is not None:
                        self._last_edit_cache.pop(cache_key, None)
                    if new_cache_key is not None:
                        self._last_edit_cache[new_cache_key] = text
                await self._update_throttle_on_replacement(cache_key, new_cache_key)

        if new_id and message_id and new_id != message_id:
            try:
                await self._execute(
                    operation="delete_message",
                    chat_id=chat_id,
                    message_id=message_id,
                    call=lambda: self._view.delete_message(  # type: ignore[misc]
                        chat_id,
                        message_id,
                        suppress_exceptions=False,
                    ),
                    log_extra=log_extra,
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.debug(
                    "Failed to delete message after replacement",
                    extra=self._build_extra(
                        chat_id=chat_id,
                        message_id=message_id,
                        operation="delete_message",
                        error_type=type(exc).__name__,
                        extra=log_extra,
                    ),
                )

            self._logger.info(
                "Sent replacement message after edit failure",
                extra=self._build_extra(
                    chat_id=chat_id,
                    message_id=message_id,
                    operation="edit_message_text",
                    new_message_id=new_id,
                    extra=log_extra,
                ),
            )

        return new_id

    def _normalize_cache_key(
        self, chat_id: ChatId, message_id: MessageId
    ) -> Optional[tuple[ChatId, MessageId]]:
        if not chat_id or not message_id:
            return None
        return (chat_id, message_id)

    async def send_message_safe(
        self,
        *,
        call: Callable[[], Awaitable[T]],
        chat_id: Optional[ChatId],
        operation: Optional[str] = None,
        log_extra: Optional[Mapping[str, Any]] = None,
    ) -> T:
        """Execute a sending coroutine with retry and structured logging."""

        op_name = operation or getattr(call, "__name__", "send_message")
        return await self._execute(
            operation=op_name,
            call=call,
            chat_id=chat_id,
            message_id=None,
            log_extra=log_extra,
        )

    async def edit_message_safe(
        self,
        *,
        call: Callable[[], Awaitable[T]],
        chat_id: Optional[ChatId],
        message_id: Optional[MessageId],
        operation: Optional[str] = None,
        log_extra: Optional[Mapping[str, Any]] = None,
    ) -> T:
        """Execute an edit coroutine with retry and structured logging."""

        op_name = operation or getattr(call, "__name__", "edit_message")
        return await self._execute(
            operation=op_name,
            call=call,
            chat_id=chat_id,
            message_id=message_id,
            log_extra=log_extra,
        )

    async def delete_message_safe(
        self,
        *,
        call: Callable[[], Awaitable[T]],
        chat_id: Optional[ChatId],
        message_id: Optional[MessageId],
        operation: Optional[str] = None,
        log_extra: Optional[Mapping[str, Any]] = None,
    ) -> T:
        """Execute a delete coroutine with retry and structured logging."""

        op_name = operation or getattr(call, "__name__", "delete_message")
        return await self._execute(
            operation=op_name,
            call=call,
            chat_id=chat_id,
            message_id=message_id,
            log_extra=log_extra,
        )

    async def _send_message_return_id(
        self,
        chat_id: ChatId,
        text: str,
        *,
        reply_markup: Optional[Any] = None,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        return await self._execute(
            operation="send_message_return_id",
            chat_id=chat_id,
            message_id=None,
            call=lambda: self._view.send_message_return_id(  # type: ignore[misc]
                chat_id,
                text,
                reply_markup=reply_markup,
                request_category=request_category,
                suppress_exceptions=False,
            ),
            log_extra=None,
        )

    async def _execute(
        self,
        *,
        operation: str,
        call: Callable[[], Awaitable[T]],
        chat_id: Optional[ChatId],
        message_id: Optional[MessageId],
        log_extra: Optional[Mapping[str, Any]] = None,
    ) -> T:
        max_attempts = self._max_retries + 1
        attempt = 0
        delay = self._base_delay

        while True:
            try:
                return await call()
            except asyncio.CancelledError:  # pragma: no cover - propagate cancellation
                raise
            except RetryAfter as exc:
                attempt += 1
                wait_time = float(getattr(exc, "retry_after", delay) or delay)
                self._logger.warning(
                    "RetryAfter received from Telegram; backing off",
                    extra=self._build_extra(
                        chat_id=chat_id,
                        message_id=message_id,
                        operation=operation,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retry_after=wait_time,
                        error_type=type(exc).__name__,
                        extra=log_extra,
                    ),
                )
                if attempt >= max_attempts:
                    self._logger.error(
                        "RetryAfter exceeded retry budget",
                        extra=self._build_extra(
                            chat_id=chat_id,
                            message_id=message_id,
                            operation=operation,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            retry_after=wait_time,
                            error_type=type(exc).__name__,
                            extra=log_extra,
                        ),
                    )
                    raise
                await asyncio.sleep(wait_time)
                delay = self._base_delay
                continue
            except (TimedOut, NetworkError) as exc:
                if isinstance(exc, BadRequest):
                    raise
                attempt += 1
                if attempt >= max_attempts:
                    self._logger.error(
                        "Telegram operation failed after retries",
                        extra=self._build_extra(
                            chat_id=chat_id,
                            message_id=message_id,
                            operation=operation,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            error_type=type(exc).__name__,
                            extra=log_extra,
                        ),
                    )
                    raise
                self._logger.warning(
                    "Transient Telegram error; retrying",
                    extra=self._build_extra(
                        chat_id=chat_id,
                        message_id=message_id,
                        operation=operation,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error_type=type(exc).__name__,
                        delay=delay,
                        extra=log_extra,
                    ),
                )
                await asyncio.sleep(delay)
                delay = min(delay * self._multiplier, self._max_delay)
                continue

    async def _apply_edit_throttle(
        self,
        key: tuple[ChatId, MessageId],
        *,
        chat_id: ChatId,
        message_id: MessageId,
    ) -> None:
        loop = asyncio.get_event_loop()
        wait_time = 0.0
        async with self._throttle_lock:
            last_edit = self._edit_throttle.get(key, 0.0)
            now = loop.time()
            elapsed = now - last_edit
            if last_edit and elapsed < 1.0:
                wait_time = 1.0 - elapsed
            if wait_time <= 0:
                self._edit_throttle[key] = now
        if wait_time > 0:
            self._logger.debug(
                "Throttling edit_message_text, waiting %.2fs",
                wait_time,
                extra=self._build_extra(
                    chat_id=chat_id,
                    message_id=message_id,
                    operation="edit_message_text",
                ),
            )
            await asyncio.sleep(wait_time)
            async with self._throttle_lock:
                self._edit_throttle[key] = loop.time()

    async def _touch_throttle(self, key: tuple[ChatId, MessageId]) -> None:
        loop = asyncio.get_event_loop()
        async with self._throttle_lock:
            self._edit_throttle[key] = loop.time()

    async def _update_throttle_on_replacement(
        self,
        old_key: Optional[tuple[ChatId, MessageId]],
        new_key: Optional[tuple[ChatId, MessageId]],
    ) -> None:
        if old_key is None and new_key is None:
            return
        loop = asyncio.get_event_loop()
        async with self._throttle_lock:
            if old_key is not None:
                self._edit_throttle.pop(old_key, None)
            if new_key is not None:
                self._edit_throttle[new_key] = loop.time()

    def _build_extra(
        self,
        *,
        chat_id: Optional[ChatId],
        message_id: Optional[MessageId],
        operation: str,
        context: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "operation": operation,
        }
        if context is not None:
            payload["context"] = context
        if extra is not None:
            payload.update(dict(extra))
        payload.update(kwargs)
        return payload

    def _log_bad_request(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        context: Optional[str],
        exc: BadRequest,
        *,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        preview = text
        max_preview_length = 120
        if len(preview) > max_preview_length:
            preview = preview[: max_preview_length - 3] + "..."
        error_message = getattr(exc, "message", None) or str(exc)
        self._logger.warning(
            "BadRequest when editing message; will send a replacement",
            extra=self._build_extra(
                chat_id=chat_id,
                message_id=message_id,
                operation="edit_message_text",
                context=context or "general",
                error_message=error_message,
                text_preview=preview,
                extra=extra,
            ),
        )

