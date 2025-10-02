import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
import fakeredis
import fakeredis.aioredis
import redis.asyncio as aioredis

from pokerapp.aiogram_flow import protect_against_races
from pokerapp.entities import Game, GameState
from pokerapp.lock_manager import LockManager
from pokerapp.utils.messaging_service import MessagingService


class DummyCallback:
    def __init__(self, chat_id: int, user_id: int, data: str, callback_id: str = "cb-1") -> None:
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.from_user = SimpleNamespace(id=user_id)
        self.id = callback_id
        self.data = data
        self.answer = AsyncMock()


class StubTableManager:
    def __init__(self, loads, *, save_result: bool = True) -> None:
        self._loads = iter(loads)
        self.save_calls = []
        self.save_result = save_result

    async def load_game_with_version(self, chat_id: int):
        return next(self._loads)

    async def save_game_with_version_check(self, chat_id: int, game: Game, version: int) -> bool:
        self.save_calls.append((chat_id, game, version))
        return self.save_result


class StubLockManager:
    def __init__(self, tokens) -> None:
        self._tokens = iter(tokens)
        self.acquire_calls = []
        self.release_calls = []

    async def acquire_action_lock(
        self,
        chat_id: int,
        user_id: int,
        *,
        action_data: Optional[str] = None,
        timeout_seconds: int = 5,
    ):
        self.acquire_calls.append((chat_id, user_id, action_data, timeout_seconds))
        return next(self._tokens, None)

    async def release_action_lock(
        self,
        chat_id: int,
        user_id: int,
        token: str,
        *,
        action_data: Optional[str] = None,
    ) -> bool:
        self.release_calls.append((chat_id, user_id, token, action_data))
        return True


def test_mark_callback_processed_rejects_duplicates() -> None:
    game = Game()
    first = game.mark_callback_processed("abc")
    second = game.mark_callback_processed("abc")

    assert first is True
    assert second is False


def test_mark_callback_processed_limits_history() -> None:
    game = Game()
    for index in range(150):
        assert game.mark_callback_processed(f"cb-{index}") is True

    assert len(game.processed_callbacks) <= 100


@pytest.mark.asyncio
async def test_action_lock_serialises_players() -> None:
    manager = LockManager(logger=logging.getLogger("lock-test"), default_timeout_seconds=1)

    token = await manager.acquire_action_lock(123, 456)
    assert token is not None

    blocked = await manager.acquire_action_lock(123, 456)
    assert blocked is None

    released = await manager.release_action_lock(123, 456, token)
    assert released is True

    new_token = await manager.acquire_action_lock(123, 456)
    assert new_token is not None
    await manager.release_action_lock(123, 456, new_token)


@pytest.mark.asyncio
async def test_action_lock_expires_without_release() -> None:
    manager = LockManager(logger=logging.getLogger("lock-expiry"), default_timeout_seconds=1)

    with patch("pokerapp.lock_manager.time.monotonic", return_value=0.0):
        token = await manager.acquire_action_lock(1, 2, timeout_seconds=1)
    assert token is not None

    with patch("pokerapp.lock_manager.time.monotonic", return_value=0.5):
        assert await manager.acquire_action_lock(1, 2, timeout_seconds=1) is None

    with patch("pokerapp.lock_manager.time.monotonic", return_value=2.1):
        new_token = await manager.acquire_action_lock(1, 2, timeout_seconds=1)
    assert new_token is not None


def test_callback_data_round_trip() -> None:
    data = MessagingService.build_action_callback_data(
        "call", GameState.ROUND_FLOP, 42
    )
    parsed = MessagingService.parse_action_callback_data(data)

    assert parsed["action_type"] == "call"
    assert parsed["stage"] == "ROUND_FLOP"
    assert parsed["version"] == 42


def test_callback_data_invalid_format() -> None:
    with pytest.raises(ValueError):
        MessagingService.parse_action_callback_data("not-valid")


@pytest.mark.asyncio
async def test_protect_against_races_happy_path() -> None:
    initial_game = Game()
    initial_game.state = GameState.ROUND_FLOP
    initial_game.callback_version = 5
    callback_id = "cb-success"

    updated_game = Game()
    updated_game.state = GameState.ROUND_FLOP
    updated_game.callback_version = 5
    updated_game.processed_callbacks.add(callback_id)

    table_manager = StubTableManager([(initial_game, 1), (updated_game, 2)])
    lock_manager = StubLockManager(["token-1"])

    messaging_service = SimpleNamespace(
        parse_action_callback_data=MessagingService.parse_action_callback_data
    )

    callback_data = MessagingService.build_action_callback_data(
        "call", GameState.ROUND_FLOP, 5
    )
    callback = DummyCallback(123, 456, callback_data, callback_id=callback_id)

    observed: dict[str, object] = {}

    @protect_against_races
    async def handler(_, *, game, version, **kwargs):
        observed["game"] = game
        observed["version"] = version
        return "ok"

    result = await handler(
        callback,
        table_manager=table_manager,
        lock_manager=lock_manager,
        messaging_service=messaging_service,
    )

    assert result == "ok"
    assert observed["game"] is updated_game
    assert observed["version"] == 2
    assert len(table_manager.save_calls) == 1
    assert lock_manager.acquire_calls == [
        (123, 456, callback_data, 5),
    ]
    assert lock_manager.release_calls == [
        (123, 456, "token-1", callback_data),
    ]


@pytest.mark.asyncio
async def test_protect_against_races_rejects_duplicates() -> None:
    game = Game()
    game.state = GameState.ROUND_FLOP
    game.mark_callback_processed("dupe")

    table_manager = StubTableManager([(game, 1)])
    lock_manager = StubLockManager(["token"])

    messaging_service = SimpleNamespace(
        parse_action_callback_data=MessagingService.parse_action_callback_data
    )

    callback_data = MessagingService.build_action_callback_data(
        "call", GameState.ROUND_FLOP, game.callback_version
    )
    callback = DummyCallback(1, 99, callback_data, callback_id="dupe")

    called = False

    @protect_against_races
    async def handler(*args, **kwargs):
        nonlocal called
        called = True

    await handler(
        callback,
        table_manager=table_manager,
        lock_manager=lock_manager,
        messaging_service=messaging_service,
    )

    assert called is False
    callback.answer.assert_awaited_once()
    assert lock_manager.acquire_calls == []


@pytest.mark.asyncio
async def test_action_lock_distributed_across_instances() -> None:
    server = fakeredis.FakeServer()
    redis_pool_1 = fakeredis.aioredis.FakeRedis(server=server)
    redis_pool_2 = fakeredis.aioredis.FakeRedis(server=server)

    redis_keys = {"action_lock_prefix": "action:lock:"}
    logger = logging.getLogger("lock-distributed")

    lock_mgr_instance1 = LockManager(
        logger=logger,
        redis_pool=redis_pool_1,
        redis_keys=redis_keys,
    )
    lock_mgr_instance2 = LockManager(
        logger=logger,
        redis_pool=redis_pool_2,
        redis_keys=redis_keys,
    )

    chat_id, user_id = 12345, 67890

    token1 = await lock_mgr_instance1.acquire_action_lock(chat_id, user_id)
    assert token1 is not None

    token2 = await lock_mgr_instance2.acquire_action_lock(chat_id, user_id)
    assert token2 is None

    released = await lock_mgr_instance1.release_action_lock(chat_id, user_id, token1)
    assert released is True

    token3 = await lock_mgr_instance2.acquire_action_lock(chat_id, user_id)
    assert token3 is not None


@pytest.mark.asyncio
async def test_action_lock_prevents_token_stealing() -> None:
    server = fakeredis.FakeServer()
    redis_pool_a = fakeredis.aioredis.FakeRedis(server=server)
    redis_pool_b = fakeredis.aioredis.FakeRedis(server=server)

    redis_keys = {"action_lock_prefix": "action:lock:"}
    logger = logging.getLogger("lock-token")

    lock_mgr_a = LockManager(logger=logger, redis_pool=redis_pool_a, redis_keys=redis_keys)
    lock_mgr_b = LockManager(logger=logger, redis_pool=redis_pool_b, redis_keys=redis_keys)

    chat_id, user_id = 11111, 22222

    token_a = await lock_mgr_a.acquire_action_lock(chat_id, user_id)
    assert token_a is not None

    fake_token = str(uuid.uuid4())
    stolen = await lock_mgr_b.release_action_lock(chat_id, user_id, fake_token)
    assert stolen is False

    token_b = await lock_mgr_b.acquire_action_lock(chat_id, user_id)
    assert token_b is None


@pytest.mark.asyncio
async def test_action_lock_auto_expires() -> None:
    server = fakeredis.FakeServer()
    redis_pool = fakeredis.aioredis.FakeRedis(server=server)
    redis_keys = {"action_lock_prefix": "action:lock:"}
    logger = logging.getLogger("lock-expire-distributed")

    lock_mgr = LockManager(logger=logger, redis_pool=redis_pool, redis_keys=redis_keys)
    chat_id, user_id = 99999, 88888

    token = await lock_mgr.acquire_action_lock(chat_id, user_id, timeout_seconds=1)
    assert token is not None

    token2 = await lock_mgr.acquire_action_lock(chat_id, user_id)
    assert token2 is None

    await asyncio.sleep(1.5)

    token3 = await lock_mgr.acquire_action_lock(chat_id, user_id)
    assert token3 is not None


@pytest.mark.asyncio
async def test_action_lock_handles_redis_failure(caplog) -> None:
    mock_redis = AsyncMock()
    mock_redis.set.side_effect = aioredis.ConnectionError("Redis unavailable")

    redis_keys = {"action_lock_prefix": "action:lock:"}
    caplog.set_level(logging.ERROR)

    lock_mgr = LockManager(
        logger=logging.getLogger("lock-redis-failure"),
        redis_pool=mock_redis,
        redis_keys=redis_keys,
    )

    chat_id, user_id = 55555, 66666

    token = await lock_mgr.acquire_action_lock(chat_id, user_id)
    assert token is None

    error_messages = [record.message for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("Failed to acquire distributed lock" in message for message in error_messages)


@pytest.mark.asyncio
async def test_action_lock_concurrent_callback_storm() -> None:
    server = fakeredis.FakeServer()
    redis_pool = fakeredis.aioredis.FakeRedis(server=server)

    manager = LockManager(
        logger=logging.getLogger("lock-storm"),
        redis_keys={"engine": {"action_lock_prefix": "test:storm:"}},
        redis_pool=redis_pool,
    )

    chat_id = 999
    user_id = 777
    action_data = "raise_100"
    ttl = 5

    tasks = [
        manager.acquire_action_lock(
            chat_id=chat_id,
            user_id=user_id,
            action_data=action_data,
            timeout_seconds=ttl,
        )
        for _ in range(12)
    ]

    results = await asyncio.gather(*tasks)

    successful_locks = [result for result in results if result is not None]
    assert len(successful_locks) == 1, f"Expected 1 lock, got {len(successful_locks)}"

    lock_token = successful_locks[0]
    stored_key = f"test:storm:{chat_id}:{user_id}:{action_data}"
    stored_value = await redis_pool.get(stored_key)
    assert stored_value == lock_token.encode(), "Winning token should be stored in Redis"

    await manager.release_action_lock(
        chat_id=chat_id,
        user_id=user_id,
        lock_token=lock_token,
        action_data=action_data,
    )


@pytest.mark.asyncio
async def test_protect_against_races_detects_stale_version() -> None:
    game_initial = Game()
    game_initial.state = GameState.ROUND_FLOP
    game_initial.callback_version = 1

    game_updated = Game()
    game_updated.state = GameState.ROUND_FLOP
    game_updated.callback_version = 2

    table_manager = StubTableManager([(game_initial, 10), (game_updated, 11)])
    lock_manager = StubLockManager(["tok"])

    messaging_service = SimpleNamespace(
        parse_action_callback_data=MessagingService.parse_action_callback_data
    )

    data = MessagingService.build_action_callback_data("call", GameState.ROUND_FLOP, 1)
    callback = DummyCallback(5, 7, data)

    @protect_against_races
    async def handler(*args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("Handler should not be invoked")

    await handler(
        callback,
        table_manager=table_manager,
        lock_manager=lock_manager,
        messaging_service=messaging_service,
    )

    callback.answer.assert_awaited()
    assert lock_manager.release_calls == [
        (5, 7, "tok", data),
    ]


@pytest.mark.asyncio
async def test_protect_against_races_detects_stage_mismatch() -> None:
    game_initial = Game()
    game_initial.state = GameState.ROUND_FLOP
    game_initial.callback_version = 3

    game_updated = Game()
    game_updated.state = GameState.ROUND_TURN
    game_updated.callback_version = 3

    table_manager = StubTableManager([(game_initial, 1), (game_updated, 2)])
    lock_manager = StubLockManager(["tok-stage"])

    messaging_service = SimpleNamespace(
        parse_action_callback_data=MessagingService.parse_action_callback_data
    )

    data = MessagingService.build_action_callback_data("call", GameState.ROUND_FLOP, 3)
    callback = DummyCallback(10, 11, data)

    @protect_against_races
    async def handler(*args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("Handler should not be invoked")

    await handler(
        callback,
        table_manager=table_manager,
        lock_manager=lock_manager,
        messaging_service=messaging_service,
    )

    callback.answer.assert_awaited()
    assert lock_manager.release_calls == [
        (10, 11, "tok-stage", data),
    ]


@pytest.mark.asyncio
async def test_protect_against_races_callback_storm() -> None:
    server = fakeredis.FakeServer()
    redis_pool = fakeredis.aioredis.FakeRedis(server=server)

    manager = LockManager(
        logger=logging.getLogger("lock-deco-storm"),
        redis_keys={"engine": {"action_lock_prefix": "test:deco:"}},
        redis_pool=redis_pool,
    )

    class StormTableManager:
        def __init__(self) -> None:
            self._game = Game()
            self._game.state = GameState.ROUND_FLOP
            self._game.callback_version = 1

        async def load_game_with_version(self, chat_id: int):
            return self._game, 1

        async def save_game_with_version_check(self, chat_id: int, game: Game, version: int) -> bool:
            return True

    table_manager = StormTableManager()
    messaging_service = SimpleNamespace(
        parse_action_callback_data=MessagingService.parse_action_callback_data
    )

    process_count = 0
    duplicate_count = 0

    @protect_against_races
    async def handle_callback(callback_query, **kwargs):
        nonlocal process_count
        process_count += 1
        await asyncio.sleep(0.1)

    callback_data = MessagingService.build_action_callback_data(
        "bet",
        GameState.ROUND_FLOP,
        table_manager._game.callback_version,
    )
    callback = DummyCallback(
        chat_id=888,
        user_id=555,
        data=callback_data,
        callback_id="storm_123",
    )

    original_answer = callback.answer

    async def counting_answer(text: str = "", **kwargs: object) -> bool:
        nonlocal duplicate_count
        if isinstance(text, str) and "already processed" in text.lower():
            duplicate_count += 1
        return await original_answer(text, **kwargs)

    callback.answer = counting_answer

    tasks = [
        handle_callback(
            callback,
            table_manager=table_manager,
            lock_manager=manager,
            messaging_service=messaging_service,
        )
        for _ in range(15)
    ]

    await asyncio.gather(*tasks, return_exceptions=True)

    assert process_count == 1, f"Expected 1 process, got {process_count}"
    assert duplicate_count == 14, f"Expected 14 duplicates, got {duplicate_count}"
