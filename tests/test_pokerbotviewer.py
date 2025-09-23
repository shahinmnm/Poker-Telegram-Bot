import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from types import SimpleNamespace

from pokerapp.cards import Card

from pokerapp.config import (
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    DEFAULT_RATE_LIMIT_PER_SECOND,
)
from pokerapp.entities import Game, GameState, Player, PlayerAction
from pokerapp.pokerbotview import PokerBotViewer, build_player_cards_keyboard
from pokerapp.utils.request_metrics import RequestCategory


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



def test_update_player_anchors_and_keyboards_highlights_active_player():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._update_message = AsyncMock(return_value=101)
    viewer.edit_message_reply_markup = AsyncMock(return_value=True)
    viewer.send_message_return_id = AsyncMock()

    game = Game()
    game.chat_id = -777
    game.state = GameState.ROUND_FLOP
    game.cards_table = [Card('A♠'), Card('K♦'), Card('5♣')]

    player_one = Player(
        user_id=1,
        mention_markdown='@one',
        wallet=MagicMock(),
        ready_message_id='ready-1',
    )
    player_two = Player(
        user_id=2,
        mention_markdown='@two',
        wallet=MagicMock(),
        ready_message_id='ready-2',
    )

    game.add_player(player_one, seat_index=0)
    game.add_player(player_two, seat_index=1)
    player_one.cards = [Card('J♠'), Card('J♦')]
    player_two.cards = [Card('9♣'), Card('9♦')]
    player_one.display_name = 'Player One'
    player_two.display_name = 'Player Two'
    player_one.role_label = 'دیلر'
    player_two.role_label = 'بلایند بزرگ'

    player_one.anchor_message = (game.chat_id, 101)
    player_two.anchor_message = (game.chat_id, 202)
    game.current_player_index = 0

    stage_name = game.state.name
    community_cards = viewer._extract_community_cards(game)

    base_one = viewer._build_anchor_text(
        display_name=player_one.display_name,
        mention_markdown=player_one.mention_markdown,
        seat_number=1,
        role_label=player_one.role_label,
    )
    keyboard_one = viewer._compose_anchor_keyboard(
        stage_name=stage_name,
        hole_cards=viewer._extract_player_hole_cards(player_one),
        community_cards=community_cards,
    )
    payload_one = viewer._reply_keyboard_signature(
        text=base_one,
        reply_markup=keyboard_one,
        stage_name=stage_name,
        community_cards=community_cards,
        hole_cards=viewer._extract_player_hole_cards(player_one),
        turn_indicator="",
    )
    viewer._anchor_registry.register_role(
        game.chat_id,
        player_id=player_one.user_id,
        seat_index=0,
        message_id=101,
        base_text=base_one,
        payload_signature=payload_one,
        markup_signature=viewer._serialize_markup(keyboard_one) or "",
    )
    player_one.anchor_keyboard_signature = payload_one

    base_two = viewer._build_anchor_text(
        display_name=player_two.display_name,
        mention_markdown=player_two.mention_markdown,
        seat_number=2,
        role_label=player_two.role_label,
    )
    keyboard_two = viewer._compose_anchor_keyboard(
        stage_name=stage_name,
        hole_cards=viewer._extract_player_hole_cards(player_two),
        community_cards=community_cards,
    )
    payload_two = viewer._reply_keyboard_signature(
        text=base_two,
        reply_markup=keyboard_two,
        stage_name=stage_name,
        community_cards=community_cards,
        hole_cards=viewer._extract_player_hole_cards(player_two),
        turn_indicator="",
    )
    viewer._anchor_registry.register_role(
        game.chat_id,
        player_id=player_two.user_id,
        seat_index=1,
        message_id=202,
        base_text=base_two,
        payload_signature=payload_two,
        markup_signature=viewer._serialize_markup(keyboard_two) or "",
    )
    player_two.anchor_keyboard_signature = payload_two

    run(viewer.update_player_anchors_and_keyboards(game))

    assert viewer._update_message.await_count == 1
    viewer.edit_message_reply_markup.assert_not_awaited()
    viewer.send_message_return_id.assert_not_awaited()

    first_call = viewer._update_message.await_args_list[0]

    assert first_call.kwargs['message_id'] == 101
    first_text = first_call.kwargs['text']
    assert "🟢 نوبت این بازیکن است." in first_text or "🔴 نوبت این بازیکن است." in first_text
    assert 'Player One' in first_text
    assert '🪑 صندلی: 1' in first_text
    assert '🎖️ نقش: دیلر' in first_text
    assert isinstance(first_call.kwargs['reply_markup'], ReplyKeyboardMarkup)
    assert first_call.kwargs['force_send'] is True
    first_keyboard = first_call.kwargs['reply_markup']
    assert _row_texts(first_keyboard.keyboard[0]) == ['J♠️', 'J♦️']
    assert _row_texts(first_keyboard.keyboard[1]) == ['A♠️', 'K♦️', '5♣️']
    stage_row = _row_texts(first_keyboard.keyboard[2])
    assert stage_row[1].startswith('✅')
    assert 'فلاپ' in stage_row[1]

    assert player_one.anchor_message == (game.chat_id, 101)
    assert player_two.anchor_message == (game.chat_id, 202)
    assert player_one.anchor_keyboard_signature
    assert player_two.anchor_keyboard_signature


def test_update_player_anchors_and_keyboards_skips_players_without_anchor():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer._update_message = AsyncMock()

    game = Game()
    game.chat_id = -123
    game.state = GameState.ROUND_PRE_FLOP

    player = Player(
        user_id=7,
        mention_markdown='@seven',
        wallet=MagicMock(),
        ready_message_id='ready-7',
    )
    game.add_player(player, seat_index=0)
    player.cards = [Card('A♣'), Card('K♥')]
    player.anchor_message = None

    run(viewer.update_player_anchors_and_keyboards(game))

    viewer._update_message.assert_not_awaited()
    assert player.anchor_message is None


def test_clear_all_player_anchors_deletes_messages():
    viewer = PokerBotViewer(bot=MagicMock())
    viewer.delete_message = AsyncMock()

    game = Game()
    game.chat_id = -321

    player = Player(
        user_id=9,
        mention_markdown='@player',
        wallet=MagicMock(),
        ready_message_id='ready',
    )
    game.add_player(player, seat_index=0)
    player.anchor_message = (game.chat_id, 404)
    game.message_ids_to_delete.append(404)

    run(viewer.clear_all_player_anchors(game))

    viewer.delete_message.assert_awaited_once_with(chat_id=game.chat_id, message_id=404)
    assert player.anchor_message is None
    assert player.anchor_role == 'بازیکن'
    assert player.role_label == 'بازیکن'
    assert 404 not in game.message_ids_to_delete


def test_send_player_role_anchors_attaches_group_keyboard():
    viewer = PokerBotViewer(bot=MagicMock())

    game = Game()
    game.chat_id = -555
    game.state = GameState.ROUND_PRE_FLOP

    player = Player(
        user_id=42,
        mention_markdown='@hero',
        wallet=MagicMock(),
        ready_message_id='ready-hero',
    )
    player.cards = [Card('A♠'), Card('K♦')]
    game.add_player(player, seat_index=0)

    viewer.send_message_return_id = AsyncMock(return_value=321)

    run(viewer.send_player_role_anchors(game=game, chat_id=game.chat_id))

    viewer.send_message_return_id.assert_awaited_once()
    call_kwargs = viewer.send_message_return_id.await_args.kwargs
    assert call_kwargs['chat_id'] == game.chat_id
    assert call_kwargs['request_category'] == RequestCategory.ANCHOR
    assert isinstance(call_kwargs['reply_markup'], ReplyKeyboardMarkup)
    keyboard = call_kwargs['reply_markup']
    assert _row_texts(keyboard.keyboard[0]) == ['A♠️', 'K♦️']
    assert _row_texts(keyboard.keyboard[1]) == ['⬜️']
    stage_row = _row_texts(keyboard.keyboard[2])
    assert stage_row[0].startswith('✅')

    assert player.anchor_message == (game.chat_id, 321)
    assert player.anchor_keyboard_signature
    assert player.private_keyboard_message is None
    assert player.private_keyboard_signature is None


def test_build_player_cards_keyboard_layout():
    markup = build_player_cards_keyboard(
        hole_cards=['A♠', 'K♥'],
        community_cards=['❔', '5♦', '❔', '❔', '❔'],
        current_stage='FLOP',
    )

    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.resize_keyboard is True
    assert markup.one_time_keyboard is False
    assert markup.selective is False
    assert _row_texts(markup.keyboard[0]) == ['A♠', 'K♥']
    assert _row_texts(markup.keyboard[1]) == ['❔', '5♦', '❔', '❔', '❔']
    stage_row = _row_texts(markup.keyboard[2])
    assert stage_row[0] == 'پری فلاپ'
    assert stage_row[1].startswith('✅')
    assert stage_row[2] == 'ترن'
    assert stage_row[3] == 'ریور'


@pytest.mark.parametrize("chat_id", (-123, "-123"))
def test_update_message_resends_reply_keyboard_and_deletes_previous(chat_id):
    viewer = PokerBotViewer(bot=MagicMock())
    messenger = MagicMock()
    messenger.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=777)
    )
    messenger.edit_message_text = AsyncMock()
    messenger.delete_message = AsyncMock(return_value=True)
    viewer._messenger = messenger

    keyboard = build_player_cards_keyboard(
        hole_cards=['A♠️', 'K♦️'],
        community_cards=['Q♥️', 'J♣️', '9♠️'],
        current_stage='ROUND_FLOP',
    )

    result = run(
        viewer._update_message(
            chat_id=chat_id,
            message_id=555,
            text='به‌روزرسانی',
            reply_markup=keyboard,
            force_send=True,
            request_category=RequestCategory.ANCHOR,
        )
    )

    assert result == 777
    messenger.send_message.assert_awaited_once()
    messenger.edit_message_text.assert_not_awaited()
    messenger.delete_message.assert_awaited_once()
    delete_call = messenger.delete_message.await_args
    assert delete_call.kwargs['chat_id'] == int(chat_id)
    assert delete_call.kwargs['message_id'] == 555


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
