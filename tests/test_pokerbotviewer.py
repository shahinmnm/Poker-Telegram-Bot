import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest, Forbidden

from pokerapp.cards import Card
from pokerapp.pokerbotview import PokerBotViewer


def run(coro):
    return asyncio.run(coro)


def test_delete_message_ignores_missing_message(caplog):
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = AsyncMock(
        side_effect=BadRequest("Message to delete not found")
    )

    with caplog.at_level(logging.DEBUG):
        run(viewer.delete_message(chat_id=123, message_id=456))

    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert not any(
        record.levelno == logging.WARNING and "Failed to delete message" in record.message
        for record in caplog.records
    )


def test_delete_message_logs_warning_for_unexpected_bad_request(caplog):
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = AsyncMock(side_effect=BadRequest("Some other error"))

    with caplog.at_level(logging.WARNING):
        run(viewer.delete_message(chat_id=123, message_id=456))

    assert any(
        record.levelno == logging.WARNING and "Failed to delete message" in record.message
        for record in caplog.records
    )


def test_delete_message_ignores_forbidden_when_message_cannot_be_deleted(caplog):
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = AsyncMock(
        side_effect=Forbidden("message can't be deleted")
    )

    with caplog.at_level(logging.DEBUG):
        run(viewer.delete_message(chat_id=123, message_id=456))

    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert not any(
        record.levelno == logging.WARNING and "Failed to delete message" in record.message
        for record in caplog.records
    )


def test_delete_message_logs_error_for_unexpected_exception(caplog):
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR):
        run(viewer.delete_message(chat_id=123, message_id=456))

    assert any(
        record.levelno == logging.ERROR and "Error deleting message" in record.message
        for record in caplog.records
    )


async def _passthrough_rate_limit(func, *args, **kwargs):
    return await func()


def test_send_cards_hides_group_hand_text_without_leaving_message():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = _passthrough_rate_limit  # type: ignore[assignment]
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    viewer.delete_message = AsyncMock()

    cards = [Card("A♠"), Card("K♦")]
    table_cards = [Card("2♣"), Card("3♣"), Card("4♣")]

    result = run(
        viewer.send_cards(
            chat_id=123,
            cards=cards,
            mention_markdown="@player",
            table_cards=table_cards,
            hide_hand_text=True,
        )
    )

    assert result is None
    assert viewer._bot.send_message.await_count == 1
    call = viewer._bot.send_message.await_args
    text = call.kwargs["text"]
    assert text == "\u2063"
    assert "@player" not in text
    assert "🔒" not in text
    assert call.kwargs["reply_markup"] is not None
    viewer.delete_message.assert_awaited_once_with(chat_id=123, message_id=42)


def test_send_cards_hides_group_hand_text_and_clears_previous_message():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = _passthrough_rate_limit  # type: ignore[assignment]
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=55))
    viewer.delete_message = AsyncMock()

    cards = [Card("A♠"), Card("K♦")]

    run(
        viewer.send_cards(
            chat_id=123,
            cards=cards,
            mention_markdown="@player",
            hide_hand_text=True,
            message_id=777,
        )
    )

    assert viewer.delete_message.await_count == 2
    assert viewer.delete_message.await_args_list[0].kwargs == {
        "chat_id": 123,
        "message_id": 777,
    }
    assert viewer.delete_message.await_args_list[1].kwargs == {
        "chat_id": 123,
        "message_id": 55,
    }


def test_send_cards_includes_hand_details_by_default():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = _passthrough_rate_limit  # type: ignore[assignment]
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=24))

    cards = [Card("Q♥"), Card("J♥")]
    table_cards = [Card("10♥"), Card("9♥"), Card("8♥")]

    run(
        viewer.send_cards(
            chat_id=456,
            cards=cards,
            mention_markdown="@player",
            table_cards=table_cards,
        )
    )

    assert viewer._bot.send_message.await_count == 1
    call = viewer._bot.send_message.await_args
    text = call.kwargs["text"]
    assert "Q♥" in text and "J♥" in text
    assert "10♥" in text and "9♥" in text and "8♥" in text
