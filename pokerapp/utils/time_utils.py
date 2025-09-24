"""Timezone helpers for consistently handling user-facing timestamps."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    """Return the current UTC time as an aware ``datetime`` object."""

    return datetime.now(tz=ZoneInfo("UTC"))


def to_local(dt: datetime, tz_name: str) -> datetime:
    """Convert ``dt`` into the timezone identified by ``tz_name``."""

    return dt.astimezone(ZoneInfo(tz_name))


def format_local(dt: datetime, tz_name: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format ``dt`` for ``tz_name`` using ``fmt``."""

    return to_local(dt, tz_name).strftime(fmt)


__all__ = ["now_utc", "to_local", "format_local"]
