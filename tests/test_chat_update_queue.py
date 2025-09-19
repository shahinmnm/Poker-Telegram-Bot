import asyncio
from unittest.mock import AsyncMock

import pytest

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from pokerapp.pokerbotview import PokerBotViewer


class DummyBot:
    def __init__(self):
        self.edit_message_text = AsyncMock()


def _build_viewer(debounce: float = 0.05) -> PokerBotViewer:
    bot = DummyBot()
    return PokerBotViewer(
        bot,
        rate_limit_per_minute=600,
        rate_limit_per_second=600,
        rate_limiter_delay=0,
        update_debounce=debounce,
    )


@pytest.mark.asyncio
async def test_chat_update_queue_coalesces_edits():
    viewer = _build_viewer()
    texts_seen = []

    async def record_text(*args, **kwargs):
        texts_seen.append(kwargs["text"])
        return True

    viewer._bot.edit_message_text.side_effect = record_text

    task_one = asyncio.create_task(
        viewer.edit_message_text(1, 42, "first", reply_markup=None)
    )
    await asyncio.sleep(0)
    task_two = asyncio.create_task(
        viewer.edit_message_text(1, 42, "second", reply_markup=None)
    )
    await asyncio.sleep(0)
    task_three = asyncio.create_task(
        viewer.edit_message_text(1, 42, "final", reply_markup=None)
    )

    results = await asyncio.gather(task_one, task_two, task_three)

    assert results == [42, 42, 42]
    assert viewer._bot.edit_message_text.await_count == 1
    assert texts_seen[-1] == "final"


@pytest.mark.asyncio
async def test_chat_update_queue_preserves_order():
    viewer = _build_viewer()
    call_order: list[tuple[int, str]] = []

    async def capture_order(*args, **kwargs):
        call_order.append((kwargs["message_id"], kwargs["text"]))
        return True

    viewer._bot.edit_message_text.side_effect = capture_order

    tasks = [
        asyncio.create_task(viewer.edit_message_text(99, 1, "old-1")),
        asyncio.create_task(viewer.edit_message_text(99, 2, "old-2")),
    ]
    await asyncio.sleep(0)
    tasks.extend(
        [
            asyncio.create_task(viewer.edit_message_text(99, 1, "new-1")),
            asyncio.create_task(viewer.edit_message_text(99, 2, "new-2")),
        ]
    )

    await asyncio.gather(*tasks)

    assert call_order == [(1, "new-1"), (2, "new-2")]


@pytest.mark.asyncio
async def test_chat_update_queue_skips_duplicate_payload():
    viewer = _build_viewer(debounce=0.01)
    viewer._bot.edit_message_text.return_value = True

    result = await viewer.edit_message_text(5, 77, "hello")
    assert result == 77
    assert viewer._bot.edit_message_text.await_count == 1

    duplicate = await viewer.edit_message_text(5, 77, "hello")
    assert duplicate == 77
    assert viewer._bot.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_chat_update_queue_detects_markup_change():
    viewer = _build_viewer(debounce=0.01)

    markup_one = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton("A", callback_data="a")
    )
    markup_two = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton("B", callback_data="b")
    )

    viewer._bot.edit_message_text.return_value = True

    await viewer.edit_message_text(7, 88, "hello", reply_markup=markup_one)
    assert viewer._bot.edit_message_text.await_count == 1

    await viewer.edit_message_text(7, 88, "hello", reply_markup=markup_two)
    assert viewer._bot.edit_message_text.await_count == 2
