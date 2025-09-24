"""Timezone-aware datetime helpers used across the poker application."""

from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pokerapp.config import DEFAULT_TIMEZONE_NAME as CONFIG_DEFAULT_TIMEZONE_NAME


logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE_NAME = CONFIG_DEFAULT_TIMEZONE_NAME
UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    """Return the current time as an aware ``datetime`` in UTC."""

    return dt.datetime.now(UTC)


def _ensure_aware_utc(value: dt.datetime) -> dt.datetime:
    """Coerce ``value`` to an aware UTC datetime without altering the instant."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_timezone_name(candidate: Optional[str]) -> str:
    """Return a sanitized timezone name or the configured default."""

    if isinstance(candidate, str):
        stripped = candidate.strip()
        if stripped:
            return stripped
    return DEFAULT_TIMEZONE_NAME


@lru_cache(maxsize=32)
def _resolve_zoneinfo(name: Optional[str]) -> dt.tzinfo:
    """Return a ``tzinfo`` for ``name`` falling back to UTC on failure."""

    candidate = _normalize_timezone_name(name)
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        if candidate != DEFAULT_TIMEZONE_NAME:
            logger.warning(
                "Unknown timezone %s; falling back to %s", candidate, DEFAULT_TIMEZONE_NAME
            )
            return _resolve_zoneinfo(DEFAULT_TIMEZONE_NAME)
        logger.warning("Unknown timezone %s; falling back to UTC", candidate)
        return UTC


def to_local(value: dt.datetime, tz_name: Optional[str] = DEFAULT_TIMEZONE_NAME) -> dt.datetime:
    """Convert ``value`` to the target timezone, assuming UTC when naive."""

    aware_utc = _ensure_aware_utc(value)
    zone = _resolve_zoneinfo(tz_name)
    return aware_utc.astimezone(zone)


def format_local(
    value: dt.datetime, fmt: str, tz_name: Optional[str] = DEFAULT_TIMEZONE_NAME
) -> str:
    """Return ``value`` formatted in the requested timezone using ``fmt``."""

    localized = to_local(value, tz_name=tz_name)
    return localized.strftime(fmt)


def countdown_delta(end_time: dt.datetime, start_time: dt.datetime) -> dt.timedelta:
    """Return the timedelta between ``end_time`` and ``start_time`` in UTC."""

    end_utc = _ensure_aware_utc(end_time)
    start_utc = _ensure_aware_utc(start_time)
    return end_utc - start_utc


__all__ = [
    "DEFAULT_TIMEZONE_NAME",
    "UTC",
    "now_utc",
    "to_local",
    "format_local",
    "countdown_delta",
]

