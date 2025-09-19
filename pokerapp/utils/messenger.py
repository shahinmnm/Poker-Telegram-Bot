"""Centralized helpers for Telegram messaging with async coordination.

This module replaces a collection of legacy rate-limiting utilities with a
single, well-defined entry point for all outgoing Telegram messages.  The
``TelegramMessenger`` class exposes coroutine helpers mirroring the methods of
``telegram.Bot`` that are used throughout the bot.  Each helper performs a
lightweight deduplication check backed by :mod:`cachetools` and ensures that
only one operation per chat/category is executed at a time via
``asyncio.Lock`` instances.

The deduplication cache prevents bursts of identical edits from hitting the
Telegram API which in turn keeps the bot responsive without complicated
budget accounting.  Whenever an operation is skipped due to identical content
the caller receives the most recently known ``message_id`` (when available)
and the event is logged as ``"SKIP: identical content"`` for observability.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, Optional, Tuple

from cachetools import TTLCache
from telegram import Bot, Message
from telegram.error import BadRequest


logger = logging.getLogger(__name__)


class TelegramMessenger:
    """Async helper that serialises outgoing Telegram requests.

    Parameters
    ----------
    bot:
        The underlying :class:`telegram.Bot` instance.  All Telegram API calls
        are forwarded to this object.
    dedup_ttl:
        Number of seconds identical payloads should be remembered.  Attempts to
        send the same payload within the window are skipped to avoid flooding.
    dedup_maxsize:
        Maximum number of recent payload fingerprints that should be tracked in
        the cache.
    """

    def __init__(
        self,
        bot: Bot,
        *,
        dedup_ttl: int = 3,
        dedup_maxsize: int = 2048,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._bot = bot
        self._logger = logger_ or logger.getChild("messenger")
        self._recent_payloads: TTLCache[Tuple[int, int, str, str], bool] = TTLCache(
            maxsize=dedup_maxsize,
            ttl=dedup_ttl,
        )
        self._recent_lock = asyncio.Lock()
        self._locks: Dict[Tuple[int, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    async def send_message(
        self,
        *,
        chat_id: int,
        text: Optional[str],
        category: str = "send",
        reply_markup: Any = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: Optional[bool] = None,
        force: bool = False,
        **extra: Any,
    ) -> Optional[Message]:
        """Send a message after serialising access for the ``chat_id``.

        The method mirrors :meth:`telegram.Bot.send_message` but augments it
        with a lock to avoid concurrent sends for the same chat.  The payload is
        registered in the deduplication cache once the request succeeds.
        """

        lock = await self._get_lock(chat_id, category)
        async with lock:
            message = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                **extra,
            )
            message_id = getattr(message, "message_id", None)
            if message_id:
                await self._remember_payload(
                    chat_id,
                    message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
            return message

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: Optional[str],
        category: str = "edit_text",
        reply_markup: Any = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: Optional[bool] = None,
        force: bool = False,
        **extra: Any,
    ) -> Optional[int]:
        """Edit the text of a message if the payload changed.

        Returns the resulting ``message_id`` when the edit was performed or the
        original ``message_id`` when the edit was skipped.
        """

        if not message_id:
            return None

        key = await self._fingerprint(
            chat_id,
            message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )

        if not force and await self._was_recent(key):
            self._logger.info(
                "SKIP: identical content",
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "category": category,
                },
            )
            return message_id

        lock = await self._get_lock(chat_id, category)
        async with lock:
            if not force and await self._was_recent(key):
                self._logger.info(
                    "SKIP: identical content",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "category": category,
                    },
                )
                return message_id

            try:
                result = await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                    **extra,
                )
            except BadRequest as exc:
                message = (getattr(exc, "message", None) or str(exc or "")).lower()
                if "message is not modified" in message:
                    await self._remember_recent(key)
                    return message_id
                raise

            await self._remember_recent(key)

            if isinstance(result, Message):
                return result.message_id
            if result is True:
                return message_id
            if isinstance(result, int):
                return result
            return message_id

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: Any = None,
        category: str = "edit_reply_markup",
        force: bool = False,
        **extra: Any,
    ) -> bool:
        """Update only the reply markup when it changed."""

        if not message_id:
            return False

        key = await self._fingerprint(
            chat_id,
            message_id,
            text=None,
            reply_markup=reply_markup,
            parse_mode=None,
            disable_web_page_preview=None,
        )

        if not force and await self._was_recent(key):
            self._logger.info(
                "SKIP: identical content",
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "category": category,
                },
            )
            return True

        lock = await self._get_lock(chat_id, category)
        async with lock:
            if not force and await self._was_recent(key):
                self._logger.info(
                    "SKIP: identical content",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "category": category,
                    },
                )
                return True

            await self._bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
                **extra,
            )
            await self._remember_recent(key)
            return True

    async def remember_payload(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: Optional[str],
        reply_markup: Any,
        parse_mode: Optional[str],
        disable_web_page_preview: Optional[bool] = None,
    ) -> None:
        """Manually register a payload for deduplication purposes."""

        key = await self._fingerprint(
            chat_id,
            message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        await self._remember_recent(key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _get_lock(self, chat_id: int, category: str) -> asyncio.Lock:
        normalized_key = (int(chat_id), category)
        async with self._locks_guard:
            lock = self._locks.get(normalized_key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[normalized_key] = lock
            return lock

    async def _remember_recent(self, key: Tuple[int, int, str, str]) -> None:
        async with self._recent_lock:
            self._recent_payloads[key] = True

    async def _remember_payload(
        self,
        chat_id: int,
        message_id: int,
        *,
        text: Optional[str],
        reply_markup: Any,
        parse_mode: Optional[str],
        disable_web_page_preview: Optional[bool],
    ) -> None:
        key = await self._fingerprint(
            chat_id,
            message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        await self._remember_recent(key)

    async def _was_recent(self, key: Tuple[int, int, str, str]) -> bool:
        async with self._recent_lock:
            return key in self._recent_payloads

    async def _fingerprint(
        self,
        chat_id: int,
        message_id: int,
        *,
        text: Optional[str],
        reply_markup: Any,
        parse_mode: Optional[str],
        disable_web_page_preview: Optional[bool],
    ) -> Tuple[int, int, str, str]:
        text_hash = self._hash_text(text, parse_mode, disable_web_page_preview)
        markup_hash = self._hash_markup(reply_markup)
        return int(chat_id), int(message_id), text_hash, markup_hash

    @staticmethod
    def _hash_text(
        text: Optional[str],
        parse_mode: Optional[str],
        disable_web_page_preview: Optional[bool],
    ) -> str:
        if text is None and parse_mode is None and disable_web_page_preview is None:
            return "Ø"
        components = [text or "", parse_mode or "", str(bool(disable_web_page_preview))]
        serialized = "|".join(components)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_markup(markup: Any) -> str:
        if markup is None:
            return "Ø"
        serializer = getattr(markup, "to_dict", None)
        if callable(serializer):
            try:
                payload = serializer()
            except TypeError:
                payload = markup
        else:
            payload = markup
        try:
            serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = repr(payload)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = ["TelegramMessenger"]

