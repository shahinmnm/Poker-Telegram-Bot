#!/usr/bin/env python3

import asyncio
import datetime
import logging
import logging
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import List, Tuple, Optional
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import fakeredis.aioredis
import pytest

from pokerapp.cards import Cards, Card
from pokerapp.config import Config, get_game_constants
from pokerapp.entities import Money, Player, Game, PlayerState, GameState, PlayerAction
from pokerapp.pokerbotmodel import (
    PokerBotModel,
    RoundRateModel,
    WalletManagerModel,
    KEY_CHAT_DATA_GAME,
    KEY_START_COUNTDOWN_LAST_TEXT,
    KEY_START_COUNTDOWN_CONTEXT,
    KEY_START_COUNTDOWN_INITIAL_SECONDS,
    KEY_START_COUNTDOWN_ANCHOR,
    KEY_STOP_REQUEST,
    STOP_CONFIRM_CALLBACK,
    STOP_RESUME_CALLBACK,
)
from pokerapp.pokerbotview import TurnMessageUpdate, PokerBotViewer
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.utils.request_metrics import RequestMetrics
from telegram.error import BadRequest
from telegram import InlineKeyboardMarkup
import logging


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


def _make_private_match_service(kv, table_manager) -> PrivateMatchService:
    return PrivateMatchService(
        kv=kv,
        table_manager=table_manager,
        logger=logging.getLogger("test.private_match"),
        constants=get_game_constants(),
    )


def _prepare_view_mock(view: MagicMock) -> MagicMock:
    view.edit_message_text = AsyncMock(return_value=None)
    view.send_message_return_id = AsyncMock(return_value=None)
    view.send_message = AsyncMock()
    view.announce_player_seats = AsyncMock(return_value=None)
    view.send_player_role_anchors = AsyncMock(return_value=None)
    view.delete_message = AsyncMock()
    view.start_prestart_countdown = AsyncMock(return_value=None)
    view._cancel_prestart_countdown = AsyncMock(return_value=None)
    view.clear_all_player_anchors = AsyncMock(return_value=None)
    view.update_player_anchors_and_keyboards = AsyncMock(return_value=None)
    view.sync_player_private_keyboards = AsyncMock(return_value=None)
    view.update_turn_message = AsyncMock(
        return_value=TurnMessageUpdate(
            message_id=None,
            call_label="CHECK",
            call_action=PlayerAction.CHECK,
            board_line="",
        )
    )
    view.request_metrics = RequestMetrics(
        logger_=logging.getLogger("test.pokerbotmodel.request_metrics")
    )
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
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )
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
async def test_chat_guard_uses_default_chat_lock_level():
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = fakeredis.aioredis.FakeRedis()
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    table_manager = MagicMock()
    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    calls: List[dict] = []
    call_count = 0

    @asynccontextmanager
    async def fake_guard(**kwargs):
        nonlocal call_count
        call_count += 1
        calls.append(dict(kwargs))
        if call_count == 1:
            raise TimeoutError("boom")
        yield

    model._game_engine._trace_lock_guard = fake_guard  # type: ignore[assignment]

    async with model._chat_guard(chat_id=-1234):
        pass

    assert len(calls) == 2
    assert calls[0]["lock_key"] == "chat:-1234"
    assert "level" not in calls[0] or calls[0]["level"] is None
    assert "level" not in calls[1] or calls[1]["level"] is None


@pytest.mark.asyncio
async def test_register_player_identity_updates_active_game_private_chat():
    chat_id = -4242
    kv = fakeredis.aioredis.FakeRedis()
    bot = MagicMock()
    view = PokerBotViewer(bot=bot)
    view._send_player_private_keyboard = AsyncMock()

    game = Game()
    wallet = make_wallet_mock()
    player = Player(
        user_id=321,
        mention_markdown="@player",
        wallet=wallet,
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)

    table_manager = SimpleNamespace()
    table_manager._tables = {chat_id: game}
    table_manager.save_game = AsyncMock()

    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    user = SimpleNamespace(
        id=player.user_id,
        full_name="Test Player",
        first_name="Test",
        username="tester",
    )

    await model._register_player_identity(user, private_chat_id=999)

    assert player.private_chat_id == 999
    assert model._private_chat_ids[player.user_id] == 999
    table_manager.save_game.assert_awaited_once_with(chat_id, game)

    view._send_player_private_keyboard.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_stop_creates_vote_prompt():
    chat_id = -1200
    view = _prepare_view_mock(MagicMock())
    view.send_message_return_id = AsyncMock(return_value=77)
    view.edit_message_text = AsyncMock()
    view.send_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

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

    await model._game_engine.request_stop(
        context, game, chat_id, requester_id=player_a.user_id
    )

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
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

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

    await model._game_engine.request_stop(
        context, game, chat_id, requester_id=player_a.user_id
    )
    model._game_engine.cancel_hand = AsyncMock()

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=SimpleNamespace(
            data=STOP_CONFIRM_CALLBACK,
            from_user=SimpleNamespace(id=player_b.user_id),
        ),
    )

    await model.confirm_stop_vote(update, context)

    model._game_engine.cancel_hand.assert_awaited_once()
    args = model._game_engine.cancel_hand.await_args.args
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
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

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
    game.board_message_id = 404
    game.message_ids_to_delete.extend([77, 88])
    game.anchor_message_id = 909

    stop_request = {
        "game_id": game.id,
        "message_id": 99,
        "active_players": [player_a.user_id, player_b.user_id],
        "votes": {player_a.user_id, player_b.user_id},
        "manager_override": False,
    }

    context = SimpleNamespace(chat_data={KEY_STOP_REQUEST: stop_request})

    original_game_id = game.id

    await model._game_engine.cancel_hand(game, chat_id, context, stop_request)

    wallet_a.cancel.assert_awaited_once_with(original_game_id)
    wallet_b.cancel.assert_awaited_once_with(original_game_id)
    assert game.pot == 0
    assert KEY_STOP_REQUEST not in context.chat_data
    assert view.edit_message_text.await_count == 1
    assert view.send_message.await_count == 1
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert game.state == GameState.INITIAL
    assert player_a.ready_message_id is None
    assert player_b.ready_message_id is None
    assert getattr(game, "anchor_message_id", None) is None
    assert game.board_message_id is None
    assert game.message_ids_to_delete == []




def test_send_turn_message_updates_turn_message_only():
    model, game, player, view = _build_model_with_game()
    chat_id = -501

    other_player = Player(
        user_id=2,
        mention_markdown="@other",
        wallet=make_wallet_mock(1000),
        ready_message_id="ready-2",
    )
    game.add_player(other_player, seat_index=1)
    player.seat_index = 0
    other_player.seat_index = 1
    game.cards_table = [Card("A♠"), Card("K♦"), Card("5♣")]

    turn_update = SimpleNamespace(
        message_id=321,
        call_label="CALL",
        call_action=PlayerAction.CALL,
        board_line="🃏 Board: A♠     K♦     5♣",
    )
    view.update_turn_message = AsyncMock(return_value=turn_update)
    view.update_player_anchors_and_keyboards = AsyncMock()

    asyncio.run(model._send_turn_message(game, player, chat_id))

    assert game.turn_message_id == 321
    view.update_turn_message.assert_awaited_once()
    view.update_player_anchors_and_keyboards.assert_awaited_once_with(game=game)
    view.sync_player_private_keyboards.assert_not_awaited()


def test_add_cards_to_table_does_not_send_stage_message():
    model, game, player, view = _build_model_with_game()
    chat_id = -601

    view.send_message_return_id = AsyncMock(return_value=111)
    view.delete_message = AsyncMock()
    view.update_player_anchors_and_keyboards = AsyncMock()

    game.remain_cards = [Card("2♣"), Card("3♦"), Card("4♥")]
    game.state = GameState.ROUND_FLOP

    asyncio.run(model.add_cards_to_table(3, game, chat_id, "🃏 فلاپ"))

    view.send_message_return_id.assert_not_awaited()
    view.delete_message.assert_not_awaited()
    assert game.board_message_id is None
    view.update_player_anchors_and_keyboards.assert_awaited_once_with(game=game)
    view.sync_player_private_keyboards.assert_not_awaited()


def test_add_cards_to_table_removes_existing_stage_message():
    model, game, player, view = _build_model_with_game()
    chat_id = -602

    view.delete_message = AsyncMock()
    view.update_player_anchors_and_keyboards = AsyncMock()

    game.board_message_id = 222
    game.message_ids_to_delete.append(222)
    game.state = GameState.ROUND_TURN

    asyncio.run(model.add_cards_to_table(0, game, chat_id, "🃏 فلاپ"))

    view.delete_message.assert_awaited_once_with(chat_id, 222)
    assert game.board_message_id is None
    assert 222 not in game.message_ids_to_delete
    view.update_player_anchors_and_keyboards.assert_not_awaited()
    view.sync_player_private_keyboards.assert_not_awaited()
def test_clear_game_messages_preserves_anchor_messages():
    model, game, player, view = _build_model_with_game()
    chat_id = -500
    view.delete_message = AsyncMock()
    view.update_player_anchors_and_keyboards = AsyncMock()

    player.anchor_message = (chat_id, "anchor-7")
    game.message_ids_to_delete.extend(["anchor-7", 888, 999])
    game.board_message_id = 321
    game.turn_message_id = 654

    asyncio.run(model._clear_game_messages(game, chat_id))

    deleted_pairs = {call.args for call in view.delete_message.await_args_list}
    assert (chat_id, 321) in deleted_pairs
    assert (chat_id, 654) in deleted_pairs
    assert (chat_id, 888) in deleted_pairs
    assert (chat_id, 999) in deleted_pairs
    assert (chat_id, "anchor-7") not in deleted_pairs
    view.update_player_anchors_and_keyboards.assert_not_awaited()
    view.sync_player_private_keyboards.assert_not_awaited()
    assert player.anchor_message == (chat_id, "anchor-7")
    assert game.message_ids == {}
    assert game.message_ids_to_delete == []


@pytest.mark.asyncio
async def test_auto_start_tick_starts_prestart_countdown_and_updates_state():
    chat_id = -777
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.id = "game-42"
    game.ready_message_main_id = 111
    game.ready_message_game_id = game.id
    game.ready_message_stage = game.state
    game.ready_message_main_text = "prompt"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    countdown_state = {(chat_id, str(game.id)): {"seconds": 5}}
    context = SimpleNamespace(
        job=job,
        chat_data={KEY_START_COUNTDOWN_CONTEXT: countdown_state},
    )

    await model._auto_start_tick(context)

    view.start_prestart_countdown.assert_awaited_once()
    countdown_call = view.start_prestart_countdown.await_args
    assert countdown_call.kwargs["chat_id"] == chat_id
    assert countdown_call.kwargs["game_id"] == str(game.id)
    assert countdown_call.kwargs["anchor_message_id"] == 111
    assert countdown_call.kwargs["seconds"] == 5
    payload_fn = countdown_call.kwargs["payload_fn"]
    initial_text = game.ready_message_main_text
    assert "5 ثانیه" in initial_text
    preview_text, _ = payload_fn(3)
    assert "3 ثانیه" in preview_text
    state = context.chat_data[KEY_START_COUNTDOWN_CONTEXT][(chat_id, str(game.id))]
    assert state["seconds"] == 4
    assert state["active"] is True
    assert state["last_seconds"] == 5
    assert state[KEY_START_COUNTDOWN_LAST_TEXT] == preview_text
    assert state[KEY_START_COUNTDOWN_INITIAL_SECONDS] == 5
    assert isinstance(state[KEY_START_COUNTDOWN_ANCHOR], datetime.datetime)
    assert context.chat_data[KEY_CHAT_DATA_GAME] is game
    assert game.ready_message_main_text == preview_text
    assert view._cancel_prestart_countdown.await_count == 0
    table_manager.save_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_start_tick_keeps_approx_start_time_stable():
    chat_id = -7781
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.id = "game-approx"
    game.ready_message_main_id = 222
    game.ready_message_game_id = game.id
    game.ready_message_stage = game.state
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    countdown_state = {(chat_id, str(game.id)): {"seconds": 6}}
    context = SimpleNamespace(
        job=job,
        chat_data={KEY_START_COUNTDOWN_CONTEXT: countdown_state},
    )

    def _approx_line(value: str) -> str:
        for line in value.splitlines():
            if line.startswith("🕒"):
                return line
        return ""

    await model._auto_start_tick(context)
    first_line = _approx_line(game.ready_message_main_text)
    assert first_line

    await model._auto_start_tick(context)
    second_line = _approx_line(game.ready_message_main_text)
    assert second_line == first_line


@pytest.mark.asyncio
async def test_auto_start_tick_does_not_restart_on_regular_tick():
    chat_id = -779
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.id = "game-43"
    game.ready_message_main_id = 321
    game.ready_message_game_id = game.id
    game.ready_message_stage = game.state
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    countdown_state = {(chat_id, str(game.id)): {"seconds": 4}}
    context = SimpleNamespace(
        job=job,
        chat_data={KEY_START_COUNTDOWN_CONTEXT: countdown_state},
    )

    await model._auto_start_tick(context)
    await model._auto_start_tick(context)

    assert view.start_prestart_countdown.await_count == 1
    state = context.chat_data[KEY_START_COUNTDOWN_CONTEXT][(chat_id, str(game.id))]
    assert state["seconds"] == 2
    assert state["active"] is True
    assert state["last_seconds"] == 3
    table_manager.save_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_start_tick_restarts_when_countdown_increases():
    chat_id = -780
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.id = "game-44"
    game.ready_message_main_id = 555
    game.ready_message_game_id = game.id
    game.ready_message_stage = game.state
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    countdown_state = {(chat_id, str(game.id)): {"seconds": 5}}
    context = SimpleNamespace(
        job=job,
        chat_data={KEY_START_COUNTDOWN_CONTEXT: countdown_state},
    )

    await model._auto_start_tick(context)
    context.chat_data[KEY_START_COUNTDOWN_CONTEXT][(chat_id, str(game.id))][
        "seconds"
    ] = 8

    await model._auto_start_tick(context)

    assert view.start_prestart_countdown.await_count == 2
    last_call = view.start_prestart_countdown.await_args_list[-1]
    assert last_call.kwargs["seconds"] == 8
    state = context.chat_data[KEY_START_COUNTDOWN_CONTEXT][(chat_id, str(game.id))]
    assert state["seconds"] == 7


@pytest.mark.asyncio
async def test_auto_start_tick_triggers_game_start_when_zero():
    chat_id = -774
    view = _prepare_view_mock(MagicMock())
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.id = "game-45"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )
    model._start_game = AsyncMock()

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    context = SimpleNamespace(
        job=job,
        chat_data={
            KEY_START_COUNTDOWN_CONTEXT: {
                (chat_id, str(game.id)): {
                    "seconds": 0,
                    "active": True,
                    "last_seconds": 0,
                }
            },
            "start_countdown_job": object(),
        },
    )

    await model._auto_start_tick(context)

    model._start_game.assert_awaited_once_with(
        context, game, chat_id, require_guard=False
    )
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    job.schedule_removal.assert_called_once()
    assert KEY_START_COUNTDOWN_CONTEXT not in context.chat_data
    view._cancel_prestart_countdown.assert_awaited_once_with(chat_id, str(game.id))


@pytest.mark.asyncio
async def test_auto_start_tick_creates_message_when_missing():
    chat_id = -778
    view = _prepare_view_mock(MagicMock())
    view.send_message_return_id = AsyncMock(return_value=999)
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = None
    game.id = "game-46"
    game.ready_message_main_text = ""
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    countdown_state = {(chat_id, str(game.id)): {"seconds": 3}}
    context = SimpleNamespace(
        job=job,
        chat_data={KEY_START_COUNTDOWN_CONTEXT: countdown_state},
    )

    await model._auto_start_tick(context)

    view.send_message_return_id.assert_awaited_once()
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert game.ready_message_main_id == 999
    assert view.start_prestart_countdown.await_count == 1
    call_kwargs = view.start_prestart_countdown.await_args.kwargs
    assert call_kwargs["anchor_message_id"] == 999
    assert call_kwargs["game_id"] == str(game.id)
    state = context.chat_data[KEY_START_COUNTDOWN_CONTEXT][(chat_id, str(game.id))]
    assert state["seconds"] == 2


@pytest.mark.asyncio
async def test_auto_start_tick_does_not_start_when_message_creation_fails():
    chat_id = -779
    view = _prepare_view_mock(MagicMock())
    view.send_message_return_id = AsyncMock(return_value=None)
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    game = Game()
    game.ready_message_main_id = None
    game.id = "game-47"
    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

    job = SimpleNamespace(chat_id=chat_id)
    job.schedule_removal = MagicMock()
    countdown_state = {(chat_id, str(game.id)): {"seconds": 3}}
    context = SimpleNamespace(
        job=job,
        chat_data={KEY_START_COUNTDOWN_CONTEXT: countdown_state},
    )

    await model._auto_start_tick(context)

    assert view.start_prestart_countdown.await_count == 0
    state = context.chat_data[KEY_START_COUNTDOWN_CONTEXT][(chat_id, str(game.id))]
    assert state["seconds"] == 3
    assert state["active"] is False
    view._cancel_prestart_countdown.assert_awaited_once_with(chat_id, str(game.id))
    table_manager.save_game.assert_not_awaited()


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
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )

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
    view.clear_all_player_anchors = AsyncMock(
        side_effect=lambda *args, **kwargs: call_order.append("clear_anchors")
    )
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )
    model._game_engine._clear_game_messages = AsyncMock()
    model._player_manager.send_join_prompt = AsyncMock(side_effect=record_join_prompt)
    model._game_engine._evaluate_contender_hands = MagicMock(return_value=[])
    model._game_engine._determine_pot_winners = MagicMock(return_value=[])

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

    await model._game_engine.finalize_game(
        context=context,
        game=game,
        chat_id=chat_id,
    )

    assert call_order == ["clear_anchors", "new_hand", "join_prompt"]
    table_manager.save_game.assert_awaited()
    model._game_engine._clear_game_messages.assert_awaited_once()
    view.send_showdown_results.assert_awaited_once()
    view.clear_all_player_anchors.assert_awaited_once_with(game=game)


@pytest.mark.asyncio
async def test_start_game_assigns_blinds_to_occupied_seats():
    view = _prepare_view_mock(MagicMock())
    view.send_message = AsyncMock()
    view.delete_message = AsyncMock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )
    model._matchmaking_service._divide_cards = AsyncMock()
    model._matchmaking_service._send_turn_message = AsyncMock()
    model._round_rate._set_player_blind = AsyncMock()

    game = Game()
    ready_message_id = 444
    game.ready_message_main_id = ready_message_id
    game.ready_message_game_id = game.id
    game.ready_message_stage = game.state
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

    delete_calls = {call.args for call in view.delete_message.await_args_list}
    assert (chat_id, ready_message_id) in delete_calls
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

    view.send_player_role_anchors.assert_awaited_once_with(game=game, chat_id=chat_id)
    view.sync_player_private_keyboards.assert_not_awaited()

    model._matchmaking_service._send_turn_message.assert_awaited_once()
    send_call = model._matchmaking_service._send_turn_message.await_args
    assert send_call.args[1].user_id == player_b.user_id
    assert send_call.args[2] == chat_id


@pytest.mark.asyncio
async def test_start_game_clears_ready_message_id_even_when_deletion_fails():
    view = _prepare_view_mock(MagicMock())
    view.send_message = AsyncMock()
    view.delete_message = AsyncMock(side_effect=BadRequest("not found"))
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = MagicMock()
    table_manager = MagicMock()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )
    model._matchmaking_service._divide_cards = AsyncMock()
    model._round_rate.set_blinds = AsyncMock(return_value=None)

    game = Game()
    ready_message_id = 321
    game.ready_message_main_id = ready_message_id
    game.ready_message_game_id = game.id
    game.ready_message_stage = game.state
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

    delete_calls = {call.args for call in view.delete_message.await_args_list}
    assert (chat_id, ready_message_id) in delete_calls
    assert game.ready_message_main_id is None
    assert game.ready_message_main_text == ""
    model._matchmaking_service._divide_cards.assert_awaited_once_with(game, chat_id)
    model._round_rate.set_blinds.assert_awaited_once_with(game, chat_id)
    view.send_player_role_anchors.assert_awaited_once_with(game=game, chat_id=chat_id)
    view.sync_player_private_keyboards.assert_not_awaited()


def test_send_turn_message_updates_existing_message():
    model, game, player, view = _build_model_with_game()
    chat_id = -601
    player.wallet.value.return_value = 450
    game.turn_message_id = 111
    game.last_actions = ["action"]

    view.update_turn_message = AsyncMock(
        return_value=TurnMessageUpdate(
            message_id=222,
            call_label="CALL",
            call_action=PlayerAction.CALL,
            board_line="line",
        )
    )

    asyncio.run(model._send_turn_message(game, player, chat_id))

    assert view.update_turn_message.await_count == 1
    call = view.update_turn_message.await_args
    assert call.kwargs["chat_id"] == chat_id
    assert call.kwargs["game"] == game
    assert call.kwargs["player"] == player
    assert call.kwargs["money"] == 450
    assert call.kwargs["message_id"] == 111
    assert call.kwargs["recent_actions"] == game.last_actions

    assert game.turn_message_id == 222


def test_send_turn_message_keeps_previous_when_new_message_missing():
    model, game, player, view = _build_model_with_game()
    chat_id = -602
    player.wallet.value.return_value = 320
    game.turn_message_id = 333
    game.last_actions = ["action"]

    view.update_turn_message = AsyncMock(
        return_value=TurnMessageUpdate(
            message_id=None,
            call_label="CALL",
            call_action=PlayerAction.CALL,
            board_line="line",
        )
    )

    asyncio.run(model._send_turn_message(game, player, chat_id))

    view.update_turn_message.assert_awaited_once()
    assert game.turn_message_id == 333


