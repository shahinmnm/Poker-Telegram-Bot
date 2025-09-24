import datetime as dt

from pokerapp.config import DEFAULT_TIMEZONE_NAME
from pokerapp.utils.time_utils import countdown_delta, format_local, now_utc, to_local


def test_now_utc_returns_aware_datetime():
    value = now_utc()
    assert value.tzinfo is not None
    assert value.tzinfo.utcoffset(value) == dt.timedelta(0)


def test_to_local_handles_european_dst_transition():
    before_dst = to_local(
        dt.datetime(2024, 3, 31, 0, 30, tzinfo=dt.timezone.utc), tz_name="Europe/Berlin"
    )
    after_dst = to_local(
        dt.datetime(2024, 3, 31, 1, 30, tzinfo=dt.timezone.utc), tz_name="Europe/Berlin"
    )

    assert before_dst.hour == 1
    assert before_dst.minute == 30
    assert before_dst.utcoffset() == dt.timedelta(hours=1)

    assert after_dst.hour == 3
    assert after_dst.minute == 30
    assert after_dst.utcoffset() == dt.timedelta(hours=2)


def test_to_local_handles_american_dst_fall_transition():
    first = to_local(
        dt.datetime(2024, 11, 3, 5, 30, tzinfo=dt.timezone.utc), tz_name="America/New_York"
    )
    second = to_local(
        dt.datetime(2024, 11, 3, 6, 30, tzinfo=dt.timezone.utc), tz_name="America/New_York"
    )

    assert first.hour == 1
    assert first.minute == 30
    assert first.utcoffset() == dt.timedelta(hours=-4)
    assert first.fold == 0

    assert second.hour == 1
    assert second.minute == 30
    assert second.utcoffset() == dt.timedelta(hours=-5)
    assert second.fold == 1


def test_format_local_and_countdown_delta_use_utc_baseline():
    start = dt.datetime(2024, 1, 1, 12, 0)
    end = start + dt.timedelta(minutes=5)

    delta = countdown_delta(end, start)
    assert delta == dt.timedelta(minutes=5)

    formatted = format_local(start, "%H:%M", tz_name="Asia/Tehran")
    assert formatted == "15:30"


def test_to_local_uses_config_default_when_timezone_missing():
    base = dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

    expected = to_local(base, tz_name=DEFAULT_TIMEZONE_NAME)
    actual = to_local(base)

    assert actual == expected


def test_to_local_normalizes_timezone_name_inputs():
    base = dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

    trimmed = to_local(base, tz_name="  Europe/Berlin  ")
    explicit = to_local(base, tz_name="Europe/Berlin")
    fallback = to_local(base, tz_name="   ")
    default = to_local(base, tz_name=None)

    assert trimmed == explicit
    assert fallback == to_local(base, tz_name=DEFAULT_TIMEZONE_NAME)
    assert default == to_local(base, tz_name=DEFAULT_TIMEZONE_NAME)
