#!/usr/bin/env python3

import asyncio
import unittest
from types import SimpleNamespace
from typing import Tuple
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest

from pokerapp.cards import Cards, Card
from pokerapp.config import Config
from pokerapp.entities import Money, Player, Game
from pokerapp.pokerbotmodel import (
    PokerBotModel,
    RoundRateModel,
    WalletManagerModel,
    KEY_CHAT_DATA_GAME,
)
from telegram.error import BadRequest


HANDS_FILE = "./tests/hands.txt"


def with_cards(p: Player) -> Tuple[Player, Cards]:
    return (p, [Card("6♥"), Card("A♥"), Card("A♣"), Card("A♠")])


class TestRoundRateModel(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(TestRoundRateModel, self).__init__(*args, **kwargs)
        self._user_id = 0
        self._round_rate = RoundRateModel()
        self._kv = fakeredis.FakeRedis()

    def _next_player(self, game: Game, autorized: Money) -> Player:
        self._user_id += 1
        wallet_manager = WalletManagerModel(self._user_id, kv=self._kv)
        wallet_manager.authorize_all("clean_wallet_game")
        wallet_manager.inc(autorized)
        wallet_manager.authorize(game.id, autorized)
        game.pot += autorized
        p = Player(
            user_id=self._user_id,
            mention_markdown="@test",
            wallet=wallet_manager,
            ready_message_id="",
        )
        game.players.append(p)

        return p

    def _approve_all(self, game: Game) -> None:
        for player in game.players:
            player.wallet.approve(game.id)

    def assert_authorized_money_zero(self, game_id: str, *players: Player):
        for (i, p) in enumerate(players):
            authorized = p.wallet.authorized_money(game_id=game_id)
            self.assertEqual(0, authorized, "player[" + str(i) + "]")

    def test_finish_rate_single_winner(self):
        g = Game()
        winner = self._next_player(g, 50)
        loser = self._next_player(g, 50)

        self._round_rate.finish_rate(g, player_scores={
            1: [with_cards(winner)],
            0: [with_cards(loser)],
        })
        self._approve_all(g)

        self.assertAlmostEqual(100, winner.wallet.value(), places=1)
        self.assertAlmostEqual(0, loser.wallet.value(), places=1)
        self.assert_authorized_money_zero(g.id, winner, loser)

    def test_finish_rate_two_winners(self):
        g = Game()
        first_winner = self._next_player(g, 50)
        second_winner = self._next_player(g, 50)
        loser = self._next_player(g, 100)

        self._round_rate.finish_rate(g, player_scores={
            1: [with_cards(first_winner), with_cards(second_winner)],
            0: [with_cards(loser)],
        })
        self._approve_all(g)

        self.assertAlmostEqual(100, first_winner.wallet.value(), places=1)
        self.assertAlmostEqual(100, second_winner.wallet.value(), places=1)
        self.assertAlmostEqual(0, loser.wallet.value(), places=1)
        self.assert_authorized_money_zero(
            g.id,
            first_winner,
            second_winner,
            loser,
        )

    def test_finish_rate_all_in_one_extra_winner(self):
        g = Game()
        first_winner = self._next_player(g, 15)  # All in.
        second_winner = self._next_player(g, 5)  # All in.
        extra_winner = self._next_player(g, 90)  # All in.
        loser = self._next_player(g, 90)  # Call.

        self._round_rate.finish_rate(g, player_scores={
            2: [with_cards(first_winner), with_cards(second_winner)],
            1: [with_cards(extra_winner)],
            0: [with_cards(loser)],
        })
        self._approve_all(g)

        # authorized * len(players)
        self.assertAlmostEqual(60, first_winner.wallet.value(), places=1)
        # authorized * len(players)
        self.assertAlmostEqual(20, second_winner.wallet.value(), places=1)
        # pot - winners
        self.assertAlmostEqual(120, extra_winner.wallet.value(), places=1)

        self.assertAlmostEqual(0, loser.wallet.value(), places=1)

        self.assert_authorized_money_zero(
            g.id, first_winner, second_winner, extra_winner, loser,
        )

    def test_finish_rate_all_winners(self):
        g = Game()
        first_winner = self._next_player(g, 50)
        second_winner = self._next_player(g, 100)
        third_winner = self._next_player(g, 150)

        self._round_rate.finish_rate(g, player_scores={
            1: [
                with_cards(first_winner),
                with_cards(second_winner),
                with_cards(third_winner),
            ],
        })
        self._approve_all(g)

        self.assertAlmostEqual(50, first_winner.wallet.value(), places=1)
        self.assertAlmostEqual(
            100, second_winner.wallet.value(), places=1)
        self.assertAlmostEqual(150, third_winner.wallet.value(), places=1)
        self.assert_authorized_money_zero(
            g.id, first_winner, second_winner, third_winner,
        )

    def test_finish_rate_all_in_all(self):
        g = Game()

        first_winner = self._next_player(g, 3)  # All in.
        second_winner = self._next_player(g, 60)  # All in.
        third_loser = self._next_player(g, 10)  # All in.
        fourth_loser = self._next_player(g, 10)  # All in.

        self._round_rate.finish_rate(g, player_scores={
            3: [with_cards(first_winner), with_cards(second_winner)],
            2: [with_cards(third_loser)],
            1: [with_cards(fourth_loser)],
        })
        self._approve_all(g)

        # pot * (autorized / winners_authorized)
        self.assertAlmostEqual(4, first_winner.wallet.value(), places=1)
        self.assertAlmostEqual(79, second_winner.wallet.value(), places=1)

        self.assertAlmostEqual(0, third_loser.wallet.value(), places=1)
        self.assertAlmostEqual(0, fourth_loser.wallet.value(), places=1)

        self.assert_authorized_money_zero(
            g.id, first_winner, second_winner, third_loser, fourth_loser
        )


if __name__ == '__main__':
    unittest.main()


def _build_model_with_game():
    view = MagicMock()
    view.send_cards = AsyncMock(return_value=None)
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    game = Game()
    player = Player(
        user_id=123,
        mention_markdown="[Player](tg://user?id=123)",
        wallet=MagicMock(),
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)
    player.cards = [Card("A♠"), Card("K♦")]
    return model, game, player, view


def test_add_cards_to_table_sends_plain_message_without_keyboard():
    model, game, player, view = _build_model_with_game()
    chat_id = -300
    view.send_cards = AsyncMock(return_value=None)
    asyncio.run(model._divide_cards(game, chat_id))

    view.send_message_return_id = AsyncMock(return_value=101)
    view.delete_message = AsyncMock()

    game.remain_cards = [Card("2♣"), Card("3♦"), Card("4♥")]

    view.send_cards.reset_mock()
    asyncio.run(model.add_cards_to_table(3, game, chat_id, "🃏 فلاپ"))

    assert view.send_message_return_id.await_count == 1
    assert view.send_cards.await_count == 1

    send_args = view.send_message_return_id.await_args
    assert send_args.args[0] == chat_id
    assert send_args.args[1] == "🃏 فلاپ"
    assert send_args.kwargs.get("reply_markup") is None

    call_kwargs = view.send_cards.await_args.kwargs
    assert call_kwargs["hide_hand_text"] is True
    assert call_kwargs["table_cards"] == game.cards_table
    assert call_kwargs["ready_message_id"] == player.ready_message_id
    assert "message_id" not in call_kwargs

    assert game.board_message_id == 101
    assert 101 in game.message_ids_to_delete
    assert view.delete_message.await_count == 0


def test_add_cards_to_table_replaces_player_keyboard_message():
    model, game, player, view = _build_model_with_game()
    chat_id = -301
    view.send_cards = AsyncMock(side_effect=["msg-1", "msg-1"])
    view.delete_message = AsyncMock()
    view.send_message_return_id = AsyncMock(return_value=222)

    asyncio.run(model._divide_cards(game, chat_id))

    assert player.cards_keyboard_message_id == "msg-1"
    assert "msg-1" in game.message_ids_to_delete

    asyncio.run(
        model.add_cards_to_table(
            0,
            game,
            chat_id,
            "🃏 میز",
        )
    )

    assert view.send_cards.await_count == 2
    second_call_kwargs = view.send_cards.await_args_list[1].kwargs
    assert second_call_kwargs["message_id"] == "msg-1"
    assert player.cards_keyboard_message_id == "msg-1"
    assert view.delete_message.await_count == 0
    assert game.message_ids_to_delete.count("msg-1") == 1


def test_divide_cards_sends_keyboard_without_tracking_message_id():
    model, game, player, view = _build_model_with_game()
    chat_id = -400
    view.send_cards = AsyncMock(return_value=None)

    asyncio.run(model._divide_cards(game, chat_id))

    assert view.send_cards.await_count == 1
    call_kwargs = view.send_cards.await_args.kwargs
    assert "message_id" not in call_kwargs
    assert call_kwargs["ready_message_id"] == player.ready_message_id
    assert game.message_ids == {}


def test_clear_game_messages_deletes_player_card_messages():
    model, game, player, view = _build_model_with_game()
    chat_id = -500
    view.delete_message = AsyncMock()
    view.send_cards = AsyncMock(return_value="keyboard-7")

    asyncio.run(model._divide_cards(game, chat_id))

    game.message_ids_to_delete.extend([888, 999])
    game.board_message_id = 321
    game.turn_message_id = 654

    asyncio.run(model._clear_game_messages(game, chat_id))

    deleted_pairs = {call.args for call in view.delete_message.await_args_list}
    assert (chat_id, "keyboard-7") in deleted_pairs
    assert (chat_id, 321) in deleted_pairs
    assert (chat_id, 654) in deleted_pairs
    assert (chat_id, 888) in deleted_pairs
    assert (chat_id, 999) in deleted_pairs
    assert game.message_ids == {}
    assert game.message_ids_to_delete == []


@pytest.mark.asyncio
async def test_auto_start_tick_persists_replacement_message():
    chat_id = -777
    view = MagicMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = 111
    game.ready_message_main_text = "prompt"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    view.edit_message_reply_markup = AsyncMock(return_value=False)
    model._safe_edit_message_text = AsyncMock(return_value=222)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 5})

    await model._auto_start_tick(context)

    view.edit_message_reply_markup.assert_awaited_once()
    assert game.ready_message_main_id == 222
    assert game.ready_message_main_text == "prompt"
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert context.chat_data["start_countdown"] == 4
    assert context.chat_data["start_countdown_last_rendered"] == 4
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_skips_save_when_message_unchanged():
    chat_id = -778
    view = MagicMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = 555
    game.ready_message_main_text = "prompt"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    view.edit_message_reply_markup = AsyncMock(return_value=True)
    model._safe_edit_message_text = AsyncMock(return_value=555)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(
        job=job,
        chat_data={
            "start_countdown": 12,
            "start_countdown_last_rendered": 12,
        },
    )

    await model._auto_start_tick(context)

    view.edit_message_reply_markup.assert_not_awaited()
    model._safe_edit_message_text.assert_not_awaited()
    assert game.ready_message_main_id == 555
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data["start_countdown"] == 11
    assert context.chat_data["start_countdown_last_rendered"] == 12
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_decrements_before_rendering_countdown():
    chat_id = -779
    view = MagicMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = 777
    game.ready_message_main_text = "prompt"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    view.edit_message_reply_markup = AsyncMock(return_value=True)
    model._safe_edit_message_text = AsyncMock(return_value=777)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 60})

    await model._auto_start_tick(context)

    view.edit_message_reply_markup.assert_awaited_once()
    call_args = view.edit_message_reply_markup.await_args
    button_text = call_args.args[2].inline_keyboard[0][1].text
    assert button_text == "شروع بازی (59)"
    assert call_args.args[1] == 777
    model._safe_edit_message_text.assert_not_awaited()
    assert context.chat_data["start_countdown"] == 59
    assert context.chat_data["start_countdown_last_rendered"] == 59
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_renders_on_multiple_of_four():
    chat_id = -780
    view = MagicMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = 888
    game.ready_message_main_text = "prompt"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    view.edit_message_reply_markup = AsyncMock(return_value=True)
    model._safe_edit_message_text = AsyncMock(return_value=888)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(
        job=job,
        chat_data={
            "start_countdown": 9,
            "start_countdown_last_rendered": 12,
        },
    )

    await model._auto_start_tick(context)

    view.edit_message_reply_markup.assert_awaited_once()
    model._safe_edit_message_text.assert_not_awaited()
    assert context.chat_data["start_countdown"] == 8
    assert context.chat_data["start_countdown_last_rendered"] == 8
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_handles_bad_request_without_replacement():
    chat_id = -781
    view = MagicMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = 999
    game.ready_message_main_text = "prompt"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    view.edit_message_reply_markup = AsyncMock(
        side_effect=BadRequest("Message is not modified")
    )
    model._safe_edit_message_text = AsyncMock()

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 15})

    await model._auto_start_tick(context)

    view.edit_message_reply_markup.assert_awaited_once()
    model._safe_edit_message_text.assert_not_awaited()
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data["start_countdown"] == 14
    assert context.chat_data["start_countdown_last_rendered"] == 14
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_start_game_assigns_blinds_to_occupied_seats():
    view = MagicMock()
    view.send_cards = AsyncMock()
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    model._divide_cards = AsyncMock()
    model._send_turn_message = AsyncMock()
    model._round_rate._set_player_blind = AsyncMock()

    game = Game()
    game.dealer_index = 0

    wallet_a = MagicMock()
    wallet_a.value.return_value = 1000
    wallet_a.authorize = MagicMock()
    player_a = Player(
        user_id=1,
        mention_markdown="@a",
        wallet=wallet_a,
        ready_message_id="ready-a",
    )
    game.add_player(player_a, seat_index=0)

    wallet_b = MagicMock()
    wallet_b.value.return_value = 1000
    wallet_b.authorize = MagicMock()
    player_b = Player(
        user_id=2,
        mention_markdown="@b",
        wallet=wallet_b,
        ready_message_id="ready-b",
    )
    game.add_player(player_b, seat_index=3)

    context = SimpleNamespace(chat_data={}, job_queue=None)
    chat_id = -123

    await model._start_game(context, game, chat_id)

    assert game.dealer_index == 3
    assert game.small_blind_index == 3
    assert game.big_blind_index == 0
    assert game.get_player_by_seat(game.small_blind_index) is player_b
    assert game.get_player_by_seat(game.big_blind_index) is player_a
    assert game.current_player_index == game.small_blind_index

    blind_players = {
        call.args[1].user_id for call in model._round_rate._set_player_blind.await_args_list
    }
    assert blind_players == {1, 2}


def test_send_turn_message_replaces_previous_message():
    model, game, player, view = _build_model_with_game()
    chat_id = -601
    player.wallet.value.return_value = 450
    game.turn_message_id = 111
    game.last_actions = ["action"]

    view.send_turn_actions = AsyncMock(return_value=222)
    view.delete_message = AsyncMock()

    asyncio.run(model._send_turn_message(game, player, chat_id))

    assert view.send_turn_actions.await_count == 1
    call = view.send_turn_actions.await_args
    assert call.args == (chat_id, game, player, 450)
    assert call.kwargs["recent_actions"] == game.last_actions

    view.delete_message.assert_awaited_once_with(chat_id, 111)
    assert game.turn_message_id == 222


def test_send_turn_message_keeps_previous_when_new_message_missing():
    model, game, player, view = _build_model_with_game()
    chat_id = -602
    player.wallet.value.return_value = 320
    game.turn_message_id = 333
    game.last_actions = ["action"]

    view.send_turn_actions = AsyncMock(return_value=None)
    view.delete_message = AsyncMock()

    asyncio.run(model._send_turn_message(game, player, chat_id))

    view.send_turn_actions.assert_awaited_once()
    view.delete_message.assert_not_awaited()
    assert game.turn_message_id == 333
