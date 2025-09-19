#!/usr/bin/env python3

import asyncio
import datetime
import logging
import unittest
from types import SimpleNamespace
from typing import List, Tuple, Optional
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import fakeredis.aioredis
import pytest

from pokerapp.cards import Cards, Card
from pokerapp.config import Config
from pokerapp.entities import Money, Player, Game, PlayerState, GameState
from pokerapp.pokerbotmodel import (
    PokerBotModel,
    RoundRateModel,
    WalletManagerModel,
    KEY_CHAT_DATA_GAME,
    KEY_START_COUNTDOWN_LAST_TEXT,
    KEY_START_COUNTDOWN_LAST_TIMESTAMP,
    KEY_STOP_REQUEST,
    STOP_CONFIRM_CALLBACK,
    STOP_RESUME_CALLBACK,
)
from telegram.error import BadRequest
from telegram import InlineKeyboardMarkup


HANDS_FILE = "./tests/hands.txt"


def with_cards(p: Player) -> Tuple[Player, Cards]:
    return (p, [Card("6♥"), Card("A♥"), Card("A♣"), Card("A♠")])


def make_wallet_mock(value: Optional[int] = None):
    wallet = MagicMock()
    wallet.value = AsyncMock(return_value=value)
    wallet.authorize = AsyncMock()
    wallet.inc = AsyncMock()
    wallet.cancel = AsyncMock()
    wallet.approve = AsyncMock()
    wallet.authorized_money = AsyncMock()
    return wallet


def _prepare_view_mock(view: MagicMock) -> MagicMock:
    view.edit_message_text = AsyncMock(return_value=None)
    view.send_message_return_id = AsyncMock(return_value=None)
    view.send_message = AsyncMock()
    return view


class TestRoundRateModel(unittest.IsolatedAsyncioTestCase):
    def __init__(self, *args, **kwargs):
        super(TestRoundRateModel, self).__init__(*args, **kwargs)
        self._user_id = 0
        self._round_rate = RoundRateModel()
        self._kv = fakeredis.aioredis.FakeRedis()

    async def asyncSetUp(self):
        await self._kv.flushall()

    async def _next_player(self, game: Game, autorized: Money) -> Player:
        self._user_id += 1
        wallet_manager = WalletManagerModel(self._user_id, kv=self._kv)
        await wallet_manager.authorize_all('clean_wallet_game')
        await wallet_manager.inc(autorized)
        await wallet_manager.authorize(game.id, autorized)
        game.pot += autorized
        p = Player(
            user_id=self._user_id,
            mention_markdown='@test',
            wallet=wallet_manager,
            ready_message_id='',
        )
        game.players.append(p)
        return p

    async def _approve_all(self, game: Game) -> None:
        for player in game.players:
            await player.wallet.approve(game.id)

    async def assert_authorized_money_zero(self, game_id: str, *players: Player):
        for (i, p) in enumerate(players):
            authorized = await p.wallet.authorized_money(game_id=game_id)
            self.assertEqual(0, authorized, 'player[' + str(i) + ']')

    async def test_finish_rate_single_winner(self):
        g = Game()
        winner = await self._next_player(g, 50)
        loser = await self._next_player(g, 50)

        await self._round_rate.finish_rate(g, player_scores={
            1: [with_cards(winner)],
            0: [with_cards(loser)],
        })
        await self._approve_all(g)

        self.assertAlmostEqual(100, await winner.wallet.value(), places=1)
        self.assertAlmostEqual(0, await loser.wallet.value(), places=1)
        await self.assert_authorized_money_zero(g.id, winner, loser)

    async def test_finish_rate_two_winners(self):
        g = Game()
        first_winner = await self._next_player(g, 50)
        second_winner = await self._next_player(g, 50)
        loser = await self._next_player(g, 100)

        await self._round_rate.finish_rate(g, player_scores={
            1: [with_cards(first_winner), with_cards(second_winner)],
            0: [with_cards(loser)],
        })
        await self._approve_all(g)

        self.assertAlmostEqual(100, await first_winner.wallet.value(), places=1)
        self.assertAlmostEqual(100, await second_winner.wallet.value(), places=1)
        self.assertAlmostEqual(0, await loser.wallet.value(), places=1)
        await self.assert_authorized_money_zero(
            g.id,
            first_winner,
            second_winner,
            loser,
        )

    async def test_finish_rate_all_in_one_extra_winner(self):
        g = Game()
        first_winner = await self._next_player(g, 15)  # All in.
        second_winner = await self._next_player(g, 5)  # All in.
        extra_winner = await self._next_player(g, 90)  # All in.
        loser = await self._next_player(g, 90)  # Call.

        await self._round_rate.finish_rate(g, player_scores={
            2: [with_cards(first_winner), with_cards(second_winner)],
            1: [with_cards(extra_winner)],
            0: [with_cards(loser)],
        })
        await self._approve_all(g)

        self.assertAlmostEqual(60, await first_winner.wallet.value(), places=1)
        self.assertAlmostEqual(20, await second_winner.wallet.value(), places=1)
        self.assertAlmostEqual(120, await extra_winner.wallet.value(), places=1)
        self.assertAlmostEqual(0, await loser.wallet.value(), places=1)

        await self.assert_authorized_money_zero(
            g.id, first_winner, second_winner, extra_winner, loser,
        )

    async def test_finish_rate_all_winners(self):
        g = Game()
        first_winner = await self._next_player(g, 50)
        second_winner = await self._next_player(g, 100)
        third_winner = await self._next_player(g, 150)

        await self._round_rate.finish_rate(g, player_scores={
            1: [
                with_cards(first_winner),
                with_cards(second_winner),
                with_cards(third_winner),
            ],
        })
        await self._approve_all(g)

        self.assertAlmostEqual(50, await first_winner.wallet.value(), places=1)
        self.assertAlmostEqual(
            100, await second_winner.wallet.value(), places=1)
        self.assertAlmostEqual(150, await third_winner.wallet.value(), places=1)
        await self.assert_authorized_money_zero(
            g.id, first_winner, second_winner, third_winner,
        )

    async def test_finish_rate_all_in_all(self):
        g = Game()

        first_winner = await self._next_player(g, 3)  # All in.
        second_winner = await self._next_player(g, 60)  # All in.
        third_loser = await self._next_player(g, 10)  # All in.
        fourth_loser = await self._next_player(g, 10)  # All in.

        await self._round_rate.finish_rate(g, player_scores={
            3: [with_cards(first_winner), with_cards(second_winner)],
            2: [with_cards(third_loser)],
            1: [with_cards(fourth_loser)],
        })
        await self._approve_all(g)

        self.assertAlmostEqual(4, await first_winner.wallet.value(), places=1)
        self.assertAlmostEqual(79, await second_winner.wallet.value(), places=1)

        self.assertAlmostEqual(0, await third_loser.wallet.value(), places=1)
        self.assertAlmostEqual(0, await fourth_loser.wallet.value(), places=1)

        await self.assert_authorized_money_zero(
            g.id, first_winner, second_winner, third_loser, fourth_loser
        )

if __name__ == '__main__':
    unittest.main()


def _build_model_with_game():
    view = _prepare_view_mock(MagicMock())
    view.send_cards = AsyncMock(return_value=None)
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    game = Game()
    wallet = make_wallet_mock()
    player = Player(
        user_id=123,
        mention_markdown="[Player](tg://user?id=123)",
        wallet=wallet,
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)
    player.cards = [Card("A♠"), Card("K♦")]
    return model, game, player, view


@pytest.mark.asyncio
async def test_request_stop_creates_vote_prompt():
    chat_id = -1200
    view = _prepare_view_mock(MagicMock())
    view.send_message_return_id = AsyncMock(return_value=77)
    view.edit_message_text = AsyncMock()
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)

    game = Game()
    wallet_a = make_wallet_mock()
    player_a = Player(
        user_id=1,
        mention_markdown="@player_a",
        wallet=wallet_a,
        ready_message_id="ra",
    )
    game.add_player(player_a, seat_index=0)

    wallet_b = make_wallet_mock()
    player_b = Player(
        user_id=2,
        mention_markdown="@player_b",
        wallet=wallet_b,
        ready_message_id="rb",
    )
    player_b.state = PlayerState.ALL_IN
    game.add_player(player_b, seat_index=1)

    context = SimpleNamespace(chat_data={KEY_CHAT_DATA_GAME: game})

    await model._request_stop(context, game, chat_id, requester_id=player_a.user_id)

    assert KEY_STOP_REQUEST in context.chat_data
    stop_request = context.chat_data[KEY_STOP_REQUEST]
    assert stop_request["message_id"] == 77
    assert stop_request["votes"] == {player_a.user_id}
    assert set(stop_request["active_players"]) == {player_a.user_id, player_b.user_id}

    send_args = view.send_message_return_id.await_args
    assert send_args.args[0] == chat_id
    message_text = send_args.args[1]
    assert "آراء تأیید" in message_text
    markup = send_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = markup.inline_keyboard[0]
    assert buttons[0].callback_data == STOP_CONFIRM_CALLBACK
    assert buttons[1].callback_data == STOP_RESUME_CALLBACK


@pytest.mark.asyncio
async def test_confirm_stop_vote_triggers_cancel_on_majority():
    chat_id = -1300
    view = _prepare_view_mock(MagicMock())
    view.send_message_return_id = AsyncMock(return_value=88)
    view.edit_message_text = AsyncMock(return_value=88)
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)

    game = Game()
    wallet_a = make_wallet_mock()
    player_a = Player(
        user_id=10,
        mention_markdown="@p10",
        wallet=wallet_a,
        ready_message_id="r10",
    )
    game.add_player(player_a, seat_index=0)

    wallet_b = make_wallet_mock()
    player_b = Player(
        user_id=11,
        mention_markdown="@p11",
        wallet=wallet_b,
        ready_message_id="r11",
    )
    game.add_player(player_b, seat_index=1)

    context = SimpleNamespace(chat_data={KEY_CHAT_DATA_GAME: game})

    await model._request_stop(context, game, chat_id, requester_id=player_a.user_id)

    model._cancel_hand = AsyncMock()

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=SimpleNamespace(
            data=STOP_CONFIRM_CALLBACK,
            from_user=SimpleNamespace(id=player_b.user_id),
        ),
    )

    await model.confirm_stop_vote(update, context)

    model._cancel_hand.assert_awaited_once()
    args = model._cancel_hand.await_args.args
    assert args[0] is game
    assert args[1] == chat_id


@pytest.mark.asyncio
async def test_cancel_hand_refunds_wallets_and_announces():
    chat_id = -1400
    view = _prepare_view_mock(MagicMock())
    view.send_message_return_id = AsyncMock()
    view.edit_message_text = AsyncMock(return_value=99)
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)

    game = Game()
    wallet_a = make_wallet_mock()
    wallet_b = make_wallet_mock()
    player_a = Player(
        user_id=20,
        mention_markdown="@p20",
        wallet=wallet_a,
        ready_message_id="r20",
    )
    game.add_player(player_a, seat_index=0)
    player_b = Player(
        user_id=21,
        mention_markdown="@p21",
        wallet=wallet_b,
        ready_message_id="r21",
    )
    game.add_player(player_b, seat_index=1)
    game.pot = 300

    stop_request = {
        "game_id": game.id,
        "message_id": 99,
        "active_players": [player_a.user_id, player_b.user_id],
        "votes": {player_a.user_id, player_b.user_id},
        "manager_override": False,
    }

    context = SimpleNamespace(chat_data={KEY_STOP_REQUEST: stop_request})

    original_game_id = game.id

    await model._cancel_hand(game, chat_id, context, stop_request)

    wallet_a.cancel.assert_awaited_once_with(original_game_id)
    wallet_b.cancel.assert_awaited_once_with(original_game_id)
    assert game.pot == 0
    assert KEY_STOP_REQUEST not in context.chat_data
    assert view.edit_message_text.await_count == 1
    assert view.send_message.await_count == 1
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert game.state == GameState.INITIAL


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
async def test_auto_start_tick_updates_text_with_countdown():
    chat_id = -777
    view = _prepare_view_mock(MagicMock())
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
    model._safe_edit_message_text = AsyncMock(return_value=111)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 5})

    await model._auto_start_tick(context)

    model._safe_edit_message_text.assert_awaited_once()
    call_args = model._safe_edit_message_text.await_args
    rendered_text = call_args.args[2]
    assert "4 ثانیه" in rendered_text
    assert context.chat_data["start_countdown"] == 4
    assert game.ready_message_main_text == rendered_text
    assert context.chat_data[KEY_START_COUNTDOWN_LAST_TEXT] == rendered_text
    last_timestamp = context.chat_data[KEY_START_COUNTDOWN_LAST_TIMESTAMP]
    assert isinstance(last_timestamp, datetime.datetime)
    assert last_timestamp.tzinfo is not None
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_finishes_without_rate_limit_tail_sleep(monkeypatch):
    chat_id = -779
    view = _prepare_view_mock(MagicMock())
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

    model._safe_edit_message_text = AsyncMock(return_value=111)

    sleep_calls: List[float] = []

    async def fake_sleep(duration: float) -> None:
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 3})

    await model._auto_start_tick(context)

    assert sleep_calls == []
    model._safe_edit_message_text.assert_awaited_once()
    assert context.chat_data["start_countdown"] == 2
    assert game.ready_message_main_text != "prompt"
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_throttles_and_resumes_updates():
    chat_id = -775
    view = _prepare_view_mock(MagicMock())
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
    model._safe_edit_message_text = AsyncMock(return_value=111)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 5})

    await model._auto_start_tick(context)
    assert model._safe_edit_message_text.await_count == 1
    first_text = context.chat_data[KEY_START_COUNTDOWN_LAST_TEXT]
    assert context.chat_data["start_countdown"] == 4

    await model._auto_start_tick(context)
    assert model._safe_edit_message_text.await_count == 1
    assert context.chat_data["start_countdown"] == 3
    assert context.chat_data[KEY_START_COUNTDOWN_LAST_TEXT] == first_text

    context.chat_data[KEY_START_COUNTDOWN_LAST_TIMESTAMP] -= datetime.timedelta(seconds=10)

    await model._auto_start_tick(context)
    assert model._safe_edit_message_text.await_count == 2
    assert context.chat_data["start_countdown"] == 2
    assert context.chat_data[KEY_START_COUNTDOWN_LAST_TEXT] != first_text
    job.schedule_removal.assert_not_called()
    table_manager.save_game.assert_not_awaited()
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_forces_update_when_timer_hits_zero():
    chat_id = -774
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = 222
    game.ready_message_main_text = "prompt"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    model._safe_edit_message_text = AsyncMock(return_value=222)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    previous_timestamp = datetime.datetime.now(datetime.timezone.utc)
    context = SimpleNamespace(
        job=job,
        chat_data={
            "start_countdown": 1,
            KEY_START_COUNTDOWN_LAST_TEXT: "old",
            KEY_START_COUNTDOWN_LAST_TIMESTAMP: previous_timestamp,
        },
    )

    await model._auto_start_tick(context)

    model._safe_edit_message_text.assert_awaited_once()
    final_text = model._safe_edit_message_text.await_args.args[2]
    assert "بازی در حال شروع است" in final_text
    assert context.chat_data["start_countdown"] == 0
    assert context.chat_data[KEY_START_COUNTDOWN_LAST_TEXT] == final_text
    assert context.chat_data[KEY_START_COUNTDOWN_LAST_TIMESTAMP] >= previous_timestamp
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_starts_game_and_cleans_state():
    chat_id = -776
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    model._start_game = AsyncMock()

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(
        job=job,
        chat_data={
            "start_countdown": 0,
            "start_countdown_job": object(),
        },
    )

    await model._auto_start_tick(context)

    model._start_game.assert_awaited_once_with(context, game, chat_id)
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    job.schedule_removal.assert_called_once()
    assert "start_countdown_job" not in context.chat_data
    assert "start_countdown" not in context.chat_data
    assert KEY_START_COUNTDOWN_LAST_TEXT not in context.chat_data
    assert KEY_START_COUNTDOWN_LAST_TIMESTAMP not in context.chat_data
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_creates_message_when_missing():
    chat_id = -778
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = None
    game.ready_message_main_text = ""
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    model._safe_edit_message_text = AsyncMock(return_value=999)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 3})

    await model._auto_start_tick(context)

    model._safe_edit_message_text.assert_awaited_once()
    assert game.ready_message_main_id == 999
    assert "2 ثانیه" in game.ready_message_main_text
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert context.chat_data["start_countdown"] == 2
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_auto_start_tick_recreates_missing_message_after_bad_request(caplog):
    chat_id = -779
    view = _prepare_view_mock(MagicMock())
    view.edit_message_text = AsyncMock(
        side_effect=BadRequest("message to edit not found")
    )
    view.send_message_return_id = AsyncMock(return_value=4242)
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = 555
    game.ready_message_main_text = "old"
    game.message_ids_to_delete.append(555)
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(job=job, chat_data={"start_countdown": 2})

    caplog.set_level(logging.WARNING)

    await model._auto_start_tick(context)

    assert view.edit_message_text.await_count == 1
    assert view.send_message_return_id.await_count == 1
    send_args = view.send_message_return_id.await_args
    assert send_args.args[0] == chat_id
    new_text = send_args.args[1]
    assert "1 ثانیه" in new_text
    assert send_args.kwargs.get("reply_markup") is not None

    records = [
        record
        for record in caplog.records
        if getattr(record, "context", None) == "countdown"
        and getattr(record, "message_id", None) == 555
    ]
    assert records, "Expected BadRequest warning log for countdown context"
    log_record = records[0]
    assert log_record.levelno == logging.WARNING
    assert "BadRequest" in log_record.message
    assert game.ready_message_main_id == 4242
    assert game.ready_message_main_text == new_text
    assert 555 not in game.message_ids_to_delete
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert context.chat_data["start_countdown"] == 1
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game


@pytest.mark.asyncio
async def test_safe_edit_message_text_logs_bad_request(caplog):
    chat_id = -111
    message_id = 222
    text = "x" * 150
    view = _prepare_view_mock(MagicMock())
    view.edit_message_text = AsyncMock(
        side_effect=BadRequest("message to edit not found")
    )
    view.send_message_return_id = AsyncMock(return_value=999)
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)

    caplog.set_level(logging.WARNING)

    new_id = await model._safe_edit_message_text(
        chat_id,
        message_id,
        text,
        log_context="countdown",
    )

    assert new_id == 999
    assert view.edit_message_text.await_count == 1
    assert view.send_message_return_id.await_count == 1

    log_records = [
        record
        for record in caplog.records
        if getattr(record, "message_id", None) == message_id
    ]
    assert log_records
    record = log_records[0]
    assert record.levelno == logging.WARNING
    assert record.context == "countdown"
    assert record.chat_id == chat_id
    assert record.error_message == "message to edit not found"
    assert record.text_preview.endswith("...")
    assert len(record.text_preview) == 120

@pytest.mark.asyncio
async def test_showdown_sends_new_hand_message_before_join_prompt():
    chat_id = -900
    call_order: List[str] = []

    async def record_new_hand(*args, **kwargs):
        call_order.append("new_hand")

    async def record_join_prompt(*args, **kwargs):
        call_order.append("join_prompt")

    view = _prepare_view_mock(MagicMock())
    view.send_showdown_results = AsyncMock()
    view.send_new_hand_ready_message = AsyncMock(side_effect=record_new_hand)
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    model._clear_game_messages = AsyncMock()
    model._send_join_prompt = AsyncMock(side_effect=record_join_prompt)
    model._determine_winners = MagicMock(return_value=[])

    game = Game()
    wallet = make_wallet_mock(100)
    player = Player(
        user_id=1,
        mention_markdown="@player",
        wallet=wallet,
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)
    player.state = PlayerState.ACTIVE
    game.players_by = MagicMock(return_value=[player])
    context = SimpleNamespace(chat_data={})

    await model._showdown(game, chat_id, context)

    assert call_order == ["new_hand", "join_prompt"]
    table_manager.save_game.assert_awaited()
    model._clear_game_messages.assert_awaited_once()
    view.send_showdown_results.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_game_assigns_blinds_to_occupied_seats():
    view = _prepare_view_mock(MagicMock())
    view.send_cards = AsyncMock()
    view.send_message = AsyncMock()
    view.delete_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    model._divide_cards = AsyncMock()
    model._send_turn_message = AsyncMock()
    model._round_rate._set_player_blind = AsyncMock()

    game = Game()
    ready_message_id = 444
    game.ready_message_main_id = ready_message_id
    game.ready_message_main_text = "prompt"
    game.dealer_index = 0

    wallet_a = make_wallet_mock(1000)
    player_a = Player(
        user_id=1,
        mention_markdown="@a",
        wallet=wallet_a,
        ready_message_id="ready-a",
    )
    game.add_player(player_a, seat_index=0)

    wallet_b = make_wallet_mock(1000)
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

    view.delete_message.assert_awaited_once_with(chat_id, ready_message_id)
    assert game.ready_message_main_id is None
    assert game.ready_message_main_text == ""
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


@pytest.mark.asyncio
async def test_start_game_keeps_ready_message_id_when_deletion_fails():
    view = _prepare_view_mock(MagicMock())
    view.send_cards = AsyncMock()
    view.send_message = AsyncMock()
    view.delete_message = AsyncMock(side_effect=BadRequest("not found"))
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = MagicMock()
    table_manager = MagicMock()

    model = PokerBotModel(view=view, bot=bot, cfg=cfg, kv=kv, table_manager=table_manager)
    model._divide_cards = AsyncMock()
    model._round_rate.set_blinds = AsyncMock()

    game = Game()
    ready_message_id = 321
    game.ready_message_main_id = ready_message_id
    game.ready_message_main_text = "prompt"
    game.dealer_index = -1

    wallet = make_wallet_mock(1000)
    player = Player(
        user_id=5,
        mention_markdown="@player",
        wallet=wallet,
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)

    context = SimpleNamespace(chat_data={}, job_queue=None)
    chat_id = -456

    await model._start_game(context, game, chat_id)

    view.delete_message.assert_awaited_once_with(chat_id, ready_message_id)
    assert game.ready_message_main_id == ready_message_id
    assert game.ready_message_main_text == ""
    model._divide_cards.assert_awaited_once_with(game, chat_id)
    model._round_rate.set_blinds.assert_awaited_once_with(game, chat_id)


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


