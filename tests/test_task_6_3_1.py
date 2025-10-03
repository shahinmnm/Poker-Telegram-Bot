"""Task 6.3.1: Table-Level Locking Tests."""

import asyncio
from types import SimpleNamespace
from typing import Dict, List
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from pokerapp.game_engine import GameEngine
from pokerapp.entities import Game, Player, Wallet
from pokerapp.lock_manager import LockManager


class _TestWallet(Wallet):
    """Minimal wallet implementation for test players."""

    def __init__(self) -> None:
        self._balance = 0

    @staticmethod
    def _prefix(id: int, suffix: str = ""):
        return f"wallet:{id}{suffix}"

    async def add_daily(self, amount):
        self._balance += amount
        return self._balance

    async def has_daily_bonus(self) -> bool:
        return False

    async def inc(self, amount=0):
        self._balance += amount
        return self._balance

    async def inc_authorized_money(self, game_id: str, amount):
        return None

    async def authorized_money(self, game_id: str):
        return 0

    async def authorize(self, game_id: str, amount):
        return None

    async def authorize_all(self, game_id: str):
        return 0

    async def value(self):
        return self._balance

    async def approve(self, game_id: str):
        return None

    async def cancel(self, game_id: str):
        return None


@pytest_asyncio.fixture
async def lock_manager():
    import fakeredis
    import fakeredis.aioredis

    server = fakeredis.FakeServer()
    redis = fakeredis.aioredis.FakeRedis(server=server)

    manager = LockManager(
        logger=Mock(),
        redis_pool=redis,
        redis_keys={
            "engine": {
                "stage_lock_prefix": "stage:",
                "action_lock_prefix": "action:lock:",
                "table_lock_prefix": "table:lock:",
            }
        },
    )

    yield manager

    await redis.close()


@pytest.mark.asyncio
async def test_table_lock_acquire_release(lock_manager: LockManager):
    chat_id = -42

    token = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="join",
        timeout_seconds=5,
    )

    assert token is not None
    assert len(token) == 36

    blocked = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="join",
        timeout_seconds=5,
    )
    assert blocked is None

    released = await lock_manager.release_table_lock(chat_id, token)
    assert released is True

    token2 = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="join",
        timeout_seconds=5,
    )
    assert token2 is not None


@pytest.mark.asyncio
async def test_table_lock_join_leave_serialised(lock_manager: LockManager):
    chat_id = 101

    token_join = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="join",
        timeout_seconds=5,
    )
    assert token_join is not None

    token_leave = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="leave",
        timeout_seconds=5,
    )
    assert token_leave is None

    await lock_manager.release_table_lock(chat_id, token_join)


@pytest.mark.asyncio
async def test_concurrent_joins_unique_seats(lock_manager: LockManager):
    chat_id = 500
    max_players = 8

    assigned_seats: List[int] = []

    def _build_existing_player(seat: int) -> Player:
        player = Player(
            user_id=f"existing-{seat}",
            mention_markdown=f"Player {seat}",
            wallet=_TestWallet(),
            ready_message_id=None,
            seat_index=seat,
        )
        player.display_name = f"Player {seat}"
        player.full_name = f"Player {seat}"
        return player

    async def load_game(_: int):
        game = Game()
        for seat in assigned_seats:
            game.add_player(_build_existing_player(seat), seat)
        return game, None

    async def save_game(_: int, game: Game):
        for seat_index, player in enumerate(game.seats):
            if player is not None and seat_index not in assigned_seats:
                assigned_seats.append(seat_index)

    table_manager = SimpleNamespace(
        load_game=load_game,
        save_game=save_game,
        create_game=AsyncMock(side_effect=lambda cid: Game()),
    )

    class StubView:
        def __init__(self) -> None:
            self.messages: List[Dict[str, str]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append({"chat_id": chat_id, "text": text})

    view = StubView()

    engine = GameEngine(
        table_manager=table_manager,
        view=view,
        winner_determination=Mock(),
        request_metrics=Mock(),
        round_rate=Mock(),
        player_manager=Mock(),
        matchmaking_service=Mock(),
        stats_reporter=Mock(),
        clear_game_messages=AsyncMock(),
        build_identity_from_player=lambda player: player,
        safe_int=int,
        old_players_key="old_players",
        telegram_safe_ops=Mock(),
        lock_manager=lock_manager,
        logger=Mock(),
    )

    async def join(uid: int):
        while True:
            success = await engine.join_game(
                chat_id=chat_id,
                user_id=uid,
                user_name=f"Player {uid}",
            )
            if success:
                return True
            if len(assigned_seats) >= max_players:
                return False
            await asyncio.sleep(0)

    tasks = [join(idx) for idx in range(10)]
    results = await asyncio.gather(*tasks)

    assert results.count(True) == max_players
    assert len(assigned_seats) == max_players
    assert len(set(assigned_seats)) == max_players
    assert all(0 <= seat < max_players for seat in assigned_seats)


@pytest.mark.asyncio
async def test_table_lock_timeout_expires(lock_manager: LockManager):
    chat_id = 204

    token = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="join",
        timeout_seconds=1,
    )
    assert token is not None

    blocked = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="join",
        timeout_seconds=1,
    )
    assert blocked is None

    await asyncio.sleep(1.1)

    token2 = await lock_manager.acquire_table_lock(
        chat_id=chat_id,
        operation="join",
        timeout_seconds=1,
    )
    assert token2 is not None
