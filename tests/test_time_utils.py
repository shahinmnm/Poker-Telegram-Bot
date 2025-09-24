import datetime as dt

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


def test_format_local_and_countdown_delta_use_utc_baseline():
    start = dt.datetime(2024, 1, 1, 12, 0)
    end = start + dt.timedelta(minutes=5)

    delta = countdown_delta(end, start)
    assert delta == dt.timedelta(minutes=5)

    formatted = format_local(start, "%H:%M", tz_name="Asia/Tehran")
    assert formatted == "15:30"
