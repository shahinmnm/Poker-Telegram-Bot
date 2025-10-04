import asyncio
import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Awaitable, List

import pytest
from unittest.mock import AsyncMock, MagicMock

from pokerapp.entities import Game, GameState
from pokerapp.game_engine import GameEngine


class _DummyMatchmakingService:
    def __init__(self, timings: dict) -> None:
        self._timings = timings

    async def progress_stage(
        self,
        *,
        context: Any,
        chat_id: int,
        game: Game,
        finalize_game,
        deferred_tasks: List[Awaitable[Any]],
    ) -> bool:
        async def _slow_task() -> None:
            self._timings["deferred_start"] = time.monotonic()
            await asyncio.sleep(0.05)
            self._timings["deferred_end"] = time.monotonic()

        deferred_tasks.append(_slow_task())
        game.state = GameState.ROUND_FLOP
        return True


@pytest.mark.asyncio
async def test_progress_stage_releases_stage_lock_before_deferred_tasks():
    game = Game()
    game.state = GameState.ROUND_PRE_FLOP

    table_manager = MagicMock()
    view = MagicMock()
    winner_determination = MagicMock()
    request_metrics = MagicMock()
    round_rate = MagicMock()
    player_manager = MagicMock()
    stats_reporter = MagicMock()
    clear_game_messages = AsyncMock()
    build_identity_from_player = MagicMock()
    safe_int = int
    telegram_safe_ops = MagicMock()
    lock_manager = MagicMock()
    logger = logging.getLogger("test-progress-stage")

    timings: dict = {}
    matchmaking_service = _DummyMatchmakingService(timings)

    engine = GameEngine(
        table_manager=table_manager,
        view=view,
        winner_determination=winner_determination,
        request_metrics=request_metrics,
        round_rate=round_rate,
        player_manager=player_manager,
        matchmaking_service=matchmaking_service,
        stats_reporter=stats_reporter,
        clear_game_messages=clear_game_messages,
        build_identity_from_player=build_identity_from_player,
        safe_int=safe_int,
        old_players_key="old_players",
        telegram_safe_ops=telegram_safe_ops,
        lock_manager=lock_manager,
        logger=logger,
    )

    lock_timings: dict = {}

    @asynccontextmanager
    async def fake_guard(self, **kwargs):  # type: ignore[unused-argument]
        lock_timings["start"] = time.monotonic()
        try:
            yield
        finally:
            lock_timings["end"] = time.monotonic()

    engine._trace_lock_guard = fake_guard.__get__(engine, GameEngine)  # type: ignore[assignment]

    context = SimpleNamespace()
    chat_id = -100

    result = await engine.progress_stage(context=context, chat_id=chat_id, game=game)

    assert result is True
    assert "start" in lock_timings and "end" in lock_timings
    assert "deferred_start" in timings and "deferred_end" in timings

    lock_hold = lock_timings["end"] - lock_timings["start"]
    assert lock_hold < 0.1

    assert timings["deferred_start"] >= lock_timings["end"]
    assert timings["deferred_end"] - timings["deferred_start"] >= 0.05
