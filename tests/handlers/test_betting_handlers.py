"""Regression tests for betting flow handlers.

These tests exercise the poker model's betting flow helper to ensure it
no longer relies on the deprecated ``GameEngine.progress_stage``
signature that accepted ``context`` and ``game`` arguments.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.pokerbotmodel import PokerBotModel


@pytest.mark.asyncio
async def test_progress_stage_called_with_chat_id_only_single_contender():
    model = object.__new__(PokerBotModel)
    progress_stage = AsyncMock(return_value=False)
    model.game_service = SimpleNamespace(progress_stage=progress_stage)
    model._round_rate = SimpleNamespace(
        _find_next_active_player_index=MagicMock(return_value=-1)
    )
    model._is_betting_round_over = MagicMock(return_value=False)

    game = SimpleNamespace(
        turn_message_id=None,
        players_by=lambda states: [object()],
        current_player_index=0,
    )

    result = await PokerBotModel._process_playing(model, chat_id=42, game=game)

    assert result is None
    progress_stage.assert_awaited_once_with(chat_id=42)


@pytest.mark.asyncio
async def test_progress_stage_called_with_chat_id_only_when_round_over():
    model = object.__new__(PokerBotModel)
    progress_stage = AsyncMock(return_value=False)
    model.game_service = SimpleNamespace(progress_stage=progress_stage)
    model._round_rate = SimpleNamespace(
        _find_next_active_player_index=MagicMock(return_value=-1)
    )
    model._is_betting_round_over = MagicMock(return_value=True)

    game = SimpleNamespace(
        turn_message_id=None,
        players_by=lambda states: [object(), object()],
        current_player_index=0,
    )

    result = await PokerBotModel._process_playing(model, chat_id=-101, game=game)

    assert result is None
    progress_stage.assert_awaited_once_with(chat_id=-101)


@pytest.mark.asyncio
async def test_progress_stage_called_with_chat_id_only_when_all_in():
    model = object.__new__(PokerBotModel)
    progress_stage = AsyncMock(return_value=False)
    model.game_service = SimpleNamespace(progress_stage=progress_stage)
    model._round_rate = SimpleNamespace(
        _find_next_active_player_index=MagicMock(return_value=-1)
    )
    model._is_betting_round_over = MagicMock(return_value=False)

    # Two contenders so the first branch is skipped, but betting round is not
    # over and the search for the next active player returns -1, forcing the
    # final progress_stage call.
    game = SimpleNamespace(
        turn_message_id=None,
        players_by=lambda states: [object(), object()],
        current_player_index=0,
    )

    result = await PokerBotModel._process_playing(model, chat_id=7, game=game)

    assert result is None
    progress_stage.assert_awaited_once_with(chat_id=7)
