import asyncio
import logging
from typing import List

import pytest

from pokerapp.lock_manager import LockManager, _InMemoryActionLockBackend


@pytest.mark.asyncio
async def test_estimate_queue_position_redis(redis_pool) -> None:
    """Queue position estimation counts active locks per chat."""

    manager = LockManager(logger=logging.getLogger("queue-estimate"), redis_pool=redis_pool)
    chat_id = 1
    active_tokens: List[tuple[int, str]] = []

    for user_id in (100, 101, 102):
        token = await manager.acquire_action_lock(chat_id, user_id, "fold")
        assert token is not None
        active_tokens.append((user_id, token))

    queue_pos = await manager._estimate_queue_position(chat_id, 103)
    assert queue_pos == 3

    release_user, release_token = active_tokens.pop(0)
    released = await manager.release_action_lock(chat_id, release_user, "fold", release_token)
    assert released is True

    queue_pos_after_release = await manager._estimate_queue_position(chat_id, 103)
    assert queue_pos_after_release == 2

    for user_id, token in active_tokens:
        await manager.release_action_lock(chat_id, user_id, "fold", token)


@pytest.mark.asyncio
async def test_progress_callback_deduplication(redis_pool) -> None:
    """Duplicate queue positions should not trigger repeated callbacks."""

    manager = LockManager(logger=logging.getLogger("queue-progress"), redis_pool=redis_pool)
    chat_id = 77
    blocker_token = await manager.acquire_action_lock(chat_id, 2, "call")
    assert blocker_token is not None

    feedback_calls: List[int] = []

    async def mock_callback(metadata):
        position = metadata.get("queue_position")
        try:
            position_int = int(position)
        except (TypeError, ValueError):
            return
        if position_int > 0:
            feedback_calls.append(position_int)

    async def release_blocker() -> None:
        await asyncio.sleep(0.05)
        await manager.release_action_lock(chat_id, 2, "call", blocker_token)

    release_task = asyncio.create_task(release_blocker())

    lock_acquisition = await manager.acquire_action_lock_with_retry(
        chat_id=chat_id,
        user_id=2,
        action_data="call",
        max_retries=5,
        initial_backoff=0.01,
        total_timeout=0.5,
        progress_callback=mock_callback,
    )

    assert lock_acquisition is not None
    token, _metadata = lock_acquisition
    assert feedback_calls, "Expected at least one progress callback invocation"
    assert len(feedback_calls) == len(set(feedback_calls))
    assert all(position > 0 for position in feedback_calls)

    await manager.release_action_lock(chat_id, 2, "call", token)
    await release_task


@pytest.mark.asyncio
async def test_in_memory_backend_keys_wildcard() -> None:
    """In-memory backend honours wildcard pattern matching for keys."""

    backend = _InMemoryActionLockBackend()

    await backend.set("action:lock:1:100:fold", "token1", ex=10, nx=True)
    await backend.set("action:lock:1:101:call", "token2", ex=10, nx=True)
    await backend.set("action:lock:2:100:raise", "token3", ex=10, nx=True)

    keys_chat_one = await backend.keys("action:lock:1:*")
    assert len(keys_chat_one) == 2
    assert all(key.startswith("action:lock:1:") for key in keys_chat_one)

    exact_match = await backend.keys("action:lock:2:100:raise")
    assert exact_match == ["action:lock:2:100:raise"]
