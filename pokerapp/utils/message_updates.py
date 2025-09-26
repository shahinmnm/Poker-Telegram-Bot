"""Helpers for safe Telegram message updates within the poker bot."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from pokerapp.utils.cache import MessagePayload

logger = logging.getLogger(__name__)


async def safe_edit_message(
    messaging_service: Any,
    *,
    chat_id: int,
    message_id: Optional[int],
    text: Optional[str],
    reply_markup: Optional[Any] = None,
    current_game_id: Optional[str] = None,
    **params: Any,
):
    """Edit a Telegram message only when the payload has changed."""

    if message_id is None:
        return None

    params = dict(params)
    force = bool(params.pop("force", False))

    safe_method = getattr(messaging_service, "safe_edit_message", None)
    if callable(safe_method):
        return await safe_method(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            force=force,
            current_game_id=current_game_id,
            **params,
        )

    cache = getattr(messaging_service, "message_state_cache", None)
    service_logger: logging.Logger = getattr(
        messaging_service, "_logger", logger
    )

    markup_hash: Optional[str] = None
    if reply_markup is not None:
        try:
            serialize = getattr(messaging_service, "_serialize_markup", None)
            if callable(serialize):
                serialized_markup = serialize(reply_markup)
            else:
                from pokerapp.utils.messaging_service import MessagingService

                serialized_markup = MessagingService._serialize_markup(reply_markup)
            markup_payload = json.dumps(
                serialized_markup,
                sort_keys=True,
                ensure_ascii=False,
            )
            markup_hash = hashlib.md5(markup_payload.encode("utf-8")).hexdigest()
        except Exception:
            service_logger.debug(
                "Failed to serialize reply markup for hashing; proceeding without hash",
                exc_info=True,
            )
            markup_hash = None

    payload = MessagePayload(
        text=text,
        markup_hash=markup_hash,
        parse_mode=params.get("parse_mode"),
    )

    if (
        not force
        and cache is not None
        and hasattr(cache, "matches")
        and callable(getattr(cache, "matches"))
    ):
        try:
            matches = await cache.matches(chat_id, message_id, payload)
        except Exception:
            service_logger.debug(
                "MessageStateCache check failed; falling back to direct edit",
                exc_info=True,
            )
        else:
            if matches:
                service_logger.debug(
                    "Safe edit skipped: identical content for chat_id=%s, message_id=%s",
                    chat_id,
                    message_id,
                )
                return None
    if cache is not None and hasattr(cache, "update") and callable(
        getattr(cache, "update")
    ):
        try:
            await cache.update(chat_id, message_id, payload)
        except Exception:
            service_logger.debug(
                "MessageStateCache update failed; continuing with edit",
                exc_info=True,
            )
    try:
        return await messaging_service.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            force=force,
            current_game_id=current_game_id,
            **params,
        )
    except Exception:
        if cache is not None and hasattr(cache, "forget") and callable(
            getattr(cache, "forget")
        ):
            try:
                await cache.forget(chat_id, message_id)
            except Exception:
                service_logger.debug(
                    "MessageStateCache forget failed after edit exception",
                    exc_info=True,
                )
        raise
