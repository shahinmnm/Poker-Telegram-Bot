from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pokerapp.config import Config
from pokerapp.utils.time_utils import format_local, now_utc, to_local


def test_now_utc_returns_aware_datetime():
    value = now_utc()
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


def test_to_local_applies_tehran_offset():
    base = datetime(2024, 1, 1, 0, 0, tzinfo=ZoneInfo("UTC"))

    local = to_local(base, tz_name="Asia/Tehran")

    assert local.hour == 3
    assert local.minute == 30
    assert local.utcoffset() == timedelta(hours=3, minutes=30)


def test_to_local_handles_dst_transition():
    before = to_local(
        datetime(2024, 3, 10, 6, 30, tzinfo=ZoneInfo("UTC")), tz_name="America/New_York"
    )
    after = to_local(
        datetime(2024, 3, 10, 7, 30, tzinfo=ZoneInfo("UTC")), tz_name="America/New_York"
    )

    assert before.hour == 1
    assert before.utcoffset() == timedelta(hours=-5)

    assert after.hour == 3
    assert after.utcoffset() == timedelta(hours=-4)


def test_format_local_uses_configured_timezone():
    cfg = Config()
    base = datetime(2024, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC"))

    formatted = format_local(base, "%Y-%m-%d %H:%M", tz_name=cfg.TIMEZONE_NAME)
    expected = to_local(base, tz_name=cfg.TIMEZONE_NAME).strftime("%Y-%m-%d %H:%M")

    assert formatted == expected
