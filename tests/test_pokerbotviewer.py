import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

from telegram import ReplyKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from pokerapp.cards import Card
from pokerapp.pokerbotview import PokerBotViewer


MENTION_LINK = "tg://user?id=123"
MENTION_MARKDOWN = f"[Player]({MENTION_LINK})"
HIDDEN_MENTION_TEXT = f"[\u2063]({MENTION_LINK})\u2063"


def run(coro):
    return asyncio.run(coro)


def _row_texts(row):
    return [getattr(button, "text", button) for button in row]


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


def test_notify_admin_failure_does_not_deadlock():
    viewer = PokerBotViewer(bot=MagicMock(), admin_chat_id=999)
    viewer._rate_limiter._delay = 0
    viewer._rate_limiter._error_delay = 0

    failing_call = AsyncMock(side_effect=RuntimeError("boom"))
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))

    async def invoke_send():
        return await viewer._rate_limiter.send(failing_call, chat_id=555)

    result = run(asyncio.wait_for(invoke_send(), timeout=0.5))

    assert result is None
    assert viewer._bot.send_message.await_count == 2
    chat_ids = {call.kwargs["chat_id"] for call in viewer._bot.send_message.await_args_list}
    assert chat_ids == {999}
    events = [json.loads(call.kwargs["text"])["event"] for call in viewer._bot.send_message.await_args_list]
    assert events.count("rate_limiter_error") == 1
    assert events.count("rate_limiter_failed") == 1


async def _passthrough_rate_limit(func, *args, **kwargs):
    return await func()


def test_send_cards_hides_group_hand_text_keeps_keyboard_message():
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
            mention_markdown=MENTION_MARKDOWN,
            table_cards=table_cards,
            hide_hand_text=True,
        )
    )

    assert result == 42
    assert viewer._bot.send_message.await_count == 1
    call = viewer._bot.send_message.await_args
    text = call.kwargs["text"]
    assert text == HIDDEN_MENTION_TEXT
    assert "Player" not in text
    assert "🔒" not in text
    assert "reply_to_message_id" not in call.kwargs
    markup = call.kwargs["reply_markup"]
    assert markup is not None
    assert _row_texts(markup.keyboard[0]) == ["A♠", "K♦"]
    assert _row_texts(markup.keyboard[1]) == ["2♣", "3♣", "4♣"]
    assert _row_texts(markup.keyboard[2]) == ["🔁 پری فلاپ", "✅ فلاپ", "🔁 ترن", "🔁 ریور"]
    assert viewer.delete_message.await_count == 0


def test_send_cards_hidden_text_replies_to_ready_message():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = _passthrough_rate_limit  # type: ignore[assignment]
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    viewer.delete_message = AsyncMock()

    cards = [Card("A♠"), Card("K♦")]

    result = run(
        viewer.send_cards(
            chat_id=123,
            cards=cards,
            mention_markdown=MENTION_MARKDOWN,
            ready_message_id="777",
            hide_hand_text=True,
        )
    )

    assert result == 99
    call = viewer._bot.send_message.await_args
    assert call.kwargs["reply_to_message_id"] == "777"
    assert call.kwargs["text"] == HIDDEN_MENTION_TEXT
    assert viewer.delete_message.await_count == 0


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
            mention_markdown=MENTION_MARKDOWN,
            table_cards=table_cards,
        )
    )

    assert viewer._bot.send_message.await_count == 1
    call = viewer._bot.send_message.await_args
    text = call.kwargs["text"]
    assert "Q♥" in text and "J♥" in text
    assert "10♥" in text and "9♥" in text and "8♥" in text
    markup = call.kwargs["reply_markup"]
    assert _row_texts(markup.keyboard[0]) == ["Q♥", "J♥"]
    assert _row_texts(markup.keyboard[1]) == ["10♥", "9♥", "8♥"]
    assert _row_texts(markup.keyboard[2])[1].startswith("✅")


def test_table_markup_excludes_show_table_button():
    table_cards = [Card("A♠"), Card("K♦"), Card("Q♣")]

    markup = PokerBotViewer._get_table_markup(table_cards, stage="flop")

    assert _row_texts(markup.keyboard[0]) == ["A♠", "K♦", "Q♣"]
    stage_row = _row_texts(markup.keyboard[1])
    assert "👁️ نمایش میز" not in stage_row
    assert stage_row == ["پری فلاپ", "✅ فلاپ", "ترن", "ریور"]


def test_new_hand_ready_message_uses_reply_keyboard():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._rate_limiter.send = _passthrough_rate_limit  # type: ignore[assignment]
    viewer._bot.send_message = AsyncMock()

    run(viewer.send_new_hand_ready_message(chat_id=987))

    assert viewer._bot.send_message.await_count == 1
    call = viewer._bot.send_message.await_args
    markup = call.kwargs.get("reply_markup")
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.resize_keyboard is True
    assert markup.one_time_keyboard is False
    assert markup.selective is False
    assert _row_texts(markup.keyboard[0]) == ["/start", "نشستن سر میز"]
    assert _row_texts(markup.keyboard[1]) == ["/stop"]
