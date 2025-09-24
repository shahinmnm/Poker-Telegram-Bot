"""Helper utilities for structured logging across the poker bot."""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, MutableMapping, Tuple, Union


LoggerLike = Union[logging.Logger, logging.LoggerAdapter]

#: Application log records must include these core context keys so that
#: downstream processing pipelines can rely on a consistent schema.
REQUIRED_LOG_KEYS: Tuple[str, ...] = (
    "game_id",
    "chat_id",
    "user_id",
    "event_type",
    "request_category",
)

#: Backwards compatible alias used throughout the codebase.  The tuple mirrors
#: ``REQUIRED_LOG_KEYS`` to avoid duplicating schemas in older call sites.
STANDARD_CONTEXT_KEYS: Tuple[str, ...] = REQUIRED_LOG_KEYS

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


def enforce_context(
    logger: LoggerLike, default_ctx: Mapping[str, Any] | None = None
) -> ContextLoggerAdapter:
    """Wrap ``logger`` ensuring :data:`REQUIRED_LOG_KEYS` are always present.

    The helper is intended for boundary wiring (for example during
    :mod:`pokerapp.bootstrap`) where infrastructure services receive a logger.
    It merges any context that is already attached to ``logger`` with the
    provided ``default_ctx`` while guaranteeing that the required keys exist.
    """

    base_logger, base_extra = _unwrap_logger(logger)
    enforced: Dict[str, Any] = {**DEFAULT_LOG_CONTEXT, **base_extra}
    if default_ctx:
        enforced.update(default_ctx)
    for key in REQUIRED_LOG_KEYS:
        enforced.setdefault(key, None)
    return ContextLoggerAdapter(base_logger, enforced)


def normalise_request_category(value: Any) -> Any:
    """Convert enums or other simple objects into serialisable values."""

    if value is None:
        return None
    if hasattr(value, "value"):
        candidate = getattr(value, "value")
        if isinstance(candidate, (str, int, float)):
            return candidate
    return value

