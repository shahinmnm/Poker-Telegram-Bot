import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Mapping, Optional

import pytest

import pokerapp.betting_handler as betting_handler_module
from pokerapp.betting_handler import BettingHandler
from pokerapp.lock_manager import LockManager


class _CounterChild:
    def __init__(self, store: Dict[tuple, float], key: tuple) -> None:
        self._store = store
        self._key = key

    def inc(self, amount: float = 1.0) -> None:
        self._store[self._key] = self._store.get(self._key, 0.0) + float(amount)


class _CounterStub:
    def __init__(self) -> None:
        self.counts: Dict[tuple, float] = {}

    def labels(self, **labels: Any) -> _CounterChild:
        key = tuple(sorted(labels.items()))
        return _CounterChild(self.counts, key)


class _HistogramChild(_CounterChild):
    def observe(self, value: float) -> None:  # type: ignore[override]
        super().inc(float(value))


class _HistogramStub(_CounterStub):
    def labels(self, **labels: Any) -> _HistogramChild:  # type: ignore[override]
        key = tuple(sorted(labels.items()))
        return _HistogramChild(self.counts, key)

    def observe(self, value: float) -> None:
        key = ("_default",)
        self.counts[key] = self.counts.get(key, 0.0) + float(value)


@pytest.fixture(autouse=True)
def _patch_metrics(monkeypatch):
    counter_stub = _CounterStub()
    wait_stub = _HistogramStub()
    queue_stub = _HistogramStub()
    monkeypatch.setattr(betting_handler_module, "LOCK_RETRY_TOTAL", counter_stub)
    monkeypatch.setattr(betting_handler_module, "LOCK_WAIT_DURATION", wait_stub)
    monkeypatch.setattr(betting_handler_module, "LOCK_QUEUE_DEPTH", queue_stub)
    yield counter_stub, wait_stub, queue_stub


@pytest.fixture
def fast_sleep(monkeypatch):
    recorded: List[float] = []
    original_sleep = betting_handler_module.asyncio.sleep

    async def _sleep(delay: float) -> None:
        recorded.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(betting_handler_module.asyncio, "sleep", _sleep)
    return recorded


class StubWalletService:
    def __init__(self, *, reservation_ttl: float = 300.0) -> None:
        self._reservation_ttl = reservation_ttl
        self.reservations: Dict[str, Dict[str, Any]] = {}
        self.rollbacks: List[str] = []
        self.commits: List[str] = []

    async def reserve_chips(
        self,
        *,
        user_id: int,
        chat_id: int,
        amount: int,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> tuple[bool, Optional[str], str]:
        reservation_id = f"resv-{len(self.reservations) + 1}"
        self.reservations[reservation_id] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "amount": amount,
            "status": "pending",
            "metadata": dict(metadata or {}),
        }
        return True, reservation_id, "reserved"

    async def commit_reservation(self, reservation_id: str) -> tuple[bool, str]:
        record = self.reservations.get(reservation_id)
        if not record or record["status"] != "pending":
            return False, "missing"
        record["status"] = "committed"
        self.commits.append(reservation_id)
        return True, "committed"

    async def rollback_reservation(
        self,
        reservation_id: str,
        reason: str,
        *,
        allow_committed: bool = False,
    ) -> tuple[bool, str]:
        record = self.reservations.get(reservation_id)
        if not record:
            return False, "missing"
        if record["status"] == "committed" and not allow_committed:
            return False, "committed"
        record["status"] = "rolled_back"
        self.rollbacks.append(reason)
        return True, "rolled_back"


class StubGameEngine:
    def __init__(self) -> None:
        self._state: Dict[str, Any] = {
            "version": 1,
            "current_player_id": 1,
            "current_bet": 100,
            "pot": 0,
            "players": [
                {"user_id": 1, "chips": 1_000, "current_bet": 0, "folded": False},
                {"user_id": 2, "chips": 1_000, "current_bet": 0, "folded": False},
                {"user_id": 3, "chips": 1_000, "current_bet": 0, "folded": False},
                {"user_id": 4, "chips": 1_000, "current_bet": 0, "folded": False},
                {"user_id": 5, "chips": 1_000, "current_bet": 0, "folded": False},
            ],
        }

    async def load_game_state(self, chat_id: int) -> Mapping[str, Any]:
        return dict(self._state)

    async def save_game_state_with_version(
        self,
        chat_id: int,
        state: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> bool:
        if int(self._state.get("version", 0)) != expected_version:
            return False
        next_state = dict(state)
        next_state["version"] = expected_version + 1
        self._state = next_state
        return True

    async def apply_betting_action(
        self,
        state: Mapping[str, Any],
        user_id: int,
        action: str,
        amount: int,
    ) -> Mapping[str, Any]:
        updated = dict(state)
        players = [dict(p) for p in state.get("players", [])]
        for player in players:
            if int(player.get("user_id", 0)) == int(user_id):
                if action == "fold":
                    player["folded"] = True
                else:
                    player["chips"] = int(player.get("chips", 0)) - amount
                    player["current_bet"] = int(player.get("current_bet", 0)) + amount
                break
        updated["players"] = players
        updated["pot"] = int(state.get("pot", 0)) + amount
        return updated


class ScriptedLockManager:
    def __init__(self, failures: List[bool], queue_depths: List[int]) -> None:
        self._failures = failures
        self._queue_depths = queue_depths or [0]
        self._acquire_calls = 0
        self._queue_index = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire_table_write_lock(self, chat_id: int, timeout: Optional[float] = None):
        if self._acquire_calls < len(self._failures) and self._failures[self._acquire_calls]:
            self._acquire_calls += 1
            raise TimeoutError("busy")
        self._acquire_calls += 1
        async with self._lock:
            yield

    async def get_lock_queue_depth(self, chat_id: int) -> int:
        if self._queue_index < len(self._queue_depths):
            depth = self._queue_depths[self._queue_index]
            self._queue_index += 1
        else:
            depth = self._queue_depths[-1]
        return depth

    async def estimate_wait_time(self, queue_depth: int) -> float:
        if queue_depth <= 0:
            return 0.0
        return 7.5 if queue_depth <= 2 else 17.5


class QueueingLockManager:
    def __init__(self) -> None:
        self._queue: List[asyncio.Task[Any]] = []
        self._running = False

    @asynccontextmanager
    async def acquire_table_write_lock(self, chat_id: int, timeout: Optional[float] = None):
        task = asyncio.current_task()
        if task is None:
            yield
            return

        if task not in self._queue:
            self._queue.append(task)
            raise TimeoutError("busy")

        if self._queue[0] is not task:
            raise TimeoutError("busy")

        while self._running:
            await asyncio.sleep(0)

        self._running = True
        self._queue.pop(0)
        try:
            yield
        finally:
            self._running = False

    async def get_lock_queue_depth(self, chat_id: int) -> int:
        depth = len(self._queue)
        return max(0, depth)

    async def estimate_wait_time(self, queue_depth: int) -> float:
        if queue_depth <= 0:
            return 0.0
        if queue_depth <= 2:
            return 7.5
        if queue_depth <= 4:
            return 17.5
        return 27.5


@pytest.mark.asyncio
async def test_retry_successful_after_backoff(fast_sleep, _patch_metrics):
    wallet = StubWalletService()
    engine = StubGameEngine()
    lock = ScriptedLockManager([True, False], [2, 0])
    handler = BettingHandler(
        wallet,
        engine,
        lock,
        enable_smart_retry=True,
        retry_settings={
            "max_attempts": 7,
            "backoff_delays_seconds": [0.01, 0.01, 0.02, 0.02, 0.04, 0.04, 0.04, 0.04],
        },
    )

    result = await handler.handle_betting_action(1, 99, "call")

    assert result.success is True
    assert wallet.commits == ["resv-1"]
    counter_stub, wait_stub, queue_stub = _patch_metrics
    assert counter_stub.counts[(("outcome", "success"),)] == 1
    assert queue_stub.counts.get(("_default",), 0.0) >= 2
    assert fast_sleep, "Expected exponential backoff to invoke sleep"


@pytest.mark.asyncio
async def test_fail_fast_when_queue_too_deep(_patch_metrics):
    wallet = StubWalletService()
    engine = StubGameEngine()
    lock = ScriptedLockManager([True], [6])
    handler = BettingHandler(wallet, engine, lock, enable_smart_retry=True)

    result = await handler.handle_betting_action(1, 99, "call")

    assert result.success is False
    assert "queue" in result.message.lower()
    assert wallet.rollbacks[-1] == "queue_congested"
    counter_stub, _, _ = _patch_metrics
    assert counter_stub.counts[(("outcome", "abandoned"),)] == 1


@pytest.mark.asyncio
async def test_reservation_expiry_during_retry(_patch_metrics):
    wallet = StubWalletService(reservation_ttl=2.0)
    engine = StubGameEngine()
    lock = ScriptedLockManager([True], [2])
    handler = BettingHandler(wallet, engine, lock, enable_smart_retry=True)

    result = await handler.handle_betting_action(1, 99, "call")

    assert result.success is False
    assert "reservation" in result.message.lower()
    assert wallet.rollbacks[-1] in {"reservation_expiring", "reservation_expired"}
    counter_stub, _, _ = _patch_metrics
    assert counter_stub.counts[(("outcome", "abandoned"),)] == 1


@pytest.mark.asyncio
async def test_max_retries_exceeded_triggers_timeout(_patch_metrics):
    wallet = StubWalletService()
    engine = StubGameEngine()
    lock = ScriptedLockManager([True, True, True, True], [1])
    handler = BettingHandler(wallet, engine, lock, enable_smart_retry=True)

    result = await handler.handle_betting_action(1, 99, "call")

    assert result.success is False
    assert "please try again" in result.message.lower()
    counter_stub, _, _ = _patch_metrics
    assert counter_stub.counts[(("outcome", "timeout"),)] >= 1
    assert counter_stub.counts[(("outcome", "max_retries"),)] >= 1


@pytest.mark.asyncio
async def test_queue_depth_estimation(redis_pool):
    manager = LockManager(logger=logging.getLogger("lock-test"), redis_pool=redis_pool)
    chat_id = 42
    redis_key = "lock:queue:42"
    await redis_pool.zadd(redis_key, {"op1": 1, "op2": 2, "op3": 3})

    depth = await manager.get_lock_queue_depth(chat_id)
    wait_estimate = await manager.estimate_wait_time(depth)

    assert depth == 3
    assert 15 <= wait_estimate <= 20


@pytest.mark.asyncio
async def test_concurrent_retry_behaviour(fast_sleep, _patch_metrics):
    wallet = StubWalletService()
    engine = StubGameEngine()
    lock = QueueingLockManager()
    handler = BettingHandler(
        wallet,
        engine,
        lock,
        enable_smart_retry=True,
        retry_settings={
            "max_attempts": 7,
            "backoff_delays_seconds": [0.01, 0.01, 0.02, 0.02, 0.04, 0.04, 0.04, 0.04],
        },
    )

    async def _play():
        return await handler.handle_betting_action(1, 99, "call")

    tasks = [asyncio.create_task(_play()) for _ in range(5)]
    results = await asyncio.gather(*tasks)

    successes = [result for result in results if result.success]
    failures = [result for result in results if not result.success]

    assert successes, "expected at least one successful retry"
    assert all("busy" in result.message.lower() or "wait" in result.message.lower() for result in failures)
    assert len(wallet.commits) == len(successes)
    assert len(wallet.rollbacks) >= len(failures)
    assert not lock._running
    assert len(lock._queue) == len(failures)
    counter_stub, _, _ = _patch_metrics
    success_count = counter_stub.counts.get((("outcome", "success"),), 0)
    assert success_count >= len(successes)
