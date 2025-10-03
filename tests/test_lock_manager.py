import asyncio
import contextlib
import logging
from typing import List

import pytest

from pokerapp.lock_manager import LockManager, LockOrderError


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(record)


@pytest.mark.asyncio
async def test_lock_manager_acquire_when_free() -> None:
    logger = logging.getLogger("lock_manager_test_free")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    key = "stage:1"
    acquired = await manager.acquire(key, timeout=0.5)
    assert acquired
    manager.release(key)


@pytest.mark.asyncio
async def test_lock_manager_retries_before_acquire() -> None:
    logger = logging.getLogger("lock_manager_test_retry")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=0.5,
        max_retries=2,
        retry_backoff_seconds=0.05,
    )

    key = "stage:retry"

    async def holder() -> None:
        async with manager.guard(key, timeout=1):
            await asyncio.sleep(0.3)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    try:
        acquired = await manager.acquire(key, timeout=0.5)
        assert acquired
        manager.release(key)
        await hold_task

        assert any(
            "Timeout acquiring Lock" in record.getMessage()
            for record in handler.records
        )
    finally:
        if not hold_task.done():
            await hold_task
        logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_lock_manager_guard_timeout() -> None:
    logger = logging.getLogger("lock_manager_test_timeout")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=0.1,
        max_retries=1,
        retry_backoff_seconds=0.05,
    )

    key = "stage:timeout"

    async def holder() -> None:
        async with manager.guard(key, timeout=1):
            await asyncio.sleep(0.3)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError):
        async with manager.guard(key, timeout=0.1):
            pass

    await hold_task


@pytest.mark.asyncio
async def test_lock_manager_context_logging() -> None:
    logger = logging.getLogger("lock_manager_test_context")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    context = {"request_category": "test"}
    context_extra = {"game_id": "abc"}
    async with manager.guard("stage:42", context=context, context_extra=context_extra):
        pass

    messages = [record.getMessage() for record in handler.records]
    identity_fragment = "Lock 'stage:42' (level=1, chat_id=42, game_id=abc)"
    context_fragment = (
        "[context: chat_id=42, game_id='abc', lock_category='stage', lock_key='stage:42',"
        " lock_level=1, lock_name='stage:42', request_category='test']"
    )
    assert any(
        "acquired" in message
        and identity_fragment in message
        and context_fragment in message
        for message in messages
    )
    assert any(
        "released" in message
        and identity_fragment in message
        and context_fragment in message
        for message in messages
    )

    assert any(
        record.__dict__.get("event_type") == "lock_acquired"
        and record.__dict__.get("call_site_function") not in {None, "unknown"}
        for record in handler.records
    )
    assert any(
        record.__dict__.get("event_type") == "lock_released"
        and record.__dict__.get("release_function") not in {None, "unknown"}
        for record in handler.records
    )

    logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_lock_manager_category_timeout_override() -> None:
    logger = logging.getLogger("lock_manager_test_category_timeout")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=5,
        max_retries=0,
        retry_backoff_seconds=0.01,
        category_timeouts={"engine_stage": 0.05},
    )

    key = "stage:category-timeout"

    async def holder() -> None:
        async with manager.guard(key, timeout=0.2):
            await asyncio.sleep(0.2)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError):
        async with manager.guard(key):
            pass

    await hold_task


@pytest.mark.asyncio
async def test_lock_manager_metrics_recording() -> None:
    logger = logging.getLogger("lock_manager_test_metrics")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=0.5,
        max_retries=0,
        retry_backoff_seconds=0.01,
        category_timeouts={"engine_stage": 0.1},
    )

    key = "stage:metrics"

    async def holder() -> None:
        async with manager.guard(key, timeout=0.2):
            await asyncio.sleep(0.15)

    hold_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)

    with pytest.raises(TimeoutError):
        async with manager.guard(key, timeout=0.05):
            pass

    await hold_task

    metrics = manager.metrics
    assert metrics["lock_timeouts"] >= 1
    assert metrics["lock_contention"] >= 1


@pytest.mark.asyncio
async def test_lock_manager_release_from_loop_callback() -> None:
    logger = logging.getLogger("lock_manager_test_callback_release")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    key = "stage:callback-release"
    assert await manager.acquire(key, timeout=0.5)

    loop = asyncio.get_running_loop()
    release_future: asyncio.Future[None] = loop.create_future()

    def _release_from_callback() -> None:
        try:
            manager.release(key)
        except Exception as exc:  # pragma: no cover - defensive
            if not release_future.done():
                release_future.set_exception(exc)
        else:
            if not release_future.done():
                release_future.set_result(None)

    loop.call_soon(_release_from_callback)
    await release_future

    reacquired = await manager.acquire(key, timeout=0.5)
    assert reacquired
    manager.release(key)


def test_lock_manager_empty_snapshot_logs_debug() -> None:
    logger = logging.getLogger("lock_manager_test_empty_snapshot")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    manager._log_lock_snapshot_on_timeout("empty_stage")

    snapshot_records = [
        record
        for record in handler.records
        if record.getMessage().startswith("Lock snapshot (empty_stage)")
    ]
    assert snapshot_records, "Expected lock snapshot log to be emitted"
    assert all(record.levelno < logging.WARNING for record in snapshot_records)

    logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_lock_manager_allows_mixed_lock_levels_without_release() -> None:
    logger = logging.getLogger("lock_manager_test_hierarchy")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    async with manager.guard(
        "stage:hierarchy", context={"order": "engine_stage"}
    ):
        async with manager.guard(
            "pokerbot:player_report:hierarchy",
            context={"order": "player_report"},
        ):
            assert True


@pytest.mark.asyncio
async def test_lock_manager_detects_reverse_lock_order() -> None:
    logger = logging.getLogger("lock_manager_test_hierarchy_violation")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    async with manager.guard("wallet:reverse", context={"order": "wallet"}):
        with pytest.raises(LockOrderError):
            await manager.acquire(
                "stage:reverse", context={"order": "engine_stage"}
            )


@pytest.mark.asyncio
async def test_lock_manager_allows_increasing_lock_order() -> None:
    logger = logging.getLogger("lock_manager_test_increasing_violation")
    manager = LockManager(logger=logger, default_timeout_seconds=1)

    async with manager.guard(
        "stage:increasing", context={"order": "engine_stage"}
    ):
        acquired = await manager.acquire(
            "chat:increasing", context={"order": "chat"}
        )
        try:
            assert acquired is True
        finally:
            manager.release("chat:increasing")


@pytest.mark.asyncio
async def test_detect_deadlock_cycle_detection() -> None:
    logger = logging.getLogger("lock_manager_test_deadlock")
    manager = LockManager(
        logger=logger,
        default_timeout_seconds=5,
        max_retries=0,
        retry_backoff_seconds=0.01,
    )

    ready_stage = asyncio.Event()
    ready_player = asyncio.Event()
    start_cycle = asyncio.Event()

    async def task_one() -> None:
        async with manager.guard(
            "stage:cycle",
            timeout=5,
            context={"task": "one"},
            level=5,
        ):
            ready_stage.set()
            await start_cycle.wait()
            try:
                acquired = await manager.acquire(
                    "wallet:cycle",
                    timeout=5,
                    context={"task": "one"},
                    level=5,
                )
                if acquired:
                    manager.release("wallet:cycle")
            except asyncio.CancelledError:
                raise

    async def task_two() -> None:
        async with manager.guard(
            "wallet:cycle",
            timeout=5,
            context={"task": "two"},
            level=5,
        ):
            ready_player.set()
            await start_cycle.wait()
            try:
                acquired = await manager.acquire(
                    "stage:cycle",
                    timeout=5,
                    context={"task": "two"},
                    level=5,
                )
                if acquired:
                    manager.release("stage:cycle")
            except asyncio.CancelledError:
                raise

    task_one_future = asyncio.create_task(task_one(), name="cycle-one")
    task_two_future = asyncio.create_task(task_two(), name="cycle-two")

    await ready_stage.wait()
    await ready_player.wait()
    start_cycle.set()

    await asyncio.sleep(0.1)
    snapshot = manager.detect_deadlock()

    assert snapshot["waiting"]
    assert snapshot["cycles"]
    assert any("cycle-one" in node for cycle in snapshot["cycles"] for node in cycle)
    assert any("cycle-two" in node for cycle in snapshot["cycles"] for node in cycle)

    task_one_future.cancel()
    task_two_future.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task_one_future
    with contextlib.suppress(asyncio.CancelledError):
        await task_two_future


@pytest.fixture
def rw_lock_manager() -> LockManager:
    """Return a fresh lock manager instance for read/write lock tests."""

    logger = logging.getLogger("lock_manager_test_rw")
    logger.setLevel(logging.DEBUG)
    return LockManager(logger=logger)


@pytest.mark.asyncio
async def test_table_read_lock_allows_concurrent_readers(
    rw_lock_manager: LockManager,
) -> None:
    chat_id = 4242
    events: list[str] = []

    async def reader(idx: int) -> None:
        async with rw_lock_manager.table_read_lock(chat_id):
            events.append(f"start_{idx}")
            await asyncio.sleep(0.05)
            events.append(f"end_{idx}")

    await asyncio.gather(*(reader(i) for i in range(4)))

    assert events[:4] == [f"start_{i}" for i in range(4)]
    assert len(events) == 8


@pytest.mark.asyncio
async def test_table_write_lock_blocks_readers(
    rw_lock_manager: LockManager,
) -> None:
    chat_id = 5050
    timeline: list[str] = []

    async def writer() -> None:
        async with rw_lock_manager.table_write_lock(chat_id):
            timeline.append("writer_start")
            await asyncio.sleep(0.1)
            timeline.append("writer_end")

    async def reader(name: str) -> None:
        await asyncio.sleep(0.02)
        async with rw_lock_manager.table_read_lock(chat_id):
            timeline.append(name)

    await asyncio.gather(writer(), reader("reader_a"), reader("reader_b"))

    assert timeline[0] == "writer_start"
    assert timeline[1] == "writer_end"
    assert "reader_a" in timeline[2:]
    assert "reader_b" in timeline[2:]


@pytest.mark.asyncio
async def test_table_write_lock_waits_for_readers(
    rw_lock_manager: LockManager,
) -> None:
    chat_id = 6060
    timeline: list[str] = []

    async def reader(idx: int) -> None:
        async with rw_lock_manager.table_read_lock(chat_id):
            timeline.append(f"reader_{idx}_start")
            await asyncio.sleep(0.08)
            timeline.append(f"reader_{idx}_end")

    async def writer() -> None:
        await asyncio.sleep(0.02)
        async with rw_lock_manager.table_write_lock(chat_id):
            timeline.append("writer_start")

    await asyncio.gather(reader(1), reader(2), writer())

    assert timeline[:2] == ["reader_1_start", "reader_2_start"]
    assert timeline[-1] == "writer_start"


@pytest.mark.asyncio
async def test_stage_lock_metrics_updated(rw_lock_manager: LockManager) -> None:
    chat_id = 11

    async with rw_lock_manager.stage_lock(chat_id):
        await asyncio.sleep(0.03)

    async with rw_lock_manager.stage_lock(chat_id):
        await asyncio.sleep(0.07)

    metrics = rw_lock_manager.get_metrics()

    assert metrics["stage_lock_acquisitions"] == 2
    assert metrics["stage_lock_avg_hold_time"] >= 0.03
    assert metrics["stage_lock_p95_hold_time"] >= 0.07


@pytest.mark.asyncio
async def test_table_lock_metrics_tracking(rw_lock_manager: LockManager) -> None:
    chat_id = 12

    for _ in range(2):
        async with rw_lock_manager.table_read_lock(chat_id):
            await asyncio.sleep(0.01)

    async with rw_lock_manager.table_write_lock(chat_id):
        await asyncio.sleep(0.02)

    stats = rw_lock_manager.get_metrics()["table_lock_stats"][chat_id]
    assert stats["read_acquisitions"] == 2
    assert stats["write_acquisitions"] == 1
    assert stats["avg_read_time"] >= 0.01
    assert stats["avg_write_time"] >= 0.02


@pytest.mark.asyncio
async def test_table_lock_no_deadlock_on_alternating_access(
    rw_lock_manager: LockManager,
) -> None:
    chat_id = 13

    async def reader() -> None:
        async with rw_lock_manager.table_read_lock(chat_id):
            await asyncio.sleep(0)

    async def writer() -> None:
        async with rw_lock_manager.table_write_lock(chat_id):
            await asyncio.sleep(0)

    await asyncio.gather(*[reader() if i % 2 == 0 else writer() for i in range(60)])

    stats = rw_lock_manager.get_metrics()["table_lock_stats"][chat_id]
    assert stats["read_acquisitions"] == 30
    assert stats["write_acquisitions"] == 30


@pytest.mark.asyncio
async def test_lock_manager_metrics_reset_clears_rw_metrics(
    rw_lock_manager: LockManager,
) -> None:
    chat_id = 14

    async with rw_lock_manager.stage_lock(chat_id):
        pass

    async with rw_lock_manager.table_read_lock(chat_id):
        pass

    rw_lock_manager.reset_metrics()
    metrics = rw_lock_manager.get_metrics()

    assert metrics["stage_lock_acquisitions"] == 0
    assert metrics["table_lock_stats"] == {}


@pytest.mark.asyncio
async def test_multiple_writers_queue_fifo(rw_lock_manager: LockManager) -> None:
    chat_id = 12345
    order: list[int] = []

    async def writer(writer_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        async with rw_lock_manager.table_write_lock(chat_id):
            order.append(writer_id)
            await asyncio.sleep(0.05)

    tasks = [
        asyncio.create_task(writer(idx, idx * 0.01))
        for idx in range(1, 6)
    ]

    await asyncio.gather(*tasks)

    assert order == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_reader_defers_to_waiting_writer(rw_lock_manager: LockManager) -> None:
    rw_lock_manager.writer_priority = True
    chat_id = 54321
    events: list[str] = []

    async def reader(reader_id: int) -> None:
        async with rw_lock_manager.table_read_lock(chat_id):
            events.append(f"reader_{reader_id}_acquired")
            await asyncio.sleep(0.1)
        events.append(f"reader_{reader_id}_released")

    async def writer() -> None:
        await asyncio.sleep(0.02)
        events.append("writer_queued")
        async with rw_lock_manager.table_write_lock(chat_id):
            events.append("writer_acquired")
            await asyncio.sleep(0.05)
        events.append("writer_released")

    async def late_reader() -> None:
        await asyncio.sleep(0.03)
        events.append("late_reader_queued")
        async with rw_lock_manager.table_read_lock(chat_id):
            events.append("late_reader_acquired")

    await asyncio.gather(reader(1), writer(), late_reader())

    assert events.index("reader_1_acquired") < events.index("writer_queued")
    assert events.index("writer_queued") < events.index("late_reader_queued")
    assert events.index("reader_1_released") < events.index("writer_acquired")
    assert events.index("writer_released") < events.index("late_reader_acquired")


@pytest.mark.asyncio
async def test_cancelled_reader_releases_lock(rw_lock_manager: LockManager) -> None:
    chat_id = 777

    async def cancelled_reader() -> None:
        async with rw_lock_manager.table_read_lock(chat_id):
            await asyncio.sleep(10)

    task = asyncio.create_task(cancelled_reader())
    await asyncio.sleep(0.01)
    task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await task

    state = rw_lock_manager._table_rw_locks.get(chat_id)
    assert state is not None
    assert state.reader_count == 0
    assert not state.has_active_writer


@pytest.mark.asyncio
async def test_cancelled_writer_allows_new_writers(
    rw_lock_manager: LockManager,
) -> None:
    chat_id = 888
    writer_succeeded = False

    async def cancelled_writer() -> None:
        async with rw_lock_manager.table_write_lock(chat_id):
            await asyncio.sleep(10)

    async def subsequent_writer() -> None:
        nonlocal writer_succeeded
        async with rw_lock_manager.table_write_lock(chat_id):
            writer_succeeded = True

    task = asyncio.create_task(cancelled_writer())
    await asyncio.sleep(0.01)
    task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await task

    await subsequent_writer()
    assert writer_succeeded


@pytest.mark.asyncio
async def test_wait_time_metrics_accuracy(rw_lock_manager: LockManager) -> None:
    chat_id = 999

    async def blocking_writer() -> None:
        async with rw_lock_manager.table_write_lock(chat_id):
            await asyncio.sleep(0.2)

    async def waiting_reader() -> None:
        await asyncio.sleep(0.05)
        async with rw_lock_manager.table_read_lock(chat_id):
            pass

    await asyncio.gather(blocking_writer(), waiting_reader())

    state = rw_lock_manager._table_rw_locks[chat_id]
    metrics = state.metrics

    assert metrics.total_read_wait_time > 0.14
    assert metrics.max_read_wait_time > 0.14
    assert metrics.average_read_wait_time() > 0.14
