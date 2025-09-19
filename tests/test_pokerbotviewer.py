import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden

from pokerapp.cards import Card

from pokerapp.config import (
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    DEFAULT_RATE_LIMIT_PER_SECOND,
)
from pokerapp.pokerbotview import PokerBotViewer


MENTION_LINK = "tg://user?id=123"
MENTION_MARKDOWN = f"[Player]({MENTION_LINK})"
HIDDEN_MENTION_TEXT = f"[\u2063]({MENTION_LINK})\u2063"


def run(coro):
    return asyncio.run(coro)


def _row_texts(row):
    return [getattr(button, "text", button) for button in row]


def test_pokerbotviewer_tracks_legacy_rate_limit_settings():
    default_viewer = PokerBotViewer(bot=MagicMock())
    assert (
        default_viewer._legacy_rate_limit_per_minute
        == DEFAULT_RATE_LIMIT_PER_MINUTE
    )
    assert (
        default_viewer._legacy_rate_limit_per_second
        == DEFAULT_RATE_LIMIT_PER_SECOND
    )

    viewer = PokerBotViewer(bot=MagicMock(), rate_limit_per_minute=123)

    assert viewer._legacy_rate_limit_per_minute == 123
    assert viewer._legacy_rate_limit_per_second == DEFAULT_RATE_LIMIT_PER_SECOND

    fast_viewer = PokerBotViewer(
        bot=MagicMock(),
        rate_limit_per_minute=120,
        rate_limit_per_second=3,
        rate_limiter_delay=0.25,
    )

    assert fast_viewer._legacy_rate_limit_per_minute == 120
    assert fast_viewer._legacy_rate_limit_per_second == 3
    assert fast_viewer._legacy_rate_limiter_delay == 0.25


def test_delete_message_ignores_missing_message(caplog):
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._bot.delete_message = AsyncMock(
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
    viewer._bot.delete_message = AsyncMock(
        side_effect=BadRequest("Some other error")
    )

    with caplog.at_level(logging.WARNING):
        run(viewer.delete_message(chat_id=123, message_id=456))

    assert any(
        record.levelno == logging.WARNING and "Failed to delete message" in record.message
        for record in caplog.records
    )


def test_delete_message_ignores_forbidden_when_message_cannot_be_deleted(caplog):
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._bot.delete_message = AsyncMock(
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
    viewer._bot.delete_message = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR):
        run(viewer.delete_message(chat_id=123, message_id=456))

    assert any(
        record.levelno == logging.ERROR and "Error deleting message" in record.message
        for record in caplog.records
    )


def test_notify_admin_failure_logs_error(caplog):
    viewer = PokerBotViewer(bot=MagicMock(), admin_chat_id=999)
    viewer._bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR):
        run(viewer.notify_admin({"event": "oops"}))

    assert viewer._bot.send_message.await_count == 1
    assert any(
        record.levelno == logging.ERROR and "Failed to notify admin" in record.message
        for record in caplog.records
    )


def test_send_cards_hides_group_hand_text_keeps_keyboard_message():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    viewer._bot.delete_message = AsyncMock()

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
    assert viewer._bot.delete_message.await_count == 0


def test_send_cards_hidden_text_replies_to_ready_message():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    viewer._bot.delete_message = AsyncMock()

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
    assert viewer._bot.delete_message.await_count == 0


def test_send_cards_hidden_edit_failure_sends_new_message_and_deletes_old():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=321))
    viewer._bot.delete_message = AsyncMock()
    viewer._bot.edit_message_text = AsyncMock(side_effect=BadRequest("cannot edit"))

    cards = [Card("A♠"), Card("K♦")]

    result = run(
        viewer.send_cards(
            chat_id=123,
            cards=cards,
            mention_markdown=MENTION_MARKDOWN,
            hide_hand_text=True,
            message_id=555,
        )
    )

    assert result == 321
    assert viewer._bot.edit_message_text.await_count == 1
    assert viewer._bot.send_message.await_count == 1
    send_call = viewer._bot.send_message.await_args
    assert "reply_to_message_id" not in send_call.kwargs
    assert send_call.kwargs["text"] == HIDDEN_MENTION_TEXT
    assert viewer._bot.delete_message.await_count == 1
    delete_call = viewer._bot.delete_message.await_args
    assert delete_call.kwargs == {"chat_id": 123, "message_id": 555}


def test_send_cards_includes_hand_details_by_default():
    viewer = PokerBotViewer(bot=MagicMock())
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


def test_send_message_uses_validated_payload():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    viewer._validator.normalize_text = MagicMock(return_value="cleaned")

    run(viewer.send_message(chat_id=123, text="raw", parse_mode=ParseMode.MARKDOWN))

    assert viewer._validator.normalize_text.call_count == 1
    call = viewer._bot.send_message.await_args
    assert call.kwargs["text"] == "cleaned"


def test_send_message_skips_when_validation_fails():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._bot.send_message = AsyncMock()
    viewer._validator.normalize_text = MagicMock(return_value=None)

    result = run(viewer.send_message(chat_id=55, text="bad", parse_mode=ParseMode.MARKDOWN))

    assert result is None
    assert viewer._bot.send_message.await_count == 0
