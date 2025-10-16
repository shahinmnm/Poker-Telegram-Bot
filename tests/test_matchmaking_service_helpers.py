from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import inspect

import pytest

from pokerapp.config import Config
from pokerapp.entities import Game, GameState, Player
from pokerapp.matchmaking_service import MatchmakingService


class DummyLockManager:
    def __init__(self) -> None:
        self.calls = []

    @asynccontextmanager
    async def _guard(self, key: str, timeout: int):
        self.calls.append((key, timeout))
        yield

    def guard(self, key: str, timeout: int):
        return self._guard(key, timeout)


@pytest.fixture
def matchmaking_setup():
    view = MagicMock()
    view.send_player_role_anchors = AsyncMock()

    round_rate = MagicMock()
    round_rate.set_blinds = AsyncMock()

    request_metrics = MagicMock()
    request_metrics.start_cycle = AsyncMock()

    player_manager = MagicMock()
    player_manager.clear_seat_announcement = AsyncMock()
    player_manager.clear_player_anchors = AsyncMock()
    player_manager.assign_role_labels = MagicMock()

    stats_reporter = MagicMock()
    stats_reporter.invalidate_players = AsyncMock()

    send_turn_message = AsyncMock()

    lock_manager = DummyLockManager()

    service = MatchmakingService(
        view=view,
        round_rate=round_rate,
        request_metrics=request_metrics,
        player_manager=player_manager,
        stats_reporter=stats_reporter,
        lock_manager=lock_manager,
        send_turn_message=send_turn_message,
        safe_int=int,
        old_players_key="old_players",
        logger=MagicMock(),
    )

    return SimpleNamespace(
        service=service,
        view=view,
        round_rate=round_rate,
        request_metrics=request_metrics,
        player_manager=player_manager,
        stats_reporter=stats_reporter,
        send_turn_message=send_turn_message,
        lock_manager=lock_manager,
    )


def test_ensure_dealer_position_requires_seated_players(matchmaking_setup):
    service = matchmaking_setup.service
    service._logger.warning = MagicMock()

    game = Game()
    game.seats = [None for _ in game.seats]
    game.dealer_index = -1

    assert service._ensure_dealer_position(game) is False
    service._logger.warning.assert_called_once()


def test_ensure_dealer_position_advances_to_occupied_seat(matchmaking_setup):
    service = matchmaking_setup.service
    game = Game()
    player = Player(
        user_id=1,
        mention_markdown="@p",
        wallet=MagicMock(),
        ready_message_id="r",
    )
    game.add_player(player, seat_index=0)

    assert service._ensure_dealer_position(game) is True
    assert game.dealer_index == 0


def test_ensure_dealer_position_allows_debug_dummy(monkeypatch, matchmaking_setup):
    monkeypatch.setenv("POKERBOT_ALLOW_EMPTY_DEALER", "1")
    cfg = Config()
    logger = MagicMock()

    service = MatchmakingService(
        view=matchmaking_setup.view,
        round_rate=matchmaking_setup.round_rate,
        request_metrics=matchmaking_setup.request_metrics,
        player_manager=matchmaking_setup.player_manager,
        stats_reporter=matchmaking_setup.stats_reporter,
        lock_manager=matchmaking_setup.lock_manager,
        send_turn_message=matchmaking_setup.send_turn_message,
        safe_int=int,
        old_players_key="old_players",
        logger=logger,
        config=cfg,
    )

    game = Game()
    game.seats = [None for _ in game.seats]
    game.dealer_index = -1

    assert service._ensure_dealer_position(game) is True
    assert game.dealer_index == 0
    assert isinstance(game.seats[0], Player)
    assert logger.debug.called


@pytest.mark.asyncio
async def test_initialize_hand_state_sets_state_and_metrics(matchmaking_setup):
    service = matchmaking_setup.service
    game = Game()

    await service._initialize_hand_state(game, "-55")

    assert game.state == GameState.ROUND_PRE_FLOP
    matchmaking_setup.request_metrics.start_cycle.assert_awaited_once_with(
        -55, game.id
    )


@pytest.mark.asyncio
async def test_clear_seat_state_invokes_player_manager(matchmaking_setup):
    service = matchmaking_setup.service
    game = Game()

    await service._clear_seat_state(game, -100)

    matchmaking_setup.player_manager.clear_seat_announcement.assert_awaited_once_with(
        game, -100
    )
    matchmaking_setup.player_manager.clear_player_anchors.assert_awaited_once_with(game)


@pytest.mark.asyncio
async def test_deal_hole_cards_delegates_to_divide(matchmaking_setup):
    service = matchmaking_setup.service
    service._divide_cards = AsyncMock()

    game = Game()
    await service._deal_hole_cards(game, -200)

    service._divide_cards.assert_awaited_once_with(game, -200)


@pytest.mark.asyncio
async def test_post_blinds_assigns_roles_and_invalidates(matchmaking_setup):
    service = matchmaking_setup.service
    game = Game()
    player = Player(
        user_id=1,
        mention_markdown="@p",
        wallet=MagicMock(),
        ready_message_id="r",
    )
    game.add_player(player, seat_index=0)

    matchmaking_setup.round_rate.set_blinds.return_value = player

    current = await service._post_blinds_and_prepare_players(game, -300)

    assert current is player
    matchmaking_setup.player_manager.assign_role_labels.assert_called_once_with(game)
    matchmaking_setup.stats_reporter.invalidate_players.assert_awaited_once_with(
        game.players, chat_id=-300
    )


@pytest.mark.asyncio
async def test_handle_post_start_notifications_sends_updates(matchmaking_setup):
    service = matchmaking_setup.service
    game = Game()
    player = Player(
        user_id=1,
        mention_markdown="@p",
        wallet=MagicMock(),
        ready_message_id="r",
    )
    game.add_player(player, seat_index=0)

    context = SimpleNamespace(chat_data={})
    notification = await service._handle_post_start_notifications(
        context=context,
        game=game,
        chat_id=-400,
        current_player=player,
    )

    assert game.chat_id == -400
    matchmaking_setup.view.send_player_role_anchors.assert_awaited_once()
    matchmaking_setup.send_turn_message.assert_called_once_with(game, player, -400)
    assert inspect.isawaitable(notification)
    await notification
    assert context.chat_data["old_players"] == [player.user_id]
    assert game.last_actions[-1] == "بازی شروع شد"

