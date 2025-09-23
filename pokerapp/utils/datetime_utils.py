"""Helpers for consistent timezone-aware datetime handling."""

from __future__ import annotations

import datetime as dt
from typing import Optional

UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    """Return the current UTC time as a timezone-aware ``datetime``."""
    return dt.datetime.now(UTC)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Ensure ``value`` is timezone-aware in UTC.

    Naive datetimes are assumed to already represent UTC and will be annotated
    accordingly. Aware datetimes are converted to UTC.
    """

    tzinfo = value.tzinfo
    if tzinfo is None or tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def coerce_utc(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """Return ``value`` normalized to UTC, handling ``None`` gracefully."""

    if value is None:
        return None
    return ensure_utc(value)


def isoformat_utc(value: dt.datetime, *, timespec: Optional[str] = None) -> str:
    """Format ``value`` as an ISO 8601 string with a ``Z`` UTC suffix."""

    aware = ensure_utc(value)
    text = aware.isoformat() if timespec is None else aware.isoformat(timespec=timespec)
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def utc_isoformat(*, timespec: Optional[str] = None) -> str:
    """Return ``utc_now()`` formatted as ISO 8601 with a ``Z`` suffix."""

    return isoformat_utc(utc_now(), timespec=timespec)


__all__ = [
    "UTC",
    "utc_now",
    "ensure_utc",
    "coerce_utc",
    "isoformat_utc",
    "utc_isoformat",
]
