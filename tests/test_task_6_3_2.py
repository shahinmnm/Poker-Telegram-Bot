import asyncio
import logging

import pytest

from pokerapp.entities import Game, Player
from pokerapp.lock_manager import LockManager


class DummyWallet:
    def __init__(self) -> None:
        self.authorizations: list[tuple[str, int]] = []

    async def authorize(self, game_id: str, amount: int) -> None:
        self.authorizations.append((game_id, amount))

    async def value(self) -> int:
        return 1_000


@pytest.mark.asyncio
async def test_action_lock_prevents_duplicate(redis_pool) -> None:
    """Test that acquiring the same lock twice blocks the second attempt."""

    manager = LockManager(logger=logging.getLogger("lock-prevent"), redis_pool=redis_pool)
    chat_id, user_id = 123, 456

    token1 = await manager.acquire_action_lock(chat_id, user_id, "fold")
    assert token1 is not None

    token2 = await manager.acquire_action_lock(chat_id, user_id, "fold")
    assert token2 is None

    released = await manager.release_action_lock(chat_id, user_id, "fold", token1)
    assert released is True

    token3 = await manager.acquire_action_lock(chat_id, user_id, "fold")
    assert token3 is not None
    await manager.release_action_lock(chat_id, user_id, "fold", token3)


@pytest.mark.asyncio
async def test_action_lock_allows_different_users(redis_pool) -> None:
    """Test that different users can acquire locks simultaneously."""

    manager = LockManager(logger=logging.getLogger("lock-multi"), redis_pool=redis_pool)
    chat_id = 987

    token_alice = await manager.acquire_action_lock(chat_id, 100, "fold")
    token_bob = await manager.acquire_action_lock(chat_id, 200, "call")

    assert token_alice is not None
    assert token_bob is not None

    released_alice = await manager.release_action_lock(chat_id, 100, "fold", token_alice)
    released_bob = await manager.release_action_lock(chat_id, 200, "call", token_bob)

    assert released_alice is True
    assert released_bob is True


@pytest.mark.asyncio
async def test_action_lock_expires_after_ttl(redis_pool) -> None:
    """Test that lock auto-expires and can be reacquired."""

    manager = LockManager(logger=logging.getLogger("lock-ttl"), redis_pool=redis_pool)
    chat_id, user_id = 222, 333

    token1 = await manager.acquire_action_lock(chat_id, user_id, "raise", ttl=1)
    assert token1 is not None

    await asyncio.sleep(1.1)

    token2 = await manager.acquire_action_lock(chat_id, user_id, "raise")
    assert token2 is not None

    await manager.release_action_lock(chat_id, user_id, "raise", token2)


@pytest.mark.asyncio
async def test_action_lock_release_validation(redis_pool) -> None:
    """Test that lock release validates the token."""

    manager = LockManager(logger=logging.getLogger("lock-release"), redis_pool=redis_pool)
    chat_id, user_id = 111, 222

    token = await manager.acquire_action_lock(chat_id, user_id, "check")
    assert token is not None

    released_wrong = await manager.release_action_lock(
        chat_id, user_id, "check", "wrong-token-12345"
    )
    assert released_wrong is False

    released_correct = await manager.release_action_lock(chat_id, user_id, "check", token)
    assert released_correct is True

    released_again = await manager.release_action_lock(chat_id, user_id, "check", token)
    assert released_again is False


@pytest.mark.asyncio
async def test_game_engine_rejects_duplicate_action(game_engine_factory) -> None:
    game = Game()
    chat_id, user_id = 777, 888
    game.chat_id = chat_id
    wallet = DummyWallet()
    player = Player(user_id, "Player", wallet, "ready")
    game.add_player(player, seat_index=0)
    game.current_player_index = 0
    game.turn_deadline = asyncio.get_running_loop().time() + 5

    engine, table_manager, view = game_engine_factory(
        game=game, logger_name="engine-action"
    )

    task1 = asyncio.create_task(engine.process_action(chat_id, user_id, "fold"))
    task2 = asyncio.create_task(engine.process_action(chat_id, user_id, "fold"))

    results = await asyncio.gather(task1, task2)

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert table_manager.save_count == 1
    assert any(chat == user_id for chat, _ in view.messages)


@pytest.mark.asyncio
async def test_turn_deadline_enforcement(game_engine_factory) -> None:
    game = Game()
    chat_id, user_id = 123, 456
    game.chat_id = chat_id
    wallet = DummyWallet()
    player = Player(user_id, "Player", wallet, "ready")
    game.add_player(player, seat_index=0)
    game.current_player_index = 0

    engine, table_manager, _ = game_engine_factory(
        game=game, logger_name="engine-deadline"
    )

    loop = asyncio.get_running_loop()
    game.turn_deadline = loop.time() - 10

    success = await engine.process_action(chat_id, user_id, "fold")

    assert success is False
    assert table_manager.save_count == 0
