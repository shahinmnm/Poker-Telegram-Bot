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
from pokerapp.pokerbotmodel import PokerBotModel, RoundRateModel, WalletManagerModel


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
    view.send_cards = AsyncMock(side_effect=["msg-1", "msg-2"])
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
    assert player.cards_keyboard_message_id == "msg-2"
    assert view.delete_message.await_count == 1
    delete_args = view.delete_message.await_args_list[0].args
    assert delete_args == (chat_id, "msg-1")
    assert "msg-2" in game.message_ids_to_delete
    assert "msg-1" not in game.message_ids_to_delete


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
    model._safe_edit_message_text = AsyncMock(return_value=222)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 5})

    await model._auto_start_tick(context)

    assert game.ready_message_main_id == 222
    assert game.ready_message_main_text == "prompt"
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert context.chat_data["start_countdown"] == 4


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
    model._safe_edit_message_text = AsyncMock(return_value=555)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 10})

    await model._auto_start_tick(context)

    assert game.ready_message_main_id == 555
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data["start_countdown"] == 9
