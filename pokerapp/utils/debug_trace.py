"""Helpers for deep tracing of outgoing Telegram API calls.

This module centralises the debug logging that is enabled when the
``POKERBOT_DEBUG_TRACE_MESSAGES`` environment variable is set.  The helper
inspects the current stack to determine where a Telegram API call originated
from, which game state is active and, when possible, which scheduled job or
update triggered the action.  The collected information is emitted using the
``DEBUG_TRACE`` tag so it can easily be filtered from standard logs.

The tracing utilities are intentionally defensive – any exception raised while
collecting diagnostic information is swallowed to ensure the poker bot's
runtime behaviour is unchanged when tracing is enabled.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


LOGGER = logging.getLogger("pokerbot.debug_trace")

DEBUG_TRACE_ENABLED = os.getenv("POKERBOT_DEBUG_TRACE_MESSAGES", "0") == "1"


def trace_telegram_api_call(
    method: str,
    *,
    chat_id: Optional[int],
    message_id: Optional[int] = None,
    text: Optional[str] = None,
    reply_markup: Any = None,
) -> None:
    """Emit a structured debug log for an outgoing Telegram API request.

    Parameters
    ----------
    method:
        Telegram API method name (for example ``sendMessage``).
    chat_id:
        Identifier of the target chat, when available.
    message_id:
        Identifier of the message being edited or deleted.
    text:
        Textual payload associated with the request.  For photo messages the
        caption should be provided instead.
    reply_markup:
        Any reply markup associated with the request.
    """

    if not DEBUG_TRACE_ENABLED:
        return

    try:
        stack = inspect.stack(context=0)
        origin = _find_origin_frame(stack)
        game_context = _gather_game_context(stack)
        trigger = _detect_trigger(stack)
        message_hash = _message_payload_hash(text, reply_markup)

        origin_description = _format_origin(origin)

        lines = [f"DEBUG_TRACE: {method} called"]
        if origin_description:
            lines.append("")
            lines.append(f"by: {origin_description}")

        if game_context.game_state is not None:
            lines.append("")
            lines.append(f"game_state: {game_context.game_state}")
        if game_context.turn_message_id is not None:
            lines.append(f"turn_message_id: {game_context.turn_message_id}")
        if game_context.anchor_ids:
            anchor_ids = ", ".join(str(anchor) for anchor in game_context.anchor_ids)
            lines.append(f"anchor_ids: [{anchor_ids}]")

        if trigger:
            lines.append("")
            lines.append(f"triggered_by: {trigger}")

        lines.append("")
        if chat_id is not None:
            lines.append(f"chat_id: {chat_id}")
        if message_id is not None:
            lines.append(f"message_id: {message_id}")
        lines.append(f"message_text_hash: {message_hash}")

        LOGGER.info("\n".join(lines))
    except Exception:  # pragma: no cover - defensive logging
        LOGGER.exception("Failed to produce debug trace for Telegram API call")
    finally:
        # Break potential reference cycles created by ``inspect.stack``
        del stack


def _format_origin(frame_info: Optional[inspect.FrameInfo]) -> Optional[str]:
    if frame_info is None:
        return None

    module_name = frame_info.frame.f_globals.get("__name__", "<unknown>")
    filename = Path(frame_info.filename).name
    func_name = frame_info.function
    self_obj = frame_info.frame.f_locals.get("self")
    if self_obj is not None:
        class_name = type(self_obj).__name__
        func_name = f"{class_name}.{func_name}"
    return f"{module_name}.{func_name}() @ {filename}:{frame_info.lineno}"


def _find_origin_frame(stack: Sequence[inspect.FrameInfo]) -> Optional[inspect.FrameInfo]:
    skip_modules = {
        "pokerapp.utils.debug_trace",
        "pokerapp.utils.messaging_service",
    }
    skip_class_names = {"MessagingService", "RequestManager"}

    for frame_info in stack[1:]:
        module_name = frame_info.frame.f_globals.get("__name__", "")
        if any(module_name.startswith(prefix) for prefix in skip_modules):
            continue
        self_obj = frame_info.frame.f_locals.get("self")
        if self_obj is not None and type(self_obj).__name__ in skip_class_names:
            continue
        return frame_info
    return None


def _gather_game_context(stack: Sequence[inspect.FrameInfo]) -> "_GameContext":
    context = _GameContext()

    for frame_info in stack:
        locals_ = frame_info.frame.f_locals
        for candidate in _iter_context_candidates(locals_):
            context.absorb(candidate)
        if context.is_complete:
            break

    return context


def _iter_context_candidates(locals_: Dict[str, Any]) -> Iterable[Any]:
    keys = (
        "self",
        "game",
        "orchestrator",
        "table",
        "manager",
    )
    for key in keys:
        obj = locals_.get(key)
        if obj is not None:
            yield obj


def _detect_trigger(stack: Sequence[inspect.FrameInfo]) -> Optional[str]:
    for frame_info in stack:
        locals_ = frame_info.frame.f_locals

        update = locals_.get("update")
        trigger = _describe_update_like(update)
        if trigger:
            return trigger

        callback = locals_.get("callback_query")
        trigger = _describe_callback(callback)
        if trigger:
            return trigger

        context = locals_.get("context")
        trigger = _describe_job_context(context)
        if trigger:
            return trigger

        job = locals_.get("job")
        trigger = _describe_job(job)
        if trigger:
            return trigger

    return None


def _describe_update_like(update: Any) -> Optional[str]:
    if update is None:
        return None
    try:
        callback_query = getattr(update, "callback_query", None)
        trigger = _describe_callback(callback_query)
        if trigger:
            return trigger

        message = getattr(update, "message", None)
        if message is not None:
            user = getattr(message, "from_user", None) or getattr(message, "from", None)
            user_id = getattr(user, "id", None)
            if user_id is not None:
                return f"message from user_id={user_id}"

        effective_user = getattr(update, "effective_user", None)
        if effective_user is not None:
            user_id = getattr(effective_user, "id", None)
            if user_id is not None:
                event_type = getattr(update, "event_type", None)
                if event_type:
                    return f"{event_type} from user_id={user_id}"
                return f"update from user_id={user_id}"

        event_type = getattr(update, "event_type", None)
        if event_type:
            return str(event_type)
    except Exception:
        return None
    return None


def _describe_callback(callback: Any) -> Optional[str]:
    if callback is None:
        return None
    try:
        from_user = getattr(callback, "from_user", None) or getattr(callback, "from", None)
        user_id = getattr(from_user, "id", None)
        if user_id is not None:
            return f"callback_query from user_id={user_id}"
        data = getattr(callback, "data", None)
        if data is not None:
            return f"callback_query data={data!r}"
    except Exception:
        return None
    return None


def _describe_job_context(context: Any) -> Optional[str]:
    if context is None:
        return None
    job = getattr(context, "job", None)
    return _describe_job(job)


def _describe_job(job: Any) -> Optional[str]:
    if job is None:
        return None
    try:
        name = getattr(job, "name", None)
        if name:
            return f"job {name}"
        callback = getattr(job, "callback", None)
        if callback:
            return f"job callback={callback!r}"
    except Exception:
        return None
    return None


def _message_payload_hash(text: Optional[str], reply_markup: Any) -> str:
    if text is None and reply_markup is None:
        return "-"
    try:
        payload = {
            "text": text or "",
            "reply_markup": _serialize_markup(reply_markup),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except Exception:
        return "-"


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


class _GameContext:
    __slots__ = ("game_state", "turn_message_id", "anchor_ids")

    def __init__(self) -> None:
        self.game_state: Optional[str] = None
        self.turn_message_id: Optional[int] = None
        self.anchor_ids: List[int] = []

    @property
    def is_complete(self) -> bool:
        return (
            self.game_state is not None
            and self.turn_message_id is not None
            and bool(self.anchor_ids)
        )

    def absorb(self, obj: Any) -> None:
        if obj is None:
            return

        if self.game_state is None:
            state = getattr(obj, "state", None)
            if state is not None:
                self.game_state = getattr(state, "name", str(state))

        if self.turn_message_id is None:
            for attr in ("turn_message_id", "_turn_message_id"):
                if hasattr(obj, attr):
                    value = getattr(obj, attr)
                    if value is not None:
                        try:
                            self.turn_message_id = int(value)
                        except Exception:
                            self.turn_message_id = value  # type: ignore[assignment]
                        break

        if not self.anchor_ids:
            anchor_ids = _extract_anchor_ids(obj)
            if anchor_ids:
                self.anchor_ids = anchor_ids


def _extract_anchor_ids(obj: Any) -> List[int]:
    anchor_ids: List[int] = []

    anchors = getattr(obj, "_anchors", None)
    if isinstance(anchors, dict):
        for anchor in anchors.values():
            message_id = getattr(anchor, "message_id", None)
            if message_id is not None:
                anchor_ids.append(int(message_id))

    anchor_message = getattr(obj, "anchor_message", None)
    if isinstance(anchor_message, tuple) and len(anchor_message) >= 2:
        try:
            anchor_ids.append(int(anchor_message[1]))
        except Exception:
            pass

    players = getattr(obj, "players", None)
    if isinstance(players, Iterable):
        for player in players:
            candidate = getattr(player, "anchor_message", None)
            if isinstance(candidate, tuple) and len(candidate) >= 2:
                try:
                    anchor_ids.append(int(candidate[1]))
                except Exception:
                    continue
            else:
                message_id = getattr(candidate, "message_id", None)
                if message_id is not None:
                    anchor_ids.append(int(message_id))

    if anchor_ids:
        # Remove duplicates while preserving order
        seen: Dict[int, None] = {}
        unique_ids: List[int] = []
        for anchor_id in anchor_ids:
            if anchor_id in seen:
                continue
            seen[anchor_id] = None
            unique_ids.append(anchor_id)
        anchor_ids = unique_ids

    return anchor_ids


__all__ = ["trace_telegram_api_call", "DEBUG_TRACE_ENABLED"]

