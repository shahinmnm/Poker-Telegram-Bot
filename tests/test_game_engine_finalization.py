import logging
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from pokerapp.cards import Card
from pokerapp.entities import Game, GameState, Player, PlayerState
from pokerapp.pokerbotmodel import KEY_OLD_PLAYERS, PokerBotModel
from pokerapp.stats import BaseStatsService
from pokerapp.winnerdetermination import HandsOfPoker
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.config import get_game_constants
from pokerapp.utils.request_metrics import RequestMetrics


def _make_wallet_mock(value: Optional[int] = None) -> MagicMock:
    wallet = MagicMock()
    wallet.value = AsyncMock(return_value=value if value is not None else 0)
    wallet.inc = AsyncMock()
    wallet.authorize = AsyncMock()
    wallet.cancel = AsyncMock()
    wallet.approve = AsyncMock()
    wallet.authorized_money = AsyncMock(return_value=0)
    wallet.inc_authorized_money = AsyncMock()
    wallet.add_daily = AsyncMock()
    wallet.has_daily_bonus = AsyncMock(return_value=False)
    return wallet


def _make_private_match_service(kv, table_manager) -> PrivateMatchService:
    return PrivateMatchService(
        kv=kv,
        table_manager=table_manager,
        logger=logging.getLogger("test.private_match"),
        constants=get_game_constants(),
    )


def _build_view_mock() -> MagicMock:
    view = MagicMock()
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
    view.update_turn_message = AsyncMock()
    view.send_showdown_results = AsyncMock()
    view.send_new_hand_ready_message = AsyncMock()
    view.request_metrics = RequestMetrics(
        logger_=logging.getLogger("test.game_engine.request_metrics")
    )
    return view


def _build_stats_service() -> MagicMock:
    stats = MagicMock(spec=BaseStatsService)
    stats.register_player_profile = AsyncMock()
    stats.start_hand = AsyncMock()
    stats.finish_hand = AsyncMock()
    stats.record_daily_bonus = AsyncMock()
    stats.build_player_report = AsyncMock()
    stats.format_report = MagicMock()
    stats.close = AsyncMock()
    return stats


@pytest.mark.asyncio
async def test_hand_type_label_includes_translation_and_emoji():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    cfg.constants = get_game_constants()
    kv = fakeredis.aioredis.FakeRedis()
    table_manager = MagicMock()
    stats = _build_stats_service()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
        stats_service=stats,
    )

    label = model._game_engine.hand_type_to_label(HandsOfPoker.FULL_HOUSE)
    assert label is not None
    assert "🏠" in label
    assert "فول هاوس" in label


@pytest.mark.asyncio
async def test_finalize_game_single_winner_distributes_pot_and_updates_stats():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    cfg.constants = get_game_constants()
    kv = fakeredis.aioredis.FakeRedis()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()
    stats = _build_stats_service()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
        stats_service=stats,
    )
    model._game_engine._clear_game_messages = AsyncMock()
    model._player_manager.clear_player_anchors = AsyncMock()
    model._player_manager.send_join_prompt = AsyncMock()

    game = Game()
    game.state = GameState.ROUND_RIVER
    game.cards_table = [Card("2♣"), Card("3♦"), Card("4♠"), Card("9♥"), Card("K♦")]
    game.pot = 100

    winner_wallet = _make_wallet_mock(1100)
    loser_wallet = _make_wallet_mock(900)
    winner = Player(user_id=1, mention_markdown="@winner", wallet=winner_wallet, ready_message_id="ready-w")
    loser = Player(user_id=2, mention_markdown="@loser", wallet=loser_wallet, ready_message_id="ready-l")
    game.add_player(winner, seat_index=0)
    game.add_player(loser, seat_index=1)

    winner.cards = [Card("A♠"), Card("A♦")]
    loser.cards = [Card("Q♣"), Card("J♣")]
    winner.state = PlayerState.ACTIVE
    loser.state = PlayerState.ACTIVE
    winner.total_bet = 50
    loser.total_bet = 50

    context = SimpleNamespace(chat_data={})
    chat_id = -500

    await model._game_engine.finalize_game(context=context, game=game, chat_id=chat_id)

    winner_wallet.inc.assert_awaited_once_with(100)
    loser_wallet.inc.assert_not_awaited()

    assert view.send_showdown_results.await_count == 1
    send_args, _ = view.send_showdown_results.await_args
    assert send_args[0] == chat_id
    winners_by_pot = send_args[2]
    assert winners_by_pot and winners_by_pot[0]["amount"] == 100
    assert winners_by_pot[0]["winners"][0]["player"] is winner

    stats.finish_hand.assert_awaited_once()
    _, stats_kwargs = stats.finish_hand.await_args
    results = list(stats_kwargs["results"])
    assert any(res.user_id == winner.user_id and res.payout == 100 for res in results)
    assert any(res.user_id == loser.user_id and res.result == "loss" for res in results)

    assert context.chat_data[KEY_OLD_PLAYERS] == [winner.user_id, loser.user_id]
    table_manager.save_game.assert_awaited()
    view.send_new_hand_ready_message.assert_awaited_once_with(chat_id)
    model._player_manager.send_join_prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_game_split_pot_between_tied_winners():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
    kv = fakeredis.aioredis.FakeRedis()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()
    stats = _build_stats_service()

    private_match_service = _make_private_match_service(kv, table_manager)
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
        stats_service=stats,
    )
    model._game_engine._clear_game_messages = AsyncMock()
    model._player_manager.clear_player_anchors = AsyncMock()
    model._player_manager.send_join_prompt = AsyncMock()

    game = Game()
    game.state = GameState.ROUND_RIVER
    game.cards_table = [Card("2♣"), Card("3♦"), Card("4♠"), Card("5♥"), Card("6♦")]
    game.pot = 100

    wallet_a = _make_wallet_mock(1000)
    wallet_b = _make_wallet_mock(1000)
    player_a = Player(user_id=10, mention_markdown="@a", wallet=wallet_a, ready_message_id="ready-a")
    player_b = Player(user_id=20, mention_markdown="@b", wallet=wallet_b, ready_message_id="ready-b")
    game.add_player(player_a, seat_index=0)
    game.add_player(player_b, seat_index=1)

    player_a.cards = [Card("K♣"), Card("Q♦")]
    player_b.cards = [Card("K♠"), Card("Q♣")]
    player_a.state = PlayerState.ACTIVE
    player_b.state = PlayerState.ACTIVE
    player_a.total_bet = 50
    player_b.total_bet = 50

    context = SimpleNamespace(chat_data={})
    chat_id = -777

    await model._game_engine.finalize_game(context=context, game=game, chat_id=chat_id)

    wallet_a.inc.assert_awaited_once_with(50)
    wallet_b.inc.assert_awaited_once_with(50)

    send_args, _ = view.send_showdown_results.await_args
    pot_summary = send_args[2]
    assert pot_summary and pot_summary[0]["amount"] == 100
    assert {winner_info["player"] for winner_info in pot_summary[0]["winners"]} == {player_a, player_b}

    _, stats_kwargs = stats.finish_hand.await_args
    results = list(stats_kwargs["results"])
    for result in results:
        assert result.result == "push"
        assert result.net_profit == 0
        assert result.hand_type is not None

    assert context.chat_data[KEY_OLD_PLAYERS] == [player_a.user_id, player_b.user_id]
