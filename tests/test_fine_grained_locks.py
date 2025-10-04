import asyncio
import logging

import pytest

from pokerapp.lock_manager import LockManager, LockOrderError


class TestFineGrainedLocks:
    """Test suite for Stage 4 fine-grained locking."""

    @pytest.mark.asyncio
    async def test_player_lock_allows_concurrent_different_players(self):
        """Different players can acquire locks simultaneously."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_players"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 123

        acquired: list[str] = []

        async def acquire_player_lock(player_id: str) -> None:
            async with lock_manager.player_state_lock(chat_id, player_id):
                acquired.append(player_id)
                await asyncio.sleep(0.1)

        loop = asyncio.get_running_loop()
        start = loop.time()

        await asyncio.gather(
            acquire_player_lock("player1"),
            acquire_player_lock("player2"),
            acquire_player_lock("player3"),
        )

        duration = loop.time() - start

        assert duration < 0.25, f"Took {duration}s, expected <0.25s"
        assert len(acquired) == 3

    @pytest.mark.asyncio
    async def test_player_lock_blocks_same_player(self):
        """Same player locks serialize."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_serial"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 123
        player_id = "player1"

        acquired: list[str] = []

        async def acquire_player_lock(label: str) -> None:
            async with lock_manager.player_state_lock(chat_id, player_id):
                acquired.append(f"{label}_start")
                await asyncio.sleep(0.05)
                acquired.append(f"{label}_end")

        await asyncio.gather(
            acquire_player_lock("task1"),
            acquire_player_lock("task2"),
        )

        assert acquired[0].startswith("task")
        assert acquired[1] == acquired[0].replace("start", "end")

    @pytest.mark.asyncio
    async def test_lock_hierarchy_validation_raises_error(self):
        """Acquiring higher lock while holding lower raises error."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_hierarchy"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 123

        with pytest.raises(LockOrderError) as exc_info:
            async with lock_manager.player_state_lock(chat_id, "player1"):
                async with lock_manager.table_write_lock(chat_id):
                    pass

        assert "hierarchy violation" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_same_level_locks_allowed(self):
        """Same-level locks can be held together (deck + betting)."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_same_level"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 123

        async with lock_manager.deck_lock(chat_id):
            async with lock_manager.betting_round_lock(chat_id):
                pass

    @pytest.mark.asyncio
    async def test_backward_compatibility_mode(self):
        """When disabled, falls back to table_write_lock."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_compat"),
            enable_fine_grained_locks=False,
            redis_pool=None,
        )
        chat_id = 123

        async with lock_manager.player_state_lock(chat_id, "player1"):
            async with lock_manager.table_write_lock(chat_id):
                pass
