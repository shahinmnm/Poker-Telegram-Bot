import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.entities import Game, GameState, Player, PlayerState
from pokerapp.config import GameConstants
from pokerapp.game_engine import GameEngine


@pytest.fixture
def game_engine_setup():
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    view = MagicMock()
    view.send_message = AsyncMock()

    request_metrics = MagicMock()
    request_metrics.end_cycle = AsyncMock()

    stats_reporter = MagicMock()
    stats_reporter.invalidate_players = AsyncMock()

    adaptive_cache = MagicMock()

    player_manager = MagicMock()
    player_manager.clear_player_anchors = AsyncMock()

    telegram_safe_ops = SimpleNamespace(
        edit_message_text=AsyncMock(return_value=None)
    )

    engine = GameEngine(
        table_manager=table_manager,
        view=view,
        winner_determination=MagicMock(),
        request_metrics=request_metrics,
        round_rate=MagicMock(),
        player_manager=player_manager,
        matchmaking_service=MagicMock(),
        stats_reporter=stats_reporter,
        clear_game_messages=AsyncMock(),
        build_identity_from_player=lambda player: player,
        safe_int=int,
        old_players_key="old_players",
        telegram_safe_ops=telegram_safe_ops,
        lock_manager=MagicMock(),
        logger=MagicMock(),
        adaptive_player_report_cache=adaptive_cache,
    )

    return SimpleNamespace(
        engine=engine,
        view=view,
        table_manager=table_manager,
        request_metrics=request_metrics,
        stats_reporter=stats_reporter,
        player_manager=player_manager,
        telegram_safe_ops=telegram_safe_ops,
        adaptive_cache=adaptive_cache,
    )


@pytest.mark.asyncio
async def test_refund_players_cancels_wallets_and_invalidates(game_engine_setup):
    wallet_a = MagicMock()
    wallet_a.cancel = AsyncMock()
    player_a = Player(
        user_id=1,
        mention_markdown="@a",
        wallet=wallet_a,
        ready_message_id="r1",
    )

    wallet_b = MagicMock()
    wallet_b.cancel = AsyncMock()
    player_b = Player(
        user_id=2,
        mention_markdown="@b",
        wallet=wallet_b,
        ready_message_id="r2",
    )

    await game_engine_setup.engine._refund_players(
        [player_a, player_b], "game-123"
    )

    wallet_a.cancel.assert_awaited_once_with("game-123")
    wallet_b.cancel.assert_awaited_once_with("game-123")
    game_engine_setup.adaptive_cache.invalidate_on_event.assert_called_once_with(
        {1, 2}, "hand_finished"
    )
    game_engine_setup.stats_reporter.invalidate_players.assert_awaited_once_with(
        [player_a, player_b], event_type="hand_finished"
    )


@pytest.mark.asyncio
async def test_finalize_stop_request_updates_message_and_clears_context(
    game_engine_setup,
):
    context = SimpleNamespace(chat_data={"stop_request": "keep"})
    context.chat_data[game_engine_setup.engine.KEY_STOP_REQUEST] = {
        "game_id": "game-1"
    }

    stop_request = {
        "message_id": 42,
        "active_players": {1},
        "votes": {1},
        "manager_override": False,
    }

    await game_engine_setup.engine._finalize_stop_request(
        context=context,
        chat_id=-500,
        stop_request=stop_request,
    )

    game_engine_setup.telegram_safe_ops.edit_message_text.assert_awaited_once()
    assert (
        game_engine_setup.engine.KEY_STOP_REQUEST not in context.chat_data
    )


@pytest.mark.asyncio
async def test_reset_game_state_clears_pot_and_persists(game_engine_setup):
    game = Game()
    game.pot = 300

    await game_engine_setup.engine._reset_game_state(
        game=game,
        chat_id=-400,
        context=SimpleNamespace(chat_data={}),
        send_stop_notification=True,
    )

    assert game.pot == 0
    assert game.state == GameState.INITIAL
    game_engine_setup.request_metrics.end_cycle.assert_awaited_once()
    game_engine_setup.player_manager.clear_player_anchors.assert_awaited_once_with(game)
    game_engine_setup.table_manager.save_game.assert_awaited_once_with(-400, game)
    game_engine_setup.view.send_message.assert_awaited_once_with(
        -400, game_engine_setup.engine.STOPPED_NOTIFICATION
    )


def test_render_stop_request_message_uses_translations(game_engine_setup):
    engine = game_engine_setup.engine
    game = Game()
    player = Player(user_id=1, mention_markdown="@one", wallet=None, ready_message_id=None)
    player.state = PlayerState.ACTIVE
    game.add_player(player, seat_index=0)
    game.state = GameState.ROUND_FLOP

    stop_request = {
        "active_players": [1],
        "votes": {1},
        "initiator": 1,
        "message_id": None,
        "manager_override": False,
    }

    context = SimpleNamespace(chat_data={})

    message = engine.render_stop_request_message(
        game=game,
        stop_request=stop_request,
        context=context,
    )

    lines = message.splitlines()
    assert lines[0] == engine.STOP_TITLE_TEMPLATE
    assert lines[1] == engine.STOP_INITIATED_BY_TEMPLATE.format(
        initiator=player.mention_markdown
    )
    assert engine.STOP_ACTIVE_PLAYERS_LABEL in lines


@pytest.mark.asyncio
async def test_update_votes_and_message_tracks_manager_override(game_engine_setup):
    context = SimpleNamespace(chat_data={})
    stop_request = {"message_id": 5, "votes": set(), "active_players": {1}}

    updated = await game_engine_setup.engine._update_votes_and_message(
        context=context,
        game=Game(),
        chat_id=-10,
        stop_request=stop_request,
        voter_id="manager",
        manager_id="manager",
        votes=set(),
    )

    assert updated["manager_override"] is True
    assert "manager" in updated["votes"]
    assert (
        context.chat_data[game_engine_setup.engine.KEY_STOP_REQUEST] is updated
    )


@pytest.mark.asyncio
async def test_check_if_stop_passes_triggers_cancel(game_engine_setup):
    engine = game_engine_setup.engine
    engine.cancel_hand = AsyncMock()

    stop_request = {
        "votes": {1, 2},
        "manager_override": False,
    }

    await engine._check_if_stop_passes(
        game=Game(),
        chat_id=-1,
        context=SimpleNamespace(chat_data={}),
        stop_request=stop_request,
        active_ids={1, 2, 3},
    )

    engine.cancel_hand.assert_awaited_once()


def test_game_engine_custom_stop_translations(tmp_path):
    custom_translations = {
        "default_language": "en",
        "stop_vote": {
            "buttons": {
                "confirm": {"en": "Approve stop", "fa": "تأیید توقف"},
                "resume": {"en": "Keep playing", "fa": "ادامه بازی"},
            },
            "messages": {
                "title": {
                    "en": "Custom stop request",
                    "fa": "درخواست توقف سفارشی",
                },
                "initiated_by": {
                    "en": "Starter: {initiator}",
                    "fa": "شروع‌کننده: {initiator}",
                },
                "active_players_label": {
                    "en": "Players in hand:",
                    "fa": "بازیکنان حاضر:",
                },
                "active_player_line": {
                    "en": "{player} :: {mark}",
                    "fa": "{player} :: {mark}",
                },
                "vote_counts": {
                    "en": "Votes {confirmed}/{required}",
                    "fa": "آرا {confirmed}/{required}",
                },
                "resume_text": {
                    "en": "Game resumes",
                    "fa": "بازی ادامه می‌یابد",
                },
                "no_active_players_placeholder": {
                    "en": "No active players",
                    "fa": "بازیکن فعالی وجود ندارد",
                },
            },
            "errors": {
                "no_active_game": {
                    "en": "No active game",
                    "fa": "بازی فعالی وجود ندارد",
                }
            },
        },
    }

    translations_file = tmp_path / "translations.json"
    translations_file.write_text(
        json.dumps(custom_translations, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    constants = GameConstants(
        translations_path=str(translations_file),
        translation_defaults={"default_language": "en"},
    )

    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()

    view = MagicMock()
    request_metrics = MagicMock()
    stats_reporter = MagicMock()
    player_manager = MagicMock()
    player_manager.clear_player_anchors = AsyncMock()

    telegram_safe_ops = SimpleNamespace(
        edit_message_text=AsyncMock(return_value=None)
    )

    adaptive_cache = MagicMock()

    engine = GameEngine(
        table_manager=table_manager,
        view=view,
        winner_determination=MagicMock(),
        request_metrics=request_metrics,
        round_rate=MagicMock(),
        player_manager=player_manager,
        matchmaking_service=MagicMock(),
        stats_reporter=stats_reporter,
        clear_game_messages=AsyncMock(),
        build_identity_from_player=lambda player: player,
        safe_int=int,
        old_players_key="old_players",
        telegram_safe_ops=telegram_safe_ops,
        lock_manager=MagicMock(),
        logger=MagicMock(),
        constants=constants,
        adaptive_player_report_cache=adaptive_cache,
    )

    game = Game()
    player = Player(
        user_id=1,
        mention_markdown="@player",
        wallet=None,
        ready_message_id=None,
    )
    player.state = PlayerState.ACTIVE
    game.add_player(player, seat_index=0)
    game.state = GameState.ROUND_FLOP

    stop_request = {
        "active_players": [1],
        "votes": {1},
        "initiator": 1,
        "message_id": None,
        "manager_override": False,
    }

    context = SimpleNamespace(chat_data={})

    message = engine.render_stop_request_message(
        game=game,
        stop_request=stop_request,
        context=context,
    )

    assert engine.STOP_TITLE_TEMPLATE == "Custom stop request"
    assert "Custom stop request" in message

    expected_line = engine.STOP_ACTIVE_PLAYER_LINE_TEMPLATE.format(
        mark="✅",
        player=player.mention_markdown,
    )
    assert expected_line in message
