"""Timezone helpers for consistently handling user-facing timestamps."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pokerapp.config import DEFAULT_TIMEZONE_NAME


_UTC_ZONE = ZoneInfo("UTC")


def now_utc() -> datetime:
    """Return the current time as an aware ``datetime`` in UTC."""

    return datetime.now(tz=_UTC_ZONE)


def _normalize_timezone_name(tz_name: Optional[str]) -> str:
    if isinstance(tz_name, str):
        candidate = tz_name.strip()
        if candidate:
            return candidate
    return DEFAULT_TIMEZONE_NAME


def _resolve_zone(tz_name: Optional[str]) -> ZoneInfo:
    name = _normalize_timezone_name(tz_name)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name != DEFAULT_TIMEZONE_NAME:
            return _resolve_zone(DEFAULT_TIMEZONE_NAME)
        return _UTC_ZONE


def to_local(dt: datetime, tz_name: Optional[str] = DEFAULT_TIMEZONE_NAME) -> datetime:
    """Convert ``dt`` from UTC into ``tz_name`` (defaulting to the configured zone)."""

    zone = _resolve_zone(tz_name)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=_UTC_ZONE)
    return dt.astimezone(zone)


def format_local(
    dt: datetime,
    fmt: str,
    tz_name: Optional[str] = DEFAULT_TIMEZONE_NAME,
) -> str:
    """Format ``dt`` in ``tz_name`` using ``fmt`` (defaults to the configured zone)."""

    return to_local(dt, tz_name).strftime(fmt)


__all__ = ["now_utc", "to_local", "format_local"]
