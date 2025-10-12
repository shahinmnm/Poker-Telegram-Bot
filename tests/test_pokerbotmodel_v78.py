import logging
from dataclasses import dataclass
from typing import Iterable, List

import pytest

from pokerapp.pokerbotmodel import PokerBotModel


@dataclass
class _DummyPlayer:
    user_id: int


class _DummyGame:
    def __init__(self, players: Iterable[_DummyPlayer]):
        self._players: List[_DummyPlayer] = list(players)
        self.ready_users = set()

    def seated_players(self) -> List[_DummyPlayer]:
        return list(self._players)


@pytest.fixture
def model_with_game():
    players = [_DummyPlayer(100), _DummyPlayer(200)]
    game = _DummyGame(players)

    model = PokerBotModel.__new__(PokerBotModel)
    logger = logging.getLogger(f"test.pokerbotmodel.prune.{id(game)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    model._logger = logger

    return model, game


class TestPruneReadySeatsV78:
    """Test suite for the _prune_ready_seats iteration bug fix."""

    @pytest.mark.asyncio
    async def test_prune_does_not_raise_runtime_error(self, model_with_game):
        model, game = model_with_game
        chat_id = -123

        game.ready_users = {100, 200, 888, 999}

        ready_players = await model._prune_ready_seats(game, chat_id)

        assert {player.user_id for player in ready_players} == {100, 200}
        assert game.ready_users == {100, 200}

    @pytest.mark.asyncio
    async def test_prune_removes_only_unseated_users(self, model_with_game):
        model, game = model_with_game
        chat_id = -456

        game.ready_users = {100, 777, 888}

        ready_players = await model._prune_ready_seats(game, chat_id)

        assert {player.user_id for player in ready_players} == {100}
        assert game.ready_users == {100}

    @pytest.mark.asyncio
    async def test_prune_handles_empty_ready_set(self, model_with_game):
        model, game = model_with_game
        chat_id = -789

        game.ready_users = set()

        ready_players = await model._prune_ready_seats(game, chat_id)

        assert ready_players == []
        assert game.ready_users == set()

    @pytest.mark.asyncio
    async def test_prune_handles_all_stale_ready_users(self, model_with_game):
        model, game = model_with_game
        chat_id = -321

        game.ready_users = {777, 888, 999}

        ready_players = await model._prune_ready_seats(game, chat_id)

        assert ready_players == []
        assert game.ready_users == set()

    @pytest.mark.asyncio
    async def test_prune_logs_when_stale_users_found(
        self, model_with_game, caplog
    ):
        model, game = model_with_game
        chat_id = -654

        game.ready_users = {100, 777, 888}

        with caplog.at_level(logging.INFO, logger=model._logger.name):
            await model._prune_ready_seats(game, chat_id)

        assert "Pruned 2 stale ready flags" in caplog.text
        assert "777" in caplog.text
        assert "888" in caplog.text

    @pytest.mark.asyncio
    async def test_prune_does_not_log_when_no_stale_users(
        self, model_with_game, caplog
    ):
        model, game = model_with_game
        chat_id = -987

        game.ready_users = {100, 200}

        with caplog.at_level(logging.INFO, logger=model._logger.name):
            await model._prune_ready_seats(game, chat_id)

        assert "Pruned" not in caplog.text
