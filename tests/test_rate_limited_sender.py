import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from pokerapp.pokerbotview import RateLimitedSender


@pytest.mark.parametrize(
    "max_per_minute, max_per_second, expected_delay",
    [
        (60, None, 1.0),
        (30, None, 2.0),
        (120, 2, 0.5),
        (20, 5, 0.2),
    ],
)
def test_rate_limited_sender_computes_delay(max_per_minute, max_per_second, expected_delay):
    sender = RateLimitedSender(
        max_per_minute=max_per_minute,
        max_per_second=max_per_second,
    )

    assert sender._delay == pytest.approx(expected_delay)


def test_rate_limited_sender_uses_explicit_delay():
    sender = RateLimitedSender(
        delay=0.25,
        max_per_minute=10,
        max_per_second=1,
    )

    assert sender._delay == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_rate_limited_sender_waits_longer_for_low_tokens(monkeypatch):
    sender = RateLimitedSender(delay=0.2, max_per_minute=30)

    bucket = {"tokens": 1.5, "ts": time.monotonic()}
    sender._wait_for_token = AsyncMock(return_value=bucket)

    sleep_calls = []

    async def fake_sleep(duration: float):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def dummy_call():
        return "ok"

    result = await sender.send(dummy_call, chat_id=123)

    assert result == "ok"
    assert bucket["tokens"] == pytest.approx(0.5)
    assert sleep_calls[-1] == pytest.approx(0.2 + 0.9)
