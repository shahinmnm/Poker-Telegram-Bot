import asyncio
import logging
from typing import Optional

import fakeredis
import fakeredis.aioredis
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


class DummyTableManager:
    def __init__(self, game: Game) -> None:
        self._game = game
        self.save_count = 0

    async def load_game(self, chat_id: int):
        return self._game, None

    async def save_game(self, chat_id: int, game: Game) -> None:
        self._game = game
        self.save_count += 1


class DummyView:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> Optional[int]:
        self.messages.append((chat_id, text))
        return None


class DummySafeOps:
    def __init__(self, view: DummyView) -> None:
        self._view = view
        self.calls: list[tuple[int, str]] = []

    async def send_message_safe(
        self,
        *,
        call,
        chat_id: int,
        operation: Optional[str] = None,
        log_extra: Optional[dict] = None,
    ):
        self.calls.append((chat_id, operation or "send_message"))
        return await call()


@pytest.fixture
def redis_pool():
    server = fakeredis.FakeServer()
    return fakeredis.aioredis.FakeRedis(server=server)


@pytest.mark.asyncio
async def test_action_lock_prevents_duplicate(redis_pool) -> None:
    manager = LockManager(logger=logging.getLogger("lock-prevent"), redis_pool=redis_pool)
    chat_id, user_id = 123, 456

    token1 = await manager.acquire_action_lock(chat_id, user_id, "fold")
    token2 = await manager.acquire_action_lock(chat_id, user_id, "fold")

    assert token1 is not None
    assert token2 is None

    await manager.release_action_lock(chat_id, user_id, "fold", token1)


@pytest.mark.asyncio
async def test_action_lock_allows_different_users(redis_pool) -> None:
    manager = LockManager(logger=logging.getLogger("lock-multi"), redis_pool=redis_pool)
    chat_id = 987

    token_alice = await manager.acquire_action_lock(chat_id, 100, "fold")
    token_bob = await manager.acquire_action_lock(chat_id, 200, "call")

    assert token_alice is not None
    assert token_bob is not None

    await manager.release_action_lock(chat_id, 100, "fold", token_alice)
    await manager.release_action_lock(chat_id, 200, "call", token_bob)


@pytest.mark.asyncio
async def test_action_lock_expires_after_ttl(redis_pool) -> None:
    manager = LockManager(logger=logging.getLogger("lock-ttl"), redis_pool=redis_pool)
    chat_id, user_id = 222, 333

    await manager.acquire_action_lock(chat_id, user_id, "raise", ttl=1)
    await asyncio.sleep(1.1)
    token2 = await manager.acquire_action_lock(chat_id, user_id, "raise")

    assert token2 is not None

    await manager.release_action_lock(chat_id, user_id, "raise", token2)


@pytest.mark.asyncio
async def test_action_lock_release_validation(redis_pool) -> None:
    manager = LockManager(logger=logging.getLogger("lock-release"), redis_pool=redis_pool)
    chat_id, user_id = 111, 222

    token = await manager.acquire_action_lock(chat_id, user_id, "check")
    assert token is not None

    released_wrong = await manager.release_action_lock(chat_id, user_id, "check", token="wrong")
    released_correct = await manager.release_action_lock(chat_id, user_id, "check", token)

    assert released_wrong is False
    assert released_correct is True


@pytest.mark.asyncio
async def test_game_engine_rejects_duplicate_action(redis_pool) -> None:
    from pokerapp.game_engine import GameEngine

    logger = logging.getLogger("engine-action")
    lock_manager = LockManager(logger=logger, redis_pool=redis_pool)

    game = Game()
    chat_id, user_id = 777, 888
    game.chat_id = chat_id
    wallet = DummyWallet()
    player = Player(user_id, "Player", wallet, "ready")
    game.add_player(player, seat_index=0)
    game.current_player_index = 0
    game.turn_deadline = asyncio.get_running_loop().time() + 5

    table_manager = DummyTableManager(game)
    view = DummyView()
    safe_ops = DummySafeOps(view)

    engine = GameEngine.__new__(GameEngine)
    engine._lock_manager = lock_manager
    engine._table_manager = table_manager
    engine._safe_ops = safe_ops
    engine._telegram_ops = safe_ops
    engine._view = view
    engine._logger = logger
    engine._valid_player_actions = {"fold", "check", "call", "raise"}
    engine._action_lock_ttl = 1
    engine._action_lock_feedback_text = "⚠️ Action in progress, please wait..."

    task1 = asyncio.create_task(engine.process_action(chat_id, user_id, "fold"))
    task2 = asyncio.create_task(engine.process_action(chat_id, user_id, "fold"))

    results = await asyncio.gather(task1, task2)

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert table_manager.save_count == 1
    assert any(chat == user_id for chat, _ in view.messages)
