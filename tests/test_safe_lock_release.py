"""Tests for safe early lock release optimization."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from typing import Any, List, Optional

import pytest
from unittest.mock import AsyncMock, MagicMock

from pokerapp.snapshots import FinalizationSnapshot, StageProgressSnapshot
from pokerapp.game_engine import GameEngine


@dataclass
class _SimpleWinner:
    user_id: int
    username: str
    hand_rank: str


class _SnapshotLockManager:
    def __init__(self) -> None:
        self.hold_times: List[float] = []
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def stage_lock(self, chat_id: int):
        loop = asyncio.get_event_loop()
        async with self._lock:
            start = loop.time()
            try:
                yield
            finally:
                end = loop.time()
                self.hold_times.append(end - start)


class _SnapshotTableManager:
    def __init__(self, game: Any) -> None:
        self._game = game

    async def load_game(self, chat_id: int):
        return self._game

    async def save_game(self, chat_id: int, game: Any) -> None:
        # Mark game complete after first save to block subsequent progressions
        if getattr(game, "stage", "") == "flop":
            game.stage = "complete"

    async def delete_game(self, chat_id: int) -> None:
        self._game = None


class _SnapshotMessaging:
    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.sent_messages: List[str] = []
        self.deleted: List[int] = []

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await asyncio.sleep(0)
        self.deleted.append(message_id)

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("send failed")
        self.sent_messages.append(text)


@pytest.fixture
def engine_factory():
    async def _factory(
        *,
        lock_manager: Any,
        table_manager: Any,
        messaging: Any,
        matchmaking: Optional[Any] = None,
        winner_selector: Optional[Any] = None,
    ) -> GameEngine:
        view = messaging
        winner_determination = MagicMock()
        if winner_selector is not None:
            winner_determination.determine_winner = winner_selector
        engine = GameEngine(
            table_manager=table_manager,
            view=view,
            winner_determination=winner_determination,
            request_metrics=MagicMock(),
            round_rate=MagicMock(),
            player_manager=MagicMock(),
            matchmaking_service=matchmaking or MagicMock(),
            stats_reporter=MagicMock(),
            clear_game_messages=AsyncMock(),
            build_identity_from_player=MagicMock(),
            safe_int=int,
            old_players_key="old",
            telegram_safe_ops=MagicMock(),
            lock_manager=lock_manager,
            logger=logging.getLogger("test-engine"),
        )
        engine._winner_selector = winner_selector
        return engine

    return _factory


async def _deal_cards(game: Any) -> None:
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stage_lock_released_before_messaging(engine_factory):
    lock_manager = _SnapshotLockManager()

    class _Game:
        def __init__(self) -> None:
            self.stage = "preflop"
            self.community_cards = ("A♠", "K♠", "Q♠")
            self.pot = 100
            self.message_ids = (1, 2)

    table_manager = _SnapshotTableManager(_Game())
    messaging = _SnapshotMessaging(delay=0.5)

    matchmaking = MagicMock()
    matchmaking.deal_community_cards = _deal_cards

    engine = await engine_factory(
        lock_manager=lock_manager,
        table_manager=table_manager,
        messaging=messaging,
        matchmaking=matchmaking,
    )

    start = asyncio.get_event_loop().time()
    await engine.progress_stage(chat_id=123)
    elapsed = asyncio.get_event_loop().time() - start

    assert lock_manager.hold_times, "lock should have been acquired"
    assert lock_manager.hold_times[0] < 0.5
    assert elapsed >= 0.5
    assert messaging.sent_messages


@pytest.mark.asyncio
async def test_snapshot_immutability():
    snapshot = StageProgressSnapshot(
        chat_id=1,
        pot=100,
        stage="flop",
        community_cards=("A", "K", "Q"),
        message_ids_to_delete=(1,),
        new_message_text="text",
    )

    with pytest.raises(AttributeError):
        snapshot.pot = 200

    final_snapshot = FinalizationSnapshot(
        chat_id=1,
        winner_user_id=2,
        winner_username="winner",
        pot=300,
        winning_hand="Flush",
        message_ids_to_delete=(1,),
        stats_payload={"foo": "bar"},
    )

    with pytest.raises(AttributeError):
        final_snapshot.winner_username = "other"


@pytest.mark.asyncio
async def test_concurrent_stage_progression_uses_snapshots(engine_factory):
    lock_manager = _SnapshotLockManager()

    class _Game:
        def __init__(self) -> None:
            self.stage = "preflop"
            self.community_cards = ()
            self.pot = 50
            self.message_ids = (10,)

    table_manager = _SnapshotTableManager(_Game())
    messaging = _SnapshotMessaging()

    matchmaking = MagicMock()
    matchmaking.deal_community_cards = _deal_cards

    engine = await engine_factory(
        lock_manager=lock_manager,
        table_manager=table_manager,
        messaging=messaging,
        matchmaking=matchmaking,
    )

    tasks = [engine.progress_stage(chat_id=1) for _ in range(5)]
    results = await asyncio.gather(*tasks)

    assert results.count(True) == 1
    assert results.count(False) == 4


@pytest.mark.asyncio
async def test_deferred_task_exceptions_logged(engine_factory, caplog):
    lock_manager = _SnapshotLockManager()

    class _Game:
        def __init__(self) -> None:
            self.stage = "preflop"
            self.community_cards = ()
            self.pot = 75
            self.message_ids = (5,)

    table_manager = _SnapshotTableManager(_Game())
    messaging = _SnapshotMessaging(delay=0.0, fail=True)

    matchmaking = MagicMock()
    matchmaking.deal_community_cards = _deal_cards

    engine = await engine_factory(
        lock_manager=lock_manager,
        table_manager=table_manager,
        messaging=messaging,
        matchmaking=matchmaking,
    )

    caplog.set_level(logging.WARNING)
    result = await engine.progress_stage(chat_id=7)
    assert result is True
    assert any("Deferred stage task failed" in record.getMessage() for record in caplog.records)
