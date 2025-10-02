import logging
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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

    async def acquire_action_lock(self, chat_id: int, user_id: int):
        self.acquire_calls.append((chat_id, user_id))
        return next(self._tokens, None)

    async def release_action_lock(self, chat_id: int, user_id: int, token: str) -> bool:
        self.release_calls.append((chat_id, user_id, token))
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
    assert lock_manager.acquire_calls == [(123, 456)]
    assert lock_manager.release_calls == [(123, 456, "token-1")]


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
    assert table_manager.save_calls == []


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
    assert lock_manager.release_calls == [(5, 7, "tok")]


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
    assert lock_manager.release_calls == [(10, 11, "tok-stage")]
