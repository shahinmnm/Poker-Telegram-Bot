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
    async def test_table_read_then_player_lock_respects_hierarchy(self):
        """Read locks can be upgraded to player locks without violation."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_read_player"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 321

        async with lock_manager.table_read_lock(chat_id):
            async with lock_manager.player_state_lock(chat_id, "player1"):
                pass

    @pytest.mark.asyncio
    async def test_player_to_table_write_allows_ascending(self):
        """Ascending from player to table write lock should be permitted."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_hierarchy"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 123

        async with lock_manager.player_state_lock(chat_id, "player1"):
            async with lock_manager.table_write_lock(chat_id):
                pass

    @pytest.mark.asyncio
    async def test_pot_to_player_lock_violates_hierarchy(self):
        """Descending from pot to player lock raises hierarchy error."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_pot_player"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 456

        async with lock_manager.pot_lock(chat_id):
            with pytest.raises(LockOrderError) as exc_info:
                async with lock_manager.player_state_lock(chat_id, "player2"):
                    pass

        assert "hierarchy violation" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_player_to_pot_lock_allows_ascending(self):
        """Ascending directly from player to pot should be permitted."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_player_pot"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 654

        async with lock_manager.player_state_lock(chat_id, "player3"):
            async with lock_manager.pot_lock(chat_id):
                pass

    @pytest.mark.asyncio
    async def test_read_to_pot_lock_allows_ascending_skip(self):
        """Skipping the player level between read and pot should be permitted."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_read_pot"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 987

        async with lock_manager.table_read_lock(chat_id):
            async with lock_manager.pot_lock(chat_id):
                pass

    @pytest.mark.asyncio
    async def test_lock_released_after_exception(self):
        """Locks are released when an error occurs inside the guard."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_cleanup"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 789

        class TestError(RuntimeError):
            pass

        with pytest.raises(TestError):
            async with lock_manager.deck_lock(chat_id):
                raise TestError()

        # After exception the lock should be available again.
        async with lock_manager.deck_lock(chat_id):
            pass

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

    @pytest.mark.asyncio
    async def test_full_hierarchy_nested_sequence_allowed(self):
        """Nested acquisition through every hierarchy level should succeed."""

        lock_manager = LockManager(
            logger=logging.getLogger("fine_grained_full_sequence"),
            enable_fine_grained_locks=True,
            redis_pool=None,
        )
        chat_id = 246

        release_write = asyncio.Event()
        write_acquired = asyncio.Event()

        async def acquire_table_write() -> None:
            async with lock_manager.table_write_lock(chat_id):
                write_acquired.set()
                await release_write.wait()

        async with lock_manager.table_read_lock(chat_id):
            async with lock_manager.player_state_lock(chat_id, "player4"):
                async with lock_manager.pot_lock(chat_id):
                    async with lock_manager.deck_lock(chat_id):
                        write_task = asyncio.create_task(acquire_table_write())
                        await asyncio.sleep(0)
                        assert not write_task.done(), "Hierarchy check should not fail"

        await write_acquired.wait()
        release_write.set()
        await write_task
