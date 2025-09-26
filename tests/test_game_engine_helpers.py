import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import RetryAfter

from pokerapp.config import GameConstants
from pokerapp.entities import Game, GameState, Player, PlayerState, UserException
from pokerapp.game_engine import GameEngine
from pokerapp.utils.request_metrics import RequestCategory
from pokerapp.utils.telegram_safeops import TelegramSafeOps


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

    async def _passthrough_send_message_safe(*, call, **_kwargs):
        return await call()

    telegram_safe_ops = SimpleNamespace(
        edit_message_text=AsyncMock(return_value=None),
        send_message_safe=AsyncMock(side_effect=_passthrough_send_message_safe),
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
    stop_request = {
        "game_id": "game-1",
        "message_id": 42,
        "active_players": {1},
        "votes": {1},
        "manager_override": False,
    }

    context = SimpleNamespace(
        chat_data={
            game_engine_setup.engine.KEY_STOP_REQUEST: dict(stop_request),
        }
    )

    await game_engine_setup.engine._finalize_stop_request(
        context=context,
        chat_id=-500,
        stop_request=stop_request,
    )

    game_engine_setup.telegram_safe_ops.edit_message_text.assert_awaited_once()
    call_kwargs = (
        game_engine_setup.telegram_safe_ops.edit_message_text.await_args.kwargs
    )
    log_extra = call_kwargs["log_extra"]
    assert log_extra["chat_id"] == -500
    assert log_extra["message_id"] == 42
    assert log_extra["game_id"] == "game-1"
    assert log_extra["operation"] == "stop_vote_finalize_message"
    assert log_extra["request_category"] == RequestCategory.GENERAL.value
    assert (
        game_engine_setup.engine.KEY_STOP_REQUEST not in context.chat_data
    )


@pytest.mark.asyncio
async def test_reset_game_state_clears_pot_and_persists(game_engine_setup):
    game = Game()
    game.pot = 300
    player = Player(
        user_id=1,
        mention_markdown="@one",
        wallet=None,
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)
    game.board_message_id = 777
    game.message_ids_to_delete.extend([101, 202])
    game.anchor_message_id = 555
    game.ready_message_main_id = 999
    game.ready_message_game_id = "game-token"
    game.ready_message_stage = GameState.ROUND_FLOP
    game.ready_message_main_text = "Ready list"
    game.seat_announcement_message_id = 313

    await game_engine_setup.engine._reset_core_game_state(
        game=game,
        chat_id=-400,
        context=SimpleNamespace(chat_data={}),
        send_stop_notification=True,
    )

    assert game.pot == 0
    assert game.state == GameState.INITIAL
    assert player.ready_message_id is None
    assert getattr(game, "anchor_message_id", None) is None
    assert game.board_message_id is None
    assert game.ready_message_main_id is None
    assert game.ready_message_game_id is None
    assert game.ready_message_stage is None
    assert game.ready_message_main_text == ""
    assert game.seat_announcement_message_id is None
    assert game.message_ids_to_delete == []
    game_engine_setup.request_metrics.end_cycle.assert_awaited_once()
    game_engine_setup.player_manager.clear_player_anchors.assert_awaited_once_with(game)
    game_engine_setup.table_manager.save_game.assert_awaited_once_with(-400, game)
    game_engine_setup.view.send_message.assert_awaited_once_with(
        -400, game_engine_setup.engine.STOPPED_NOTIFICATION
    )


@pytest.mark.asyncio
async def test_stop_game_initial_state_clears_messages(game_engine_setup):
    game = Game()
    player = Player(
        user_id=5,
        mention_markdown="@player",
        wallet=None,
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)
    game.board_message_id = 123
    game.message_ids_to_delete.extend([10, 20])
    game.anchor_message_id = 456
    game.ready_message_main_id = 321
    game.ready_message_game_id = "stop-game"
    game.ready_message_stage = GameState.ROUND_TURN
    game.ready_message_main_text = "Stop?"
    game.seat_announcement_message_id = 212

    context = SimpleNamespace(chat_data={})

    with pytest.raises(UserException):
        await game_engine_setup.engine.stop_game(
            context=context,
            game=game,
            chat_id=-123,
            requester_id=player.user_id,
        )

    assert player.ready_message_id is None
    assert getattr(game, "anchor_message_id", None) is None
    assert game.board_message_id is None
    assert game.ready_message_main_id is None
    assert game.ready_message_game_id is None
    assert game.ready_message_stage is None
    assert game.ready_message_main_text == ""
    assert game.seat_announcement_message_id is None
    assert game.message_ids_to_delete == []
    game_engine_setup.table_manager.save_game.assert_awaited_once_with(-123, game)


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
    game = Game()

    updated = await game_engine_setup.engine._update_votes_and_message(
        context=context,
        game=game,
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
    call_kwargs = (
        game_engine_setup.telegram_safe_ops.edit_message_text.await_args.kwargs
    )
    log_extra = call_kwargs["log_extra"]
    assert log_extra["chat_id"] == -10
    assert log_extra["message_id"] == 5
    assert log_extra["game_id"] == game.id
    assert log_extra["operation"] == "stop_vote_request_message"
    assert log_extra["request_category"] == RequestCategory.GENERAL.value


@pytest.mark.asyncio
async def test_resume_stop_vote_uses_safe_ops_log_extra(game_engine_setup):
    engine = game_engine_setup.engine
    game = Game()
    stop_request = {"game_id": game.id, "message_id": 77}
    context = SimpleNamespace(
        chat_data={engine.KEY_STOP_REQUEST: dict(stop_request)}
    )

    await engine.resume_stop_vote(
        context=context,
        game=game,
        chat_id=-20,
    )

    game_engine_setup.telegram_safe_ops.edit_message_text.assert_awaited_once()
    call_kwargs = (
        game_engine_setup.telegram_safe_ops.edit_message_text.await_args.kwargs
    )
    log_extra = call_kwargs["log_extra"]
    assert log_extra["chat_id"] == -20
    assert log_extra["message_id"] == 77
    assert log_extra["game_id"] == game.id
    assert log_extra["operation"] == "stop_vote_resume_message"
    assert log_extra["request_category"] == RequestCategory.GENERAL.value
    assert engine.KEY_STOP_REQUEST not in context.chat_data


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


@pytest.mark.asyncio
async def test_finalize_stop_request_logs_retry_details(caplog):
    class FlakyView:
        def __init__(self):
            self.edit_attempts = 0

        async def edit_message_text(
            self,
            *,
            chat_id,
            message_id,
            text,
            reply_markup,
            request_category,
            parse_mode,
            suppress_exceptions,
        ):
            self.edit_attempts += 1
            if self.edit_attempts == 1:
                raise RetryAfter(0)
            return message_id

        async def delete_message(self, chat_id, message_id, suppress_exceptions=False):
            return True

    view = FlakyView()
    table_manager = MagicMock()
    table_manager.save_game = AsyncMock()
    request_metrics = MagicMock()
    request_metrics.end_cycle = AsyncMock()
    stats_reporter = MagicMock()
    stats_reporter.invalidate_players = AsyncMock()
    player_manager = MagicMock()
    player_manager.clear_player_anchors = AsyncMock()
    logger = logging.getLogger("test.game_engine.safeops")

    telegram_safe_ops = TelegramSafeOps(
        view,
        logger=logger,
        max_retries=2,
        base_delay=0.05,
        max_delay=0.05,
        backoff_multiplier=2.0,
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
        logger=logger,
    )

    stop_request = {
        "game_id": "game-1",
        "message_id": 55,
        "active_players": set(),
        "votes": set(),
        "manager_override": False,
    }
    context = SimpleNamespace(
        chat_data={engine.KEY_STOP_REQUEST: dict(stop_request)}
    )
    chat_id = -320

    with caplog.at_level(logging.WARNING, logger=logger.name):
        await engine._finalize_stop_request(
            context=context,
            chat_id=chat_id,
            stop_request=stop_request,
        )

    assert view.edit_attempts == 2
    warning_records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "RetryAfter" in record.getMessage()
    ]
    assert warning_records
    warning_record = warning_records[0]
    assert warning_record.chat_id == chat_id
    assert warning_record.message_id == 55
    assert warning_record.request_category == RequestCategory.GENERAL.value
    assert engine.KEY_STOP_REQUEST not in context.chat_data


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
