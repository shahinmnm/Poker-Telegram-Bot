"""Tests for the countdown worker implementation."""

from __future__ import annotations

import asyncio
import math
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError

from pokerapp.services.countdown_queue import CountdownMessageQueue
from pokerapp.services.countdown_worker import CountdownWorker


@pytest.mark.asyncio
async def test_worker_lifecycle() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    safe_ops.edit_message_text = AsyncMock()
    worker = CountdownWorker(queue, safe_ops, edit_interval=0.05)

    await worker.start()
    assert worker._worker_task is not None  # type: ignore[attr-defined]
    assert not worker._shutdown_event.is_set()  # type: ignore[attr-defined]

    await worker.stop()
    assert worker._worker_task is None  # type: ignore[attr-defined]
    assert worker._shutdown_event.is_set()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_process_single_countdown() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    safe_ops.edit_message_text = AsyncMock()

    worker = CountdownWorker(queue, safe_ops, edit_interval=0.15)
    await worker.start()

    await queue.enqueue(
        chat_id=1,
        message_id=100,
        text="⏳",
        duration_seconds=0.9,
        formatter=lambda remaining: f"⏳ {math.ceil(remaining)}",
    )

    await asyncio.sleep(1.2)
    await worker.stop()

    calls = safe_ops.edit_message_text.await_args_list
    assert len(calls) >= 3
    last_text = calls[-1].kwargs["text"]
    assert "0" in last_text or "⏳" in last_text


@pytest.mark.asyncio
async def test_cancellation_honored() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    safe_ops.edit_message_text = AsyncMock()

    on_complete = AsyncMock()
    worker = CountdownWorker(queue, safe_ops, edit_interval=0.1)
    await worker.start()

    msg = await queue.enqueue(
        chat_id=2,
        message_id=200,
        text="Countdown",
        duration_seconds=1.5,
        formatter=lambda remaining: f"{int(remaining)}",
        on_complete=on_complete,
    )

    async def cancel_after_delay() -> None:
        await asyncio.sleep(0.4)
        msg.cancelled = True

    asyncio.create_task(cancel_after_delay())
    await asyncio.sleep(0.9)
    await worker.stop()

    assert msg.cancelled is True
    on_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limiting() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    timestamps: List[float] = []

    async def record_time(**kwargs: object) -> None:
        timestamps.append(asyncio.get_running_loop().time())

    safe_ops.edit_message_text = AsyncMock(side_effect=record_time)
    worker = CountdownWorker(queue, safe_ops, edit_interval=0.3)
    await worker.start()

    await queue.enqueue(
        chat_id=3,
        message_id=300,
        text="RL",
        duration_seconds=1.2,
        formatter=lambda remaining: str(int(math.ceil(remaining))),
    )

    await asyncio.sleep(1.6)
    await worker.stop()

    assert len(timestamps) >= 3
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]
    assert all(delta >= 0.29 for delta in deltas)


@pytest.mark.asyncio
async def test_multiple_countdowns_sequential() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    safe_ops.edit_message_text = AsyncMock()

    completions: List[int] = []

    def make_callback(identifier: int):
        async def _callback() -> None:
            completions.append(identifier)
        return _callback

    worker = CountdownWorker(queue, safe_ops, edit_interval=0.1)
    await worker.start()

    for idx in range(3):
        await queue.enqueue(
            chat_id=idx,
            message_id=idx,
            text="Seq",
            duration_seconds=0.4,
            formatter=lambda remaining, idx=idx: f"{idx}:{int(math.ceil(remaining))}",
            on_complete=make_callback(idx),
        )

    await asyncio.sleep(2)
    await worker.stop()

    assert completions == [0, 1, 2]


@pytest.mark.asyncio
async def test_on_complete_callback() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    safe_ops.edit_message_text = AsyncMock()

    on_complete = AsyncMock()
    worker = CountdownWorker(queue, safe_ops, edit_interval=0.1)
    await worker.start()

    await queue.enqueue(
        chat_id=10,
        message_id=10,
        text="Done",
        duration_seconds=0.3,
        formatter=lambda remaining: f"{int(math.ceil(remaining))}",
        on_complete=on_complete,
    )

    await asyncio.sleep(0.6)
    await worker.stop()

    on_complete.assert_awaited()

    safe_ops.edit_message_text.reset_mock()
    on_complete.reset_mock()
    await worker.start()

    msg = await queue.enqueue(
        chat_id=11,
        message_id=11,
        text="Cancel",
        duration_seconds=1.0,
        formatter=lambda remaining: f"{int(math.ceil(remaining))}",
        on_complete=on_complete,
    )

    await asyncio.sleep(0.3)
    msg.cancelled = True
    await asyncio.sleep(0.5)
    await worker.stop()

    assert msg.cancelled is True
    on_complete.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_error_handling() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    safe_ops.edit_message_text = AsyncMock(side_effect=TelegramAPIError(MagicMock(), "failure"))

    worker = CountdownWorker(queue, safe_ops, edit_interval=0.1)
    await worker.start()

    await queue.enqueue(
        chat_id=12,
        message_id=12,
        text="Error",
        duration_seconds=0.4,
        formatter=lambda remaining: f"{int(math.ceil(remaining))}",
    )

    await asyncio.sleep(0.5)
    await worker.stop()

    assert safe_ops.edit_message_text.await_count >= 1


@pytest.mark.asyncio
async def test_shutdown_during_countdown() -> None:
    queue = CountdownMessageQueue()
    safe_ops = MagicMock()
    timestamps: List[float] = []

    async def record_time(**kwargs: object) -> None:
        timestamps.append(asyncio.get_running_loop().time())

    safe_ops.edit_message_text = AsyncMock(side_effect=record_time)
    worker = CountdownWorker(queue, safe_ops, edit_interval=0.2)
    await worker.start()

    on_complete = AsyncMock()
    msg = await queue.enqueue(
        chat_id=13,
        message_id=13,
        text="Shutdown",
        duration_seconds=2.0,
        formatter=lambda remaining: f"{int(math.ceil(remaining))}",
        on_complete=on_complete,
    )

    await asyncio.sleep(0.4)
    await worker.stop()

    assert len(timestamps) >= 1
    on_complete.assert_not_called()
    assert msg.cancelled is False
