import logging
from collections import defaultdict
from types import SimpleNamespace
from typing import Dict, Optional
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
async def test_process_fold_win_assigns_payout_and_announces_winner():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    winner_wallet = _make_wallet_mock(1200)
    folded_wallet = _make_wallet_mock(800)
    winner = Player(
        user_id=100,
        mention_markdown="@winner",
        wallet=winner_wallet,
        ready_message_id="ready-w",
    )
    folded = Player(
        user_id=200,
        mention_markdown="@folded",
        wallet=folded_wallet,
        ready_message_id="ready-f",
    )

    game = Game()
    game.id = "test-game-showdown"
    game.pot = 200
    winner.state = PlayerState.ACTIVE
    folded.state = PlayerState.FOLD
    game.add_player(winner, seat_index=0)
    game.add_player(folded, seat_index=1)

    payouts = defaultdict(int)
    hand_labels: Dict[int, Optional[str]] = {}
    chat_id = -321

    await model._game_engine._process_fold_win(
        game,
        [folded.user_id],
        payouts=payouts,
        hand_labels=hand_labels,
        chat_id=chat_id,
    )

    assert payouts[winner.user_id] == 200
    assert hand_labels[winner.user_id] == "پیروزی با فولد رقبا"
    winner_wallet.inc.assert_not_awaited()
    view.send_message.assert_awaited_once()
    message_args, _ = view.send_message.await_args
    assert str(game.pot) in message_args[1]
    assert winner.mention_markdown in message_args[1]


@pytest.mark.asyncio
async def test_process_showdown_results_populates_payouts_and_labels():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    winner_wallet = _make_wallet_mock(1500)
    loser_wallet = _make_wallet_mock(900)
    winner = Player(
        user_id=10,
        mention_markdown="@winner",
        wallet=winner_wallet,
        ready_message_id="ready-w",
    )
    loser = Player(
        user_id=20,
        mention_markdown="@loser",
        wallet=loser_wallet,
        ready_message_id="ready-l",
    )

    game = Game()
    game.id = "test-game-showdown"
    game.pot = 120
    winner.state = PlayerState.ACTIVE
    loser.state = PlayerState.ACTIVE
    winner.cards = [Card("A♠"), Card("K♠")]
    loser.cards = [Card("Q♣"), Card("Q♦")]
    game.add_player(winner, seat_index=0)
    game.add_player(loser, seat_index=1)

    payouts = defaultdict(int)
    hand_labels: Dict[int, Optional[str]] = {}
    chat_id = -654

    async def _passthrough_send_message_safe(*, call, **_kwargs):
        return await call()

    send_message_safe = AsyncMock(side_effect=_passthrough_send_message_safe)
    model._game_engine._telegram_ops.send_message_safe = send_message_safe

    winner_data = {
        "contender_details": [
            {"player": winner, "hand_type": HandsOfPoker.FLUSH},
            {"player": loser, "hand_type": HandsOfPoker.PAIR},
        ],
        "winners_by_pot": [
            {
                "amount": 120,
                "winners": [
                    {"player": winner, "hand_type": HandsOfPoker.FLUSH},
                ],
            }
        ],
    }

    await model._game_engine._process_showdown_results(
        game,
        winner_data,
        payouts=payouts,
        hand_labels=hand_labels,
        chat_id=chat_id,
    )

    assert payouts[winner.user_id] == 120
    assert hand_labels[winner.user_id]
    assert hand_labels[loser.user_id]
    send_message_safe.assert_awaited_once()
    _, send_kwargs = send_message_safe.await_args
    assert send_kwargs["chat_id"] == chat_id
    assert send_kwargs["operation"] == "send_showdown_results"
    assert callable(send_kwargs["call"])
    assert send_kwargs["log_extra"]["operation"] == "send_showdown_results"
    assert send_kwargs["log_extra"]["game_id"] == "test-game-showdown"
    view.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_showdown_results_handles_empty_winners():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    player = Player(
        user_id=30,
        mention_markdown="@player",
        wallet=_make_wallet_mock(500),
        ready_message_id="ready",
    )
    game = Game()
    game.pot = 0
    player.state = PlayerState.ACTIVE
    game.add_player(player, seat_index=0)

    payouts = defaultdict(int)
    hand_labels: Dict[int, Optional[str]] = {}

    async def _passthrough_send_message_safe(*, call, **_kwargs):
        return await call()

    send_message_safe = AsyncMock(side_effect=_passthrough_send_message_safe)
    model._game_engine._telegram_ops.send_message_safe = send_message_safe
    chat_id = -111

    winner_data = {
        "contender_details": [{"player": player, "hand_type": HandsOfPoker.HIGH_CARD}],
        "winners_by_pot": [],
    }

    await model._game_engine._process_showdown_results(
        game,
        winner_data,
        payouts=payouts,
        hand_labels=hand_labels,
        chat_id=chat_id,
    )

    view.send_message.assert_awaited_once()
    assert send_message_safe.await_count == 2
    assert [
        recorded_call.kwargs.get("operation")
        for recorded_call in send_message_safe.await_args_list
    ] == ["announce_showdown_warning", "send_showdown_results"]


@pytest.mark.asyncio
async def test_handle_winners_returns_payouts_and_labels_for_showdown():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    engine = model._game_engine
    engine._evaluate_contender_hands = MagicMock(return_value=[{"player": None}])
    engine._determine_winners = MagicMock(return_value=[{"amount": 100, "winners": []}])

    active_player = Player(
        user_id=999,
        mention_markdown="@active",
        wallet=_make_wallet_mock(0),
        ready_message_id="ready-a",
    )
    active_player.state = PlayerState.ACTIVE
    folded_player = Player(
        user_id=500,
        mention_markdown="@folded",
        wallet=_make_wallet_mock(0),
        ready_message_id="ready-f",
    )
    folded_player.state = PlayerState.FOLD

    game = Game()
    game.add_player(active_player, seat_index=0)
    game.add_player(folded_player, seat_index=1)

    async def fake_showdown(
        game: Game,
        winner_data: Dict[str, object],
        *,
        payouts: Dict[int, int],
        hand_labels: Dict[int, Optional[str]],
        chat_id: int,
    ) -> None:
        payouts[active_player.user_id] += 75
        hand_labels[active_player.user_id] = "label"

    engine._process_showdown_results = AsyncMock(side_effect=fake_showdown)

    chat_id = -1234
    payouts, hand_labels = await engine._handle_winners(game=game, chat_id=chat_id)

    assert payouts[active_player.user_id] == 75
    assert hand_labels[active_player.user_id] == "label"
    engine._evaluate_contender_hands.assert_called_once()
    engine._determine_winners.assert_called_once()
    engine._process_showdown_results.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_winners_invokes_fold_processor_when_no_contenders():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    engine = model._game_engine
    engine._process_fold_win = AsyncMock()

    folded_player = Player(
        user_id=2000,
        mention_markdown="@folded",
        wallet=_make_wallet_mock(0),
        ready_message_id="ready-f",
    )
    folded_player.state = PlayerState.FOLD

    game = Game()
    game.add_player(folded_player, seat_index=0)

    payouts, hand_labels = await engine._handle_winners(game=game, chat_id=-4321)

    engine._process_fold_win.assert_awaited_once()
    assert isinstance(payouts, defaultdict)
    assert dict(payouts) == {}
    assert hand_labels == {}


@pytest.mark.asyncio
async def test_payout_delegates_to_distribute_payouts():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    engine = model._game_engine
    engine._distribute_payouts = AsyncMock()

    payouts = defaultdict(int)
    payouts[1] = 100
    game = Game()

    await engine._payout(game=game, payouts=payouts)

    engine._distribute_payouts.assert_awaited_once()
    args, kwargs = engine._distribute_payouts.await_args
    assert args[0] is game
    assert args[1] is payouts
    assert kwargs == {}


@pytest.mark.asyncio
async def test_announce_results_updates_cache_and_stats():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    engine = model._game_engine
    engine._invalidate_adaptive_report_cache = MagicMock()
    engine._stats_reporter.hand_finished = AsyncMock()

    game = Game()
    player = Player(
        user_id=7,
        mention_markdown="@player",
        wallet=_make_wallet_mock(0),
        ready_message_id="ready",
    )
    players_snapshot = [player]
    payouts = {7: 40}
    hand_labels = {7: "label"}

    await engine._announce_results(
        game=game,
        chat_id=-99,
        payouts=payouts,
        hand_labels=hand_labels,
        pot_total=150,
        players_snapshot=players_snapshot,
    )

    engine._invalidate_adaptive_report_cache.assert_called_once_with(
        players_snapshot, event_type="hand_finished"
    )
    engine._stats_reporter.hand_finished.assert_awaited_once_with(
        game,
        -99,
        payouts=payouts,
        hand_labels=hand_labels,
        pot_total=150,
    )


@pytest.mark.asyncio
async def test_reset_state_resets_game_and_prompts_players():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    engine = model._game_engine
    engine._reset_game_state = AsyncMock()
    engine._telegram_ops.send_message_safe = AsyncMock()
    engine._player_manager.send_join_prompt = AsyncMock()

    game = Game()
    context = SimpleNamespace(chat_data={})
    chat_id = -55
    game_id = 321

    await engine._reset_state(
        game=game,
        context=context,
        chat_id=chat_id,
        game_id=game_id,
    )

    engine._reset_game_state.assert_awaited_once_with(
        game,
        context=context,
        chat_id=chat_id,
        send_stop_notification=False,
    )
    engine._telegram_ops.send_message_safe.assert_awaited_once()
    _, kwargs = engine._telegram_ops.send_message_safe.await_args
    assert kwargs["chat_id"] == chat_id
    assert kwargs["operation"] == "send_new_hand_ready_message"
    assert callable(kwargs["call"])
    assert kwargs["log_extra"]["game_id"] == game_id
    engine._player_manager.send_join_prompt.assert_awaited_once_with(game, chat_id)


@pytest.mark.asyncio
async def test_distribute_payouts_updates_wallets():
    view = _build_view_mock()
    bot = MagicMock()
    cfg = MagicMock(DEBUG=False)
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

    wallet_a = _make_wallet_mock(1000)
    wallet_b = _make_wallet_mock(1000)
    player_a = Player(user_id=1, mention_markdown="@a", wallet=wallet_a, ready_message_id="ready-a")
    player_b = Player(user_id=2, mention_markdown="@b", wallet=wallet_b, ready_message_id="ready-b")

    game = Game()
    game.add_player(player_a, seat_index=0)
    game.add_player(player_b, seat_index=1)

    payouts = {1: 150, 2: 0}

    await model._game_engine._distribute_payouts(game, payouts)

    wallet_a.inc.assert_awaited_once_with(150)
    wallet_b.inc.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_game_state_clears_pot_and_saves_game():
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

    model._game_engine._request_metrics.end_cycle = AsyncMock()
    model._player_manager.clear_player_anchors = AsyncMock()

    wallet_active = _make_wallet_mock(500)
    wallet_empty = _make_wallet_mock(0)
    wallet_empty.value = AsyncMock(return_value=0)
    player_active = Player(user_id=5, mention_markdown="@active", wallet=wallet_active, ready_message_id="ready-a")
    player_empty = Player(user_id=6, mention_markdown="@empty", wallet=wallet_empty, ready_message_id="ready-e")

    game = Game()
    game.state = GameState.ROUND_RIVER
    game.pot = 250
    original_id = game.id
    game.add_player(player_active, seat_index=0)
    game.add_player(player_empty, seat_index=1)

    context = SimpleNamespace(chat_data={})
    chat_id = -222

    await model._game_engine._reset_game_state(game, context=context, chat_id=chat_id)

    assert context.chat_data[KEY_OLD_PLAYERS] == [player_active.user_id]
    model._game_engine._request_metrics.end_cycle.assert_awaited_once_with(
        chat_id, cycle_token=original_id
    )
    model._player_manager.clear_player_anchors.assert_awaited_once_with(game)
    table_manager.save_game.assert_awaited_once_with(chat_id, game)
    assert game.state == GameState.INITIAL
    assert game.pot == 0
    assert game.players == []
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
    adaptive_cache_mock = MagicMock()
    model._game_engine._adaptive_player_report_cache = adaptive_cache_mock

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

    adaptive_cache_mock.invalidate_on_event.assert_called_once_with(
        {1, 2}, "hand_finished"
    )
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
    adaptive_cache_mock = MagicMock()
    model._game_engine._adaptive_player_report_cache = adaptive_cache_mock

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

    adaptive_cache_mock.invalidate_on_event.assert_called_once_with(
        {10, 20}, "hand_finished"
    )
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
