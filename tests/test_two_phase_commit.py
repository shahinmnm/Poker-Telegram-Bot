"""Integration tests for Two-Phase Commit betting system."""

import asyncio
from typing import Any, Iterable
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.betting_handler import BettingHandler
from pokerapp.wallet_service import WalletService


class _DummyLock:
    async def __aenter__(self) -> None:  # pragma: no cover - trivial
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - trivial
        return None


def _make_query_stub(result: Any) -> MagicMock:
    stub = MagicMock()
    stub.filter_by.return_value = stub
    stub.with_for_update.return_value = stub

    if isinstance(result, Exception):
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise result

        stub.first = AsyncMock(side_effect=_raise)
    else:
        stub.first = AsyncMock(return_value=result)

    return stub


def _set_query_sequence(db: MagicMock, stubs: Iterable[MagicMock]) -> None:
    db.query.side_effect = list(stubs)


@pytest.fixture
def mock_db() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.query = MagicMock()
    return session


@pytest.fixture
def mock_redis() -> MagicMock:
    redis = MagicMock()
    redis.setex = AsyncMock()
    redis.get = AsyncMock(return_value="1")
    redis.eval = AsyncMock(return_value=1)
    return redis


@pytest.fixture
def wallet_service(mock_db: MagicMock, mock_redis: MagicMock) -> WalletService:
    service = WalletService(mock_db, mock_redis)
    return service


@pytest.fixture
def betting_handler(wallet_service: WalletService) -> BettingHandler:
    engine = MagicMock()
    engine.load_game_state = AsyncMock()
    engine.load_game_state_with_version = AsyncMock()
    engine.apply_betting_action = AsyncMock()
    engine.save_game_state_with_version = AsyncMock()

    lock_manager = MagicMock()
    lock_manager.acquire_table_write_lock.return_value = _DummyLock()

    return BettingHandler(wallet_service, engine, lock_manager)


@pytest.mark.asyncio
async def test_successful_bet_with_2pc(
    betting_handler: BettingHandler, wallet_service: WalletService, mock_db: MagicMock
) -> None:
    user_id, chat_id = 123, 456
    mock_player = MagicMock()
    mock_player.chips = 1000

    _set_query_sequence(
        mock_db,
        [
            _make_query_stub(mock_player),
            _make_query_stub(mock_player),
        ],
    )

    betting_handler.engine.load_game_state.return_value = {
        "players": [{"user_id": user_id, "chips": 1000, "current_bet": 0}],
        "current_bet": 10,
        "stage": "FLOP",
    }
    betting_handler.engine.load_game_state_with_version.return_value = {
        "players": [{"user_id": user_id, "chips": 1000, "current_bet": 0}],
        "current_player_id": user_id,
        "current_bet": 10,
        "stage": "FLOP",
        "version": 1,
    }
    betting_handler.engine.apply_betting_action.return_value = {"version": 2}
    betting_handler.engine.save_game_state_with_version.return_value = True

    result = await betting_handler.handle_betting_action(user_id, chat_id, "call")

    assert result.success is True
    assert "successful" in result.message.lower()
    assert mock_player.chips == 990


@pytest.mark.asyncio
async def test_insufficient_funds_rollback(
    betting_handler: BettingHandler, wallet_service: WalletService, mock_db: MagicMock
) -> None:
    user_id, chat_id = 123, 456
    mock_player = MagicMock()
    mock_player.chips = 5

    _set_query_sequence(mock_db, [_make_query_stub(mock_player)])

    betting_handler.engine.load_game_state.return_value = {
        "players": [{"user_id": user_id, "chips": 5, "current_bet": 0}],
        "current_bet": 100,
        "stage": "FLOP",
    }

    result = await betting_handler.handle_betting_action(user_id, chat_id, "call")

    assert result.success is False
    assert "insufficient" in result.message.lower()
    assert mock_player.chips == 5


@pytest.mark.asyncio
async def test_version_conflict_refund(
    betting_handler: BettingHandler, wallet_service: WalletService, mock_db: MagicMock
) -> None:
    user_id, chat_id = 123, 456
    mock_player = MagicMock()
    mock_player.chips = 1000

    _set_query_sequence(
        mock_db,
        [
            _make_query_stub(mock_player),
            _make_query_stub(mock_player),
            _make_query_stub(mock_player),
        ],
    )

    betting_handler.engine.load_game_state.return_value = {
        "players": [{"user_id": user_id, "chips": 1000, "current_bet": 0}],
        "current_bet": 10,
        "stage": "FLOP",
    }
    betting_handler.engine.load_game_state_with_version.return_value = {
        "players": [{"user_id": user_id, "chips": 1000, "current_bet": 0}],
        "current_player_id": user_id,
        "current_bet": 10,
        "version": 1,
    }
    betting_handler.engine.apply_betting_action.return_value = {"version": 2}
    betting_handler.engine.save_game_state_with_version.return_value = False

    result = await betting_handler.handle_betting_action(user_id, chat_id, "call")

    assert result.success is False
    assert "conflict" in result.message.lower()
    assert mock_player.chips == 1000


@pytest.mark.asyncio
async def test_reservation_auto_expiry(
    wallet_service: WalletService, mock_db: MagicMock
) -> None:
    user_id, chat_id = 123, 456
    mock_player = MagicMock()
    mock_player.chips = 1000

    wallet_service._reservation_ttl = 0.1

    _set_query_sequence(
        mock_db,
        [
            _make_query_stub(mock_player),
            _make_query_stub(mock_player),
            _make_query_stub(mock_player),
        ],
    )

    success, reservation_id, _ = await wallet_service.reserve_chips(user_id, chat_id, 100)
    assert success is True
    assert reservation_id in wallet_service._reservations

    await asyncio.sleep(0.2)

    assert reservation_id not in wallet_service._reservations
    assert mock_player.chips == 1000


@pytest.mark.asyncio
async def test_dlq_on_refund_failure(
    wallet_service: WalletService, mock_db: MagicMock, mock_redis: MagicMock
) -> None:
    user_id, chat_id = 123, 456
    mock_player = MagicMock()
    mock_player.chips = 1000

    mock_dlq = AsyncMock()
    wallet_service.dlq = mock_dlq

    _set_query_sequence(
        mock_db,
        [
            _make_query_stub(mock_player),
            _make_query_stub(mock_player),
            _make_query_stub(Exception("DB error")),
        ],
    )

    success, reservation_id, _ = await wallet_service.reserve_chips(user_id, chat_id, 100)
    assert success is True

    rollback_success, message = await wallet_service.rollback_reservation(reservation_id)

    assert rollback_success is False
    assert "refund" in message.lower()
    mock_dlq.push.assert_awaited_once()
    dlq_entry = mock_dlq.push.await_args.args[0]
    assert dlq_entry["user_id"] == user_id
    assert dlq_entry["amount"] == 100
