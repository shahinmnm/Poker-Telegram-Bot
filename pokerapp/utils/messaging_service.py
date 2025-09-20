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
from typing import Any, Dict, Optional, Tuple

from cachetools import TTLCache

from pokerapp.utils.debug_trace import trace_telegram_api_call

try:  # pragma: no cover - aiogram is optional at runtime
    from aiogram.exceptions import TelegramBadRequest
except Exception:  # pragma: no cover - fallback for PTB-only deployments
    TelegramBadRequest = None  # type: ignore[assignment]

try:  # pragma: no cover - python-telegram-bot is optional during testing
    from telegram.error import BadRequest as PTBBadRequest
except Exception:  # pragma: no cover
    PTBBadRequest = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


CacheKey = Tuple[int, int]
CacheEntryKey = Tuple[int, int, str]


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

    def __init__(
        self,
        bot: Any,
        *,
        cache_ttl: int = 3,
        cache_maxsize: int = 500,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._bot = bot
        self._logger = logger_ or logger.getChild("service")
        self._content_cache: TTLCache[CacheEntryKey, bool] = TTLCache(
            maxsize=cache_maxsize,
            ttl=cache_ttl,
        )
        self._cache_lock = asyncio.Lock()
        self._locks: Dict[CacheKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def send_message(
        self,
        *,
        chat_id: int,
        text: Optional[str],
        reply_markup: Any = None,
        **params: Any,
    ) -> Any:
        """Send a Telegram message and register its content hash.

        The method acquires a per-chat lock so that concurrent sends to the
        same chat remain ordered.  Once the Telegram API call succeeds the new
        ``message_id`` and its content hash are recorded, allowing future edits
        to be deduplicated.
        """

        lock = await self._acquire_lock(chat_id, 0)
        async with lock:
            trace_telegram_api_call(
                "sendMessage",
                chat_id=chat_id,
                message_id=None,
                text=text,
                reply_markup=reply_markup,
            )
            result = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                **params,
            )

            message_id = getattr(result, "message_id", None)
            if message_id is not None:
                content_hash = self._content_hash(text, reply_markup)
                await self._remember_content(chat_id, message_id, content_hash)
                self._log_api_call(
                    "send_message",
                    chat_id=chat_id,
                    message_id=message_id,
                    content_hash=content_hash,
                )
            else:
                self._log_api_call(
                    "send_message",
                    chat_id=chat_id,
                    message_id=None,
                    content_hash=self._content_hash(text, reply_markup),
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
        **params: Any,
    ) -> Optional[int]:
        """Edit an existing Telegram message while avoiding duplicate edits."""

        if message_id is None:
            return None

        content_hash = self._content_hash(text, reply_markup)
        if not force and await self._should_skip(chat_id, message_id, content_hash):
            self._logger.info(
                "SKIP EDIT: identical content for chat %s, msg %s",
                chat_id,
                message_id,
            )
            return message_id

        lock = await self._acquire_lock(chat_id, message_id)
        async with lock:
            if not force and await self._should_skip(chat_id, message_id, content_hash):
                self._logger.info(
                    "SKIP EDIT: identical content for chat %s, msg %s",
                    chat_id,
                    message_id,
                )
                return message_id

            try:
                trace_telegram_api_call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
                result = await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    **params,
                )
            except Exception as exc:  # pragma: no cover - exception path
                handled = await self._handle_bad_request(
                    exc, chat_id=chat_id, message_id=message_id, content_hash=content_hash
                )
                if handled is not None:
                    return handled
                raise

            await self._remember_content(chat_id, message_id, content_hash)
            self._log_api_call(
                "edit_message_text",
                chat_id=chat_id,
                message_id=message_id,
                content_hash=content_hash,
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
        **params: Any,
    ) -> bool:
        """Edit only the reply markup for a message if it changed."""

        if message_id is None:
            return False

        content_hash = self._content_hash(None, reply_markup)
        if not force and await self._should_skip(chat_id, message_id, content_hash):
            self._logger.info(
                "SKIP EDIT: identical content for chat %s, msg %s",
                chat_id,
                message_id,
            )
            return True

        lock = await self._acquire_lock(chat_id, message_id)
        async with lock:
            if not force and await self._should_skip(chat_id, message_id, content_hash):
                self._logger.info(
                    "SKIP EDIT: identical content for chat %s, msg %s",
                    chat_id,
                    message_id,
                )
                return True

            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                    **params,
                )
            except Exception as exc:  # pragma: no cover - exception path
                handled = await self._handle_bad_request(
                    exc, chat_id=chat_id, message_id=message_id, content_hash=content_hash
                )
                if handled is not None:
                    return bool(handled)
                raise

            await self._remember_content(chat_id, message_id, content_hash)
            self._log_api_call(
                "edit_message_reply_markup",
                chat_id=chat_id,
                message_id=message_id,
                content_hash=content_hash,
            )
            return True

    async def delete_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        **params: Any,
    ) -> bool:
        """Delete a message and clear its cached hash."""

        if message_id is None:
            return False

        lock = await self._acquire_lock(chat_id, message_id)
        async with lock:
            result: Any = False
            try:
                trace_telegram_api_call(
                    "deleteMessage",
                    chat_id=chat_id,
                    message_id=message_id,
                )
                result = await self._bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    **params,
                )
            finally:
                await self._forget_content(chat_id, message_id)

        self._log_api_call(
            "delete_message",
            chat_id=chat_id,
            message_id=message_id,
            content_hash="-",
        )

        return bool(result)

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
    ) -> Optional[int]:
        if not self._is_bad_request(exc):
            return None

        message = self._normalise_exception_message(exc)
        if "message is not modified" in message:
            await self._remember_content(chat_id, message_id, content_hash)
            self._logger.info(
                "SKIP EDIT: identical content for chat %s, msg %s",
                chat_id,
                message_id,
            )
            return message_id

        if "message to edit not found" in message or "message can't be edited" in message:
            await self._forget_content(chat_id, message_id)
            self._logger.warning(
                "EDIT FAILED: message missing or not editable for chat %s, msg %s",
                chat_id,
                message_id,
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
        chat_id: int,
        message_id: Optional[int],
        content_hash: str,
    ) -> None:
        self._logger.info(
            "API CALL: %s",
            method,
            extra={
                "chat_id": chat_id,
                "message_id": message_id,
                "content_hash": content_hash,
            },
        )


__all__ = ["MessagingService"]

