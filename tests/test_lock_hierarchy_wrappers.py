import logging

import pytest

from pokerapp.lock_manager import LockHierarchyViolation, LockManager


@pytest.mark.asyncio
async def test_wallet_then_table_lock_allowed():
    manager = LockManager(logger=logging.getLogger("hierarchy-allowed"))

    async with manager.acquire_wallet_lock(user_id=7):
        async with manager.acquire_table_write_lock(chat_id=101):
            # Nested acquisition following the hierarchy should succeed.
            pass


@pytest.mark.asyncio
async def test_table_then_wallet_lock_violates_hierarchy():
    manager = LockManager(logger=logging.getLogger("hierarchy-violation"))

    with pytest.raises(LockHierarchyViolation):
        async with manager.acquire_table_write_lock(chat_id=102):
            async with manager.acquire_wallet_lock(user_id=8):
                pass  # pragma: no cover - hierarchy violation should raise


@pytest.mark.asyncio
async def test_player_lock_between_wallet_and_table():
    manager = LockManager(logger=logging.getLogger("hierarchy-player"))

    async with manager.acquire_wallet_lock(user_id=9):
        async with manager.acquire_player_lock(chat_id=103, player_id=9):
            async with manager.acquire_table_write_lock(chat_id=103):
                # The canonical acquisition order should not raise.
                pass


@pytest.mark.asyncio
async def test_table_then_player_lock_violates_hierarchy():
    manager = LockManager(logger=logging.getLogger("hierarchy-player-violation"))

    with pytest.raises(LockHierarchyViolation):
        async with manager.acquire_table_write_lock(chat_id=104):
            async with manager.acquire_player_lock(chat_id=104, player_id=10):
                pass  # pragma: no cover - hierarchy violation should raise
