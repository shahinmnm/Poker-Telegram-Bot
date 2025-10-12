"""
Performance benchmarks for pruning subsystem.
"""
import asyncio
import time
from unittest.mock import MagicMock

import pytest

from pokerapp.entities import Game, Player
from pokerapp.pokerbotmodel import PokerBotModel


@pytest.fixture
def model_factory():
    def _factory():
        model = PokerBotModel.__new__(PokerBotModel)
        model._logger = MagicMock()
        model._metrics = MagicMock()
        model._pruning_health = MagicMock()
        return model

    return _factory


@pytest.mark.performance
class TestPruningPerformance:
    """Benchmark pruning operations under various load conditions."""

    @pytest.mark.asyncio
    async def test_prune_100_stale_users(self, model_factory):
        """Measure pruning performance with 100 stale users."""

        model = model_factory()
        game = Game()
        stale_ids = set(range(100))
        game.ready_users.update(stale_ids)

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            await model._prune_ready_seats(game, 1)
            game.ready_users = set(stale_ids)

        duration = time.perf_counter() - start
        avg_ms = (duration / iterations) * 1000

        print(f"\n📊 Prune 100 stale users: {avg_ms:.2f}ms avg")
        assert avg_ms < 5.0, f"Pruning too slow: {avg_ms}ms"

    @pytest.mark.asyncio
    async def test_prune_mixed_stale_active(self, model_factory):
        """Measure pruning with mixed stale/active ratio."""

        model = model_factory()
        game = Game()

        active_count = min(50, len(game.seats))

        for i in range(active_count):
            player = Player(
                user_id=i,
                mention_markdown=f"user{i}",
                wallet=MagicMock(),
                ready_message_id="",
            )
            game.add_player(player)

        stale_count = max(0, 100 - active_count)
        game.ready_users.update(range(active_count + stale_count))

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            ready_players = await model._prune_ready_seats(game, 1)
            assert len(ready_players) == active_count
            if stale_count:
                game.ready_users.update(
                    range(active_count, active_count + stale_count)
                )

        duration = time.perf_counter() - start
        avg_ms = (duration / iterations) * 1000

        print(f"\n📊 Prune 50/50 mix: {avg_ms:.2f}ms avg")
        assert avg_ms < 3.0, f"Mixed pruning too slow: {avg_ms}ms"

    @pytest.mark.asyncio
    async def test_concurrent_prune_operations(self, model_factory):
        """Measure concurrent pruning across multiple games."""

        model = model_factory()
        games = []

        for game_id in range(10):
            game = Game()
            game.ready_users.update(range(20))
            games.append((game_id, game))

        async def prune_game(chat_id: int, game: Game):
            return await model._prune_ready_seats(game, chat_id)

        iterations = 100
        start = time.perf_counter()

        for _ in range(iterations):
            await asyncio.gather(*(prune_game(chat_id, game) for chat_id, game in games))
            for _, game in games:
                game.ready_users = set(range(20))

        duration = time.perf_counter() - start
        total_ops = iterations * len(games)
        avg_ms = (duration / total_ops) * 1000

        print(f"\n📊 Concurrent prune (10 games): {avg_ms:.2f}ms avg per game")
        assert avg_ms < 2.0, f"Concurrent pruning too slow: {avg_ms}ms"
