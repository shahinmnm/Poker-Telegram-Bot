"""Aiogram-inspired middlewares reused inside the PTB stack."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram.dispatcher.middlewares.base import BaseMiddleware

from pokerapp.utils.cache import MessagePayload, MessageStateCache


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MessageEditEvent:
    chat_id: int
    message_id: int
    text: Optional[str]
    reply_markup: Any
    markup_hash: Optional[str]
    parse_mode: Optional[str]
    context: str
    disable_web_page_preview: bool


class MessageDiffMiddleware(BaseMiddleware):
    """Skip Telegram edits when the payload is unchanged."""

    def __init__(
        self,
        cache: MessageStateCache,
        *,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__()
        self._cache = cache
        self._logger = logger_ or logger.getChild("diff")

    async def run(
        self,
        handler: Callable[[MessageEditEvent], Awaitable[Optional[int]]],
        event: MessageEditEvent,
        *,
        force: bool = False,
        skip_cache_check: bool = False,
    ) -> Optional[int]:
        return await self._execute(
            handler,
            event,
            force=force,
            skip_cache_check=skip_cache_check,
        )

    async def __call__(
        self,
        handler: Callable[[MessageEditEvent], Awaitable[Optional[int]]],
        event: MessageEditEvent,
        data: Dict[str, Any],
    ) -> Optional[int]:
        return await self._execute(
            handler,
            event,
            force=data.get("force", False),
            skip_cache_check=data.get("skip_cache_check", False),
        )

    async def _execute(
        self,
        handler: Callable[[MessageEditEvent], Awaitable[Optional[int]]],
        event: MessageEditEvent,
        *,
        force: bool,
        skip_cache_check: bool,
    ) -> Optional[int]:
        payload = MessagePayload(
            text=event.text,
            markup_hash=event.markup_hash,
            parse_mode=event.parse_mode,
        )
        if not force and not skip_cache_check:
            if await self._cache.matches(event.chat_id, event.message_id, payload):
                self._logger.debug(
                    "Skipping edit due to identical payload",
                    extra={
                        "chat_id": event.chat_id,
                        "message_id": event.message_id,
                        "context": event.context,
                    },
                )
                return event.message_id
        result = await handler(event)
        if result is not None:
            await self._cache.update(event.chat_id, result, payload)
        return result
