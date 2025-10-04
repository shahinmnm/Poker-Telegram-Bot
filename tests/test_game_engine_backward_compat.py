"""Backward compatibility tests for the snapshot-based game engine paths."""

import warnings
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.entities import ChatId, Game
from pokerapp.game_engine import GameEngine


@pytest.fixture
def engine_dependencies() -> Dict[str, Any]:
    async def clear_game_messages(game: Game, chat_id: ChatId, **_: Any) -> None:
        return None

    def build_identity_from_player(player: Any) -> Any:
        return player

    def safe_int(value: Any) -> int:
        return int(value)

    view = MagicMock()
    view.send_message = AsyncMock(return_value=None)
    view.delete_message = AsyncMock(return_value=None)

    deps: Dict[str, Any] = {
        "table_manager": MagicMock(),
        "view": view,
        "winner_determination": MagicMock(),
        "request_metrics": MagicMock(),
        "round_rate": MagicMock(),
        "player_manager": MagicMock(),
        "matchmaking_service": MagicMock(),
        "stats_reporter": MagicMock(),
        "clear_game_messages": clear_game_messages,
        "build_identity_from_player": build_identity_from_player,
        "safe_int": safe_int,
        "old_players_key": "test:old_players",
        "telegram_safe_ops": MagicMock(),
        "lock_manager": MagicMock(),
        "logger": MagicMock(),
        "constants": None,
        "adaptive_player_report_cache": None,
        "player_factory": None,
    }
    return deps


@pytest.fixture
def game_engine(engine_dependencies: Dict[str, Any]) -> GameEngine:
    return GameEngine(**engine_dependencies)


@pytest.mark.asyncio
async def test_progress_stage_legacy_path_warns(game_engine: GameEngine) -> None:
    legacy = AsyncMock(return_value=True)
    game_engine._progress_stage_legacy = legacy  # type: ignore[attr-defined]

    context = MagicMock()
    game = Game()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await game_engine.progress_stage(
            context=context,
            chat_id=ChatId(1234),
            game=game,
        )

    legacy.assert_awaited_once_with(
        context=context,
        chat_id=ChatId(1234),
        game=game,
    )
    assert result is True
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)


@pytest.mark.asyncio
async def test_progress_stage_requires_chat_id(game_engine: GameEngine) -> None:
    with pytest.raises(ValueError):
        await game_engine.progress_stage()


@pytest.mark.asyncio
async def test_progress_stage_snapshot_path_no_warning(game_engine: GameEngine) -> None:
    snapshot = AsyncMock(return_value=True)
    game_engine._progress_stage_snapshot = snapshot  # type: ignore[attr-defined]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await game_engine.progress_stage(chat_id=ChatId(4321))

    snapshot.assert_awaited_once_with(ChatId(4321))
    assert result is True
    assert caught == []


@pytest.mark.asyncio
async def test_finalize_game_returns_none(game_engine: GameEngine) -> None:
    finalize_snapshot = AsyncMock(return_value=True)
    game_engine._finalize_game_snapshot = finalize_snapshot  # type: ignore[attr-defined]

    result = await game_engine.finalize_game(chat_id=ChatId(999))

    finalize_snapshot.assert_awaited_once_with(ChatId(999))
    assert result is None


@pytest.mark.asyncio
async def test_finalize_game_legacy_warns(game_engine: GameEngine) -> None:
    finalize_legacy = AsyncMock(return_value=None)
    game_engine._finalize_game_legacy = finalize_legacy  # type: ignore[attr-defined]

    context = MagicMock()
    game = Game()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await game_engine.finalize_game(
            context=context,
            game=game,
            chat_id=ChatId(555),
        )

    finalize_legacy.assert_awaited_once_with(
        context=context,
        game=game,
        chat_id=ChatId(555),
    )
    assert result is None
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)


@pytest.mark.asyncio
async def test_evaluate_winner_prefers_determination_service(
    game_engine: GameEngine,
) -> None:
    winner = MagicMock()
    determination = MagicMock()
    determination.determine_winner.return_value = winner
    game_engine._winner_selector = None  # type: ignore[attr-defined]
    game_engine._winner_determination = determination  # type: ignore[assignment]

    game = Game()
    result = game_engine._evaluate_winner_snapshot(game)  # type: ignore[attr-defined]

    determination.determine_winner.assert_called_once_with(game)
    assert result is winner
