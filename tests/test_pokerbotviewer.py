import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden

from pokerapp.cards import Card

from pokerapp.config import (
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    DEFAULT_RATE_LIMIT_PER_SECOND,
)
from pokerapp.entities import Game, GameState, Player, PlayerAction
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



def test_update_player_anchor_creates_anchor_message():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._messenger.send_message = AsyncMock(return_value=MagicMock(message_id=42))

    player = MagicMock(mention_markdown=MENTION_MARKDOWN, user_id=111)
    player.cards = [Card('A♣'), Card('K♥')]
    board_cards = [Card('A♠'), Card('K♦'), Card('5♣')]

    result = run(
        viewer.update_player_anchor(
            chat_id=555,
            player=player,
            seat_number=3,
            role_label='دیلر',
            board_cards=board_cards,
            player_cards=player.cards,
            game_state=GameState.ROUND_PRE_FLOP,
            active=True,
        )
    )

    assert result == 42
    call = viewer._messenger.send_message.await_args
    assert '🪑 صندلی: `3`' in call.kwargs['text']
    assert '🎖️ نقش: دیلر' in call.kwargs['text']
    assert '🃏 Board:' in call.kwargs['text']
    assert '🎯 **نوبت بازی این بازیکن است.**' in call.kwargs['text']
    markup = call.kwargs['reply_markup']
    assert isinstance(markup, InlineKeyboardMarkup)
    rows = markup.inline_keyboard
    assert [button.text for button in rows[0]] == ['🎴 کارت‌های شما']
    assert [button.text for button in rows[1]] == ['A♣️', 'K♥️']
    assert [button.text for button in rows[2]] == ['🃏 کارت‌های روی میز']
    assert [button.text for button in rows[3]] == ['A♠️', 'K♦️', '5♣️']


def test_update_player_anchor_inactive_player_keeps_card_keyboard():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._messenger.edit_message_text = AsyncMock(return_value=77)

    player = MagicMock(mention_markdown=MENTION_MARKDOWN, user_id=222)
    player.cards = [Card('Q♣'), Card('J♥')]
    board_cards = [Card('Q♠'), Card('J♦'), Card('9♣'), Card('2♥')]

    result = run(
        viewer.update_player_anchor(
            chat_id=888,
            player=player,
            seat_number=4,
            role_label='بازیکن',
            board_cards=board_cards,
            player_cards=player.cards,
            game_state=GameState.ROUND_TURN,
            active=False,
            message_id=77,
        )
    )

    assert result == 77
    call = viewer._messenger.edit_message_text.await_args
    assert '🃏 Board:' in call.kwargs['text']
    assert '🎯 **نوبت بازی این بازیکن است.**' not in call.kwargs['text']
    markup = call.kwargs['reply_markup']
    assert isinstance(markup, InlineKeyboardMarkup)
    rows = markup.inline_keyboard
    assert [button.text for button in rows[0]] == ['🎴 کارت‌های شما']
    assert [button.text for button in rows[1]] == ['Q♣️', 'J♥️']
    assert [button.text for button in rows[2]] == ['🃏 کارت‌های روی میز']
    assert [button.text for button in rows[3]] == ['Q♠️', 'J♦️', '9♣️']
    assert [button.text for button in rows[4]] == ['2♥️']


def test_update_player_anchor_when_game_inactive_shows_menu():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._messenger.edit_message_text = AsyncMock(return_value=99)

    player = MagicMock(mention_markdown=MENTION_MARKDOWN, user_id=333)
    player.cards = [Card('2♣'), Card('3♦')]

    result = run(
        viewer.update_player_anchor(
            chat_id=-777,
            player=player,
            seat_number=2,
            role_label='بازیکن',
            board_cards=[],
            player_cards=player.cards,
            game_state=GameState.INITIAL,
            active=False,
            message_id=99,
        )
    )

    assert result == 99
    call = viewer._messenger.edit_message_text.await_args
    markup = call.kwargs['reply_markup']
    assert isinstance(markup, InlineKeyboardMarkup)
    rows = [[button.text for button in row] for row in markup.inline_keyboard]
    assert rows == [
        ['🎮 شروع بازی جدید', '📊 آمار شما'],
        ['🛠 راهنما / قوانین', '💰 موجودی کیف پول'],
        ['💬 گفتگوی دوستانه'],
    ]


def test_update_turn_message_includes_stage_and_keyboard():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._update_message = AsyncMock(return_value=321)

    game = Game()
    game.state = GameState.ROUND_TURN
    game.max_round_rate = 30
    game.pot = 120
    game.cards_table = [Card('A♠'), Card('K♦'), Card('5♣'), Card('9♥')]
    game.last_actions = ['action 1', 'action 2']

    player = Player(
        user_id=111,
        mention_markdown=MENTION_MARKDOWN,
        wallet=MagicMock(),
        ready_message_id='ready',
    )
    player.seat_index = 0
    player.round_rate = 10

    result = run(
        viewer.update_turn_message(
            chat_id=555,
            game=game,
            player=player,
            money=500,
        )
    )

    assert result.message_id == 321
    call = viewer._update_message.await_args
    text = call.kwargs['text']
    assert '🎯 **نوبت:**' in text
    assert '🎰 **مرحله بازی:** Turn' in text
    assert '🃏 Board:' in text
    assert '🎬 **اکشن‌های اخیر:**' in text
    assert '⬇️ **از دکمه‌های زیر برای اقدام استفاده کنید.**' in text

    markup = call.kwargs['reply_markup']
    assert isinstance(markup, InlineKeyboardMarkup)
    first_row = [button.text for button in markup.inline_keyboard[0]]
    assert PlayerAction.FOLD.value in first_row
    assert PlayerAction.ALL_IN.value in first_row
    assert any('🎯 کال' in label for label in first_row)

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
