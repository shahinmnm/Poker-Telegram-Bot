"""Helper utilities for structured logging across the poker bot."""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, MutableMapping, Tuple, Union


LoggerLike = Union[logging.Logger, logging.LoggerAdapter]

#: Standardised context keys emitted with each log record so downstream
#: aggregation systems can rely on a consistent schema.
STANDARD_CONTEXT_KEYS: Tuple[str, ...] = (
    "game_id",
    "chat_id",
    "user_id",
    "request_category",
    "event_type",
)

#: Baseline context included in every logger adapter to guarantee the
#: ``STANDARD_CONTEXT_KEYS`` are present in the payload, even when a specific
#: operation does not have values for all fields.
DEFAULT_LOG_CONTEXT: Dict[str, Any] = {key: None for key in STANDARD_CONTEXT_KEYS}


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that keeps structured context values attached."""

    def __init__(self, logger: logging.Logger, extra: Mapping[str, Any] | None = None):
        super().__init__(logger, dict(extra or {}))

    def process(self, msg: str, kwargs: MutableMapping[str, Any]):
        extra = dict(self.extra)
        provided = kwargs.get("extra")
        if provided:
            extra.update(provided)
        kwargs["extra"] = extra
        return msg, kwargs

    def getChild(self, suffix: str) -> "ContextLoggerAdapter":  # noqa: N802 - mirror logging API
        child = self.logger.getChild(suffix)
        return ContextLoggerAdapter(child, dict(self.extra))

    def bind(self, **kwargs: Any) -> "ContextLoggerAdapter":
        return add_context(self, **kwargs)


def _unwrap_logger(logger: LoggerLike) -> tuple[logging.Logger, Mapping[str, Any]]:
    if isinstance(logger, logging.LoggerAdapter):
        base_logger = logger.logger
        base_extra = getattr(logger, "extra", {})
        return base_logger, dict(base_extra)
    return logger, {}


def add_context(logger: LoggerLike, **kwargs: Any) -> ContextLoggerAdapter:
    """Return a :class:`ContextLoggerAdapter` with merged structured context."""

    base_logger, base_extra = _unwrap_logger(logger)
    merged: Dict[str, Any] = {**DEFAULT_LOG_CONTEXT, **base_extra}
    merged.update(kwargs)
    return ContextLoggerAdapter(base_logger, merged)


def normalise_request_category(value: Any) -> Any:
    """Convert enums or other simple objects into serialisable values."""

    if value is None:
        return None
    if hasattr(value, "value"):
        candidate = getattr(value, "value")
        if isinstance(candidate, (str, int, float)):
            return candidate
    return value

