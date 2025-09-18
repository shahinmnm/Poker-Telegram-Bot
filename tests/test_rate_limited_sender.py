import asyncio
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

    permit = RateLimitedSender._TokenPermit(remaining=0.5, wait_before=1.1)
    sender._wait_for_token = AsyncMock(return_value=permit)

    sleep_calls = []

    async def fake_sleep(duration: float):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def dummy_call():
        return "ok"

    result = await sender.send(dummy_call, chat_id=123)

    assert result == "ok"
    assert permit.remaining == pytest.approx(0.5)
    assert sleep_calls == [pytest.approx(1.1)]


@pytest.mark.asyncio
async def test_rate_limited_sender_allows_parallel_chats():
    sender = RateLimitedSender(delay=0.0, max_per_minute=120)

    start_events = {1: asyncio.Event(), 2: asyncio.Event()}
    release_event = asyncio.Event()

    async def fake_call(target_chat: int):
        start_events[target_chat].set()
        await release_event.wait()
        return target_chat

    task_one = asyncio.create_task(sender.send(fake_call, 1, chat_id=1))
    task_two = asyncio.create_task(sender.send(fake_call, 2, chat_id=2))

    await asyncio.wait_for(start_events[1].wait(), timeout=0.5)
    await asyncio.wait_for(start_events[2].wait(), timeout=0.5)

    release_event.set()

    results = await asyncio.gather(task_one, task_two)
    assert set(results) == {1, 2}
