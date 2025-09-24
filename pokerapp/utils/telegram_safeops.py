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
from typing import Any, Awaitable, Callable, Optional, TypeVar

from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

from pokerapp.entities import ChatId, MessageId
from pokerapp.utils.request_metrics import RequestCategory

T = TypeVar("T")


class TelegramSafeOps:
    """Execute Telegram operations with retry and structured logging."""

    _MIN_DELAY = 0.05

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

    async def edit_message_text(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        *,
        reply_markup: Optional[Any] = None,
        parse_mode: str = ParseMode.MARKDOWN,
        log_context: Optional[str] = None,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        """Safely edit a message, retrying transient failures when required."""

        if not message_id:
            return await self._send_message_return_id(
                chat_id,
                text,
                reply_markup=reply_markup,
                request_category=request_category,
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
                ),
            )
        except BadRequest as exc:
            self._log_bad_request(chat_id, message_id, text, log_context, exc)
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
                ),
            )
            raise
        else:
            if result:
                return result

        new_id = await self._send_message_return_id(
            chat_id,
            text,
            reply_markup=reply_markup,
            request_category=request_category,
        )

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
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.debug(
                    "Failed to delete message after replacement",
                    extra=self._build_extra(
                        chat_id=chat_id,
                        message_id=message_id,
                        operation="delete_message",
                        error_type=type(exc).__name__,
                    ),
                )

            self._logger.info(
                "Sent replacement message after edit failure",
                extra=self._build_extra(
                    chat_id=chat_id,
                    message_id=message_id,
                    operation="edit_message_text",
                    new_message_id=new_id,
                ),
            )

        return new_id

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
        )

    async def _execute(
        self,
        *,
        operation: str,
        call: Callable[[], Awaitable[T]],
        chat_id: Optional[ChatId],
        message_id: Optional[MessageId],
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
                    ),
                )
                await asyncio.sleep(delay)
                delay = min(delay * self._multiplier, self._max_delay)
                continue

    def _build_extra(
        self,
        *,
        chat_id: Optional[ChatId],
        message_id: Optional[MessageId],
        operation: str,
        context: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "operation": operation,
        }
        if context is not None:
            extra["context"] = context
        extra.update(kwargs)
        return extra

    def _log_bad_request(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        context: Optional[str],
        exc: BadRequest,
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
            ),
        )

