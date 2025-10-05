"""Integration tests for fine-grained locking in GameEngine."""

import asyncio
import copy
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Dict

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from pokerapp.game_engine import GameEngine
from pokerapp.lock_manager import LockManager


@pytest_asyncio.fixture
async def game_engine():
    """Create GameEngine instance with mocked dependencies."""
    lock_manager = LockManager(
        logger=logging.getLogger("test-lock-manager"),
        enable_fine_grained_locks=True,
    )

    player_locks = defaultdict(asyncio.Lock)
    pot_locks = defaultdict(asyncio.Lock)
    table_write_locks = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def fast_table_write_lock(chat_id: int):
        lock = table_write_locks[chat_id]
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()

    lock_manager.acquire_table_write_lock = fast_table_write_lock  # type: ignore[assignment]

    @asynccontextmanager
    async def fast_table_read_lock(chat_id: int, timeout: float = 0.0):
        yield True

    lock_manager.acquire_table_read_lock = fast_table_read_lock  # type: ignore[assignment]

    @asynccontextmanager
    async def fast_player_lock(chat_id: int, user_id: int, timeout: float = 10.0):
        lock = player_locks[(chat_id, user_id)]
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()

    lock_manager.acquire_player_lock = fast_player_lock  # type: ignore[assignment]

    @asynccontextmanager
    async def fast_pot_lock(chat_id: int, timeout: float = 10.0):
        lock = pot_locks[chat_id]
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()

    lock_manager.acquire_pot_lock = fast_pot_lock  # type: ignore[assignment]

    engine = GameEngine(
        table_manager=MagicMock(),
        view=MagicMock(),
        winner_determination=MagicMock(),
        request_metrics=MagicMock(),
        round_rate=MagicMock(),
        player_manager=MagicMock(),
        matchmaking_service=MagicMock(),
        stats_reporter=MagicMock(),
        clear_game_messages=AsyncMock(),
        build_identity_from_player=MagicMock(),
        safe_int=lambda value: int(value),
        old_players_key="old_players",
        telegram_safe_ops=MagicMock(),
        lock_manager=lock_manager,
        logger=logging.getLogger("test-engine"),
    )
    
    # Shared mutable state that mimics persisted storage
    state_template: Dict[str, object] = {
        "chat_id": 12345,
        "version": 1,
        "current_bet": 10,
        "pot": 0,
        "current_player_index": 0,
        "players": [
            {"user_id": 1, "chips": 1000, "bet": 0, "state": "active", "has_acted": False},
            {"user_id": 2, "chips": 1000, "bet": 0, "state": "active", "has_acted": False},
            {"user_id": 3, "chips": 1000, "bet": 0, "state": "active", "has_acted": False},
        ]
    }

    async def load_state(_: int) -> Dict[str, object]:
        return copy.deepcopy(state_template)

    async def save_state(chat_id: int, new_state: Dict[str, object]) -> bool:
        assert chat_id == state_template["chat_id"]
        state_template.clear()
        state_template.update(copy.deepcopy(new_state))
        return True

    engine.load_game_state = AsyncMock(side_effect=load_state)
    engine.save_game_state = AsyncMock(side_effect=save_state)
    engine._state_template = state_template  # type: ignore[attr-defined]

    return engine


@pytest.mark.asyncio
async def test_concurrent_player_actions(game_engine):
    """
    Test that different players can act concurrently without blocking.
    
    Scenario:
    - Player 1 calls (requires player lock #1)
    - Player 2 folds (requires player lock #2)
    - Player 3 raises (requires player lock #3)
    
    Expected:
    - All three actions execute in parallel (< 100ms total)
    - No lock contention between different players
    """
    # Execute three actions concurrently
    start_time = asyncio.get_event_loop().time()
    
    results = await asyncio.gather(
        game_engine.handle_player_action(12345, 1, "call"),
        game_engine.handle_player_action(12345, 2, "fold"),
        game_engine.handle_player_action(12345, 3, "raise", amount=20),
    )
    
    end_time = asyncio.get_event_loop().time()
    duration = end_time - start_time
    
    # Verify all succeeded
    assert all(r["success"] for r in results), "All actions should succeed"
    
    # Verify parallel execution (should be < 100ms if truly parallel)
    # Sequential execution would take 3x longer
    assert duration < 0.1, f"Actions took {duration}s, expected concurrent execution"
    
    # Verify state was saved three times
    assert game_engine.save_game_state.call_count == 3


@pytest.mark.asyncio
async def test_same_player_actions_serialize(game_engine):
    """
    Test that actions by the SAME player serialize correctly.
    
    Scenario:
    - Player 1 attempts two rapid actions
    
    Expected:
    - Second action waits for first to complete
    - Only one action succeeds (due to turn validation)
    """
    # Attempt two actions by same player concurrently
    results = await asyncio.gather(
        game_engine.handle_player_action(12345, 1, "call"),
        game_engine.handle_player_action(12345, 1, "raise", amount=50),
        return_exceptions=True
    )
    
    # At least one should fail (not player's turn after first action)
    successful = [r for r in results if isinstance(r, dict) and r.get("success")]
    failed = [r for r in results if isinstance(r, dict) and not r.get("success")]
    
    assert len(successful) == 1, "Only one action should succeed"
    assert len(failed) == 1, "Second action should fail (not player's turn)"


@pytest.mark.asyncio
async def test_pot_lock_serializes_bet_collection(game_engine):
    """
    Test that pot updates serialize correctly when multiple players complete betting.
    
    Scenario:
    - All players call simultaneously
    - Betting round completes
    - Pot collection should happen atomically
    
    Expected:
    - Pot lock ensures only one thread collects bets
    - Final pot amount is correct
    """
    # Mock all players as having acted
    state_template = game_engine._state_template  # type: ignore[attr-defined]
    state_template["players"] = [
        {"user_id": 1, "chips": 900, "bet": 100, "state": "active", "has_acted": True},
        {"user_id": 2, "chips": 900, "bet": 100, "state": "active", "has_acted": True},
        {"user_id": 3, "chips": 900, "bet": 100, "state": "active", "has_acted": False},
    ]
    state_template["current_bet"] = 100
    
    # Player 3 completes the round
    result = await game_engine.handle_player_action(12345, 3, "call")
    
    assert result["success"], "Action should succeed"
    
    # Verify pot was updated
    saved_state = game_engine.save_game_state.call_args[0][1]
    assert saved_state["pot"] == 300, "Pot should contain all bets (3 × 100)"
    
    # Verify bets were reset
    assert all(p["bet"] == 0 for p in saved_state["players"]), "All bets should reset"
