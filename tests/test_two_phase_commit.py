from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple

import pytest

from pokerapp.betting_handler import BettingHandler, BettingResult
from pokerapp.wallet_service import WalletService


class FakeRedisClient:
    def __init__(self) -> None:
        self.reservations: Dict[str, Dict[str, Any]] = {}
        self.state_store: Dict[str, Dict[str, Any]] = {}
        self.lists: Dict[str, List[Any]] = {}

    async def eval(
        self, script: str, keys: List[str], args: List[Any]
    ) -> Any:  # pragma: no cover - exercised via wallet service
        key = keys[0]
        if script.startswith("-- reservation_create"):
            if key in self.reservations:
                return 0
            self.reservations[key] = {
                "user_id": args[0],
                "chat_id": args[1],
                "amount": args[2],
                "status": args[3],
                "metadata": args[4],
                "created_at": args[5],
            }
            return 1
        if script.startswith("-- reservation_commit"):
            record = self.reservations.get(key)
            if not record:
                return "missing"
            status = record.get("status")
            if status == "committed":
                return "committed"
            if status != "pending":
                return status
            record["status"] = "committed"
            return "ok"
        if script.startswith("-- reservation_rollback"):
            record = self.reservations.get(key)
            if not record:
                return "missing"
            status = record.get("status")
            allow_committed = args[0] == "1"
            reason = args[1]
            if status == "rolled_back":
                return "rolled_back"
            if status == "committed":
                if allow_committed:
                    record["status"] = "rolled_back"
                    record["rollback_reason"] = reason
                    return "compensated"
                return "committed"
            if status != "pending":
                return status
            record["status"] = "rolled_back"
            record["rollback_reason"] = reason
            return "rolled_back"
        if script.startswith("-- game_state_save"):
            state_json, expected_version, _ttl = args
            current = self.state_store.setdefault(key, {"version": 0, "state": "{}"})
            current_version = int(current.get("version", 0))
            if current_version != int(expected_version):
                return 0
            current["version"] = current_version + 1
            current["state"] = state_json
            return 1
        raise AssertionError(f"Unexpected script execution: {script}")

    async def hgetall(self, key: str) -> Mapping[str, Any]:
        if key in self.reservations:
            return dict(self.reservations[key])
        if key in self.state_store:
            store = self.state_store[key]
            return {"state": store.get("state", "{}"), "version": store.get("version", 0)}
        return {}

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def delete(self, key: str) -> int:
        removed = 0
        if key in self.reservations:
            del self.reservations[key]
            removed += 1
        if key in self.state_store:
            del self.state_store[key]
            removed += 1
        return removed

    async def lpush(self, key: str, value: Any) -> int:
        self.lists.setdefault(key, [])
        self.lists[key].insert(0, value)
        return len(self.lists[key])

    async def exists(self, key: str) -> bool:
        return key in self.reservations or key in self.state_store

    async def hset(self, key: str, mapping: MutableMapping[str, Any]) -> int:
        self.state_store[key] = dict(mapping)
        return 1


class FakeWalletRepository:
    def __init__(self, *, balances: Optional[Dict[Tuple[int, int], int]] = None) -> None:
        self._balances = balances or {}
        self.fail_credit = False

    async def get_balance(self, user_id: int, chat_id: int) -> int:
        return self._balances.get((user_id, chat_id), 0)

    async def debit(
        self, user_id: int, chat_id: int, amount: int, *, metadata: Mapping[str, Any]
    ) -> None:
        key = (user_id, chat_id)
        balance = self._balances.get(key, 0)
        if balance < amount:
            raise ValueError("Insufficient funds")
        self._balances[key] = balance - amount

    async def credit(
        self, user_id: int, chat_id: int, amount: int, *, metadata: Mapping[str, Any]
    ) -> None:
        if self.fail_credit:
            raise RuntimeError("credit failure")
        key = (user_id, chat_id)
        balance = self._balances.get(key, 0)
        self._balances[key] = balance + amount


class FakeDLQ:
    def __init__(self) -> None:
        self.items: List[Mapping[str, Any]] = []

    async def push(self, payload: Mapping[str, Any]) -> None:
        self.items.append(dict(payload))


class FakeGameEngine:
    def __init__(self) -> None:
        self._states: Dict[int, Dict[str, Any]] = {}
        self.fail_next_save = False

    def seed_state(self, chat_id: int, state: Mapping[str, Any]) -> None:
        self._states[chat_id] = json.loads(json.dumps(state))

    async def load_game_state(self, chat_id: int) -> Optional[Dict[str, Any]]:
        state = self._states.get(chat_id)
        if state is None:
            return None
        return json.loads(json.dumps(state))

    async def save_game_state_with_version(
        self, chat_id: int, state: Mapping[str, Any], *, expected_version: int
    ) -> bool:
        if self.fail_next_save:
            self.fail_next_save = False
            return False
        current = self._states.get(chat_id)
        current_version = int(current.get("version", 0)) if current else 0
        if current_version != expected_version:
            return False
        next_state = json.loads(json.dumps(state))
        next_state["version"] = expected_version + 1
        self._states[chat_id] = next_state
        return True

    async def apply_betting_action(
        self,
        state: Mapping[str, Any],
        user_id: int,
        action: str,
        amount: int,
    ) -> Mapping[str, Any]:
        updated = json.loads(json.dumps(state))
        players = updated.get("players", [])
        for player in players:
            if int(player.get("user_id", 0)) == int(user_id):
                if action == "fold":
                    player["folded"] = True
                else:
                    player["chips"] = int(player.get("chips", 0)) - amount
                    player["current_bet"] = int(player.get("current_bet", 0)) + amount
                break
        updated.setdefault("pot", 0)
        updated["pot"] = int(updated["pot"]) + amount
        return updated


class FakeLockManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire_table_write_lock(self, chat_id: int, timeout: Optional[float] = None):
        async with self._lock:
            yield

    async def get_lock_queue_depth(self, chat_id: int) -> int:
        return 0

    async def estimate_wait_time(self, queue_depth: int) -> float:
        return 0.0


@pytest.fixture
def redis_client() -> FakeRedisClient:
    return FakeRedisClient()


@pytest.fixture
def wallet_repository() -> FakeWalletRepository:
    return FakeWalletRepository(balances={(1, 99): 1_000})


@pytest.fixture
def wallet_service(
    wallet_repository: FakeWalletRepository, redis_client: FakeRedisClient
) -> WalletService:
    return WalletService(wallet_repository, redis_client, dlq=FakeDLQ())


@pytest.fixture
def game_engine() -> FakeGameEngine:
    engine = FakeGameEngine()
    engine.seed_state(
        99,
        {
            "version": 1,
            "current_player_id": 1,
            "current_bet": 100,
            "pot": 0,
            "players": [
                {"user_id": 1, "chips": 1_000, "current_bet": 0, "folded": False}
            ],
        },
    )
    return engine


@pytest.fixture
def lock_manager() -> FakeLockManager:
    return FakeLockManager()


@pytest.fixture
def betting_handler(
    wallet_service: WalletService,
    game_engine: FakeGameEngine,
    lock_manager: FakeLockManager,
) -> BettingHandler:
    return BettingHandler(wallet_service, game_engine, lock_manager)


@pytest.mark.asyncio
async def test_happy_path_reserve_commit(
    betting_handler: BettingHandler,
    wallet_repository: FakeWalletRepository,
    game_engine: FakeGameEngine,
) -> None:
    result = await betting_handler.handle_betting_action(1, 99, "call")

    assert result.success is True
    assert wallet_repository._balances[(1, 99)] == 900
    state = game_engine._states[99]
    assert state["version"] == 2
    assert state["pot"] == 100


@pytest.mark.asyncio
async def test_insufficient_funds(
    betting_handler: BettingHandler,
    wallet_repository: FakeWalletRepository,
) -> None:
    wallet_repository._balances[(1, 99)] = 50
    result = await betting_handler.handle_betting_action(1, 99, "call")

    assert result.success is False
    assert "insufficient" in result.message.lower()
    assert wallet_repository._balances[(1, 99)] == 50


@pytest.mark.asyncio
async def test_version_conflict_triggers_refund(
    betting_handler: BettingHandler,
    wallet_repository: FakeWalletRepository,
    game_engine: FakeGameEngine,
) -> None:
    game_engine.fail_next_save = True
    starting_balance = wallet_repository._balances[(1, 99)]

    result = await betting_handler.handle_betting_action(1, 99, "call")

    assert result.success is False
    assert "conflict" in result.message.lower()
    assert wallet_repository._balances[(1, 99)] == starting_balance


@pytest.mark.asyncio
async def test_auto_rollback_on_timeout(
    wallet_service: WalletService,
    wallet_repository: FakeWalletRepository,
) -> None:
    wallet_service._reservation_ttl = 0.1
    wallet_service._reservation_grace_period = 0

    success, reservation_id, _ = await wallet_service.reserve_chips(
        1, 99, 100, metadata={}
    )
    assert success is True
    assert reservation_id is not None
    await asyncio.sleep(0.2)
    assert wallet_repository._balances[(1, 99)] == 1_000


@pytest.mark.asyncio
async def test_dlq_on_refund_failure(
    wallet_repository: FakeWalletRepository,
    redis_client: FakeRedisClient,
) -> None:
    dlq = FakeDLQ()
    service = WalletService(wallet_repository, redis_client, dlq=dlq)
    wallet_repository.fail_credit = True
    success, reservation_id, _ = await service.reserve_chips(
        1, 99, 100, metadata={}
    )
    assert success is True
    await service.rollback_reservation(reservation_id or "", "test", allow_committed=True)
    assert dlq.items
    entry = dlq.items[0]
    assert entry["amount"] == 100
    assert entry["reason"] == "test"


@pytest.mark.asyncio
async def test_idempotent_commit(
    wallet_service: WalletService,
) -> None:
    wallet_service._reservation_grace_period = 0
    success, reservation_id, _ = await wallet_service.reserve_chips(
        1, 99, 50, metadata={}
    )
    assert success is True and reservation_id

    commit_first = await wallet_service.commit_reservation(reservation_id or "")
    commit_second = await wallet_service.commit_reservation(reservation_id or "")

    assert commit_first[0] is True
    assert commit_second[0] is True

