"""Tests for countdown queue implementation."""

import asyncio
import pytest

from pokerapp.services.countdown_queue import CountdownMessage, CountdownMessageQueue


@pytest.mark.asyncio
class TestCountdownMessage:
    """Tests for the ``CountdownMessage`` dataclass."""

    def test_message_creation(self) -> None:
        """Test basic message creation."""
        msg = CountdownMessage(
            chat_id=123,
            message_id=456,
            text="⏳ Game starts in 5 seconds",
            timestamp=1000.0,
        )

        assert msg.chat_id == 123
        assert msg.message_id == 456
        assert msg.text == "⏳ Game starts in 5 seconds"
        assert msg.timestamp == 1000.0
        assert msg.cancelled is False

    def test_message_hashable(self) -> None:
        """Ensure messages can be used in sets and dictionaries."""
        msg1 = CountdownMessage(123, 456, "text", 1000.0)
        msg2 = CountdownMessage(123, 456, "other", 2000.0)

        assert hash(msg1) == hash(msg2)

        msg_set = {msg1, msg2}
        assert len(msg_set) == 1


@pytest.mark.asyncio
class TestCountdownMessageQueue:
    """Tests for ``CountdownMessageQueue`` behavior."""

    async def test_basic_enqueue_dequeue(self) -> None:
        """Test basic enqueue and dequeue operations."""
        queue = CountdownMessageQueue()

        msg = await queue.enqueue(
            chat_id=123,
            message_id=456,
            text="⏳ Test countdown",
        )

        assert msg.chat_id == 123
        assert msg.message_id == 456
        assert msg.cancelled is False
        assert queue.get_queue_depth() == 1

        dequeued = await queue.dequeue()

        assert dequeued is not None
        assert dequeued.chat_id == 123
        assert dequeued.message_id == 456
        assert queue.get_queue_depth() == 0

    async def test_empty_queue_dequeue(self) -> None:
        """Test dequeue on empty queue returns ``None``."""
        queue = CountdownMessageQueue()

        msg = await queue.dequeue()
        assert msg is None

    async def test_cancel_existing_countdown(self) -> None:
        """Ensure old countdowns are cancelled when new ones arrive."""
        queue = CountdownMessageQueue()

        msg1 = await queue.enqueue(123, 456, "⏳ First")
        assert msg1.cancelled is False

        msg2 = await queue.enqueue(123, 456, "⏳ Second")

        assert msg1.cancelled is True
        assert msg2.cancelled is False
        assert queue.get_queue_depth() == 2

        dequeued1 = await queue.dequeue()
        assert dequeued1 is not None
        assert dequeued1.cancelled is True

        dequeued2 = await queue.dequeue()
        assert dequeued2 is not None
        assert dequeued2.cancelled is False

    async def test_cancel_countdown_method(self) -> None:
        """Test explicit ``cancel_countdown`` method."""
        queue = CountdownMessageQueue()

        msg = await queue.enqueue(123, 456, "⏳ Test")
        assert msg.cancelled is False

        result = queue.cancel_countdown(123, 456)
        assert result is True
        assert msg.cancelled is True

        result = queue.cancel_countdown(999, 999)
        assert result is False

    async def test_multiple_concurrent_countdowns(self) -> None:
        """Test multiple countdowns for different chats."""
        queue = CountdownMessageQueue()

        msg1 = await queue.enqueue(111, 456, "⏳ Chat 111")
        msg2 = await queue.enqueue(222, 456, "⏳ Chat 222")
        msg3 = await queue.enqueue(333, 456, "⏳ Chat 333")

        assert queue.get_queue_depth() == 3
        assert queue.get_active_countdowns() == 3

        assert not msg1.cancelled
        assert not msg2.cancelled
        assert not msg3.cancelled

    async def test_queue_depth_tracking(self) -> None:
        """Ensure queue depth is accurately tracked."""
        queue = CountdownMessageQueue()

        assert queue.get_queue_depth() == 0

        await queue.enqueue(123, 456, "⏳ 1")
        assert queue.get_queue_depth() == 1

        await queue.enqueue(123, 789, "⏳ 2")
        assert queue.get_queue_depth() == 2

        await queue.dequeue()
        assert queue.get_queue_depth() == 1

        await queue.dequeue()
        assert queue.get_queue_depth() == 0

    async def test_clear_queue(self) -> None:
        """Test clearing all messages and countdowns."""
        queue = CountdownMessageQueue()

        msg1 = await queue.enqueue(123, 456, "⏳ 1")
        msg2 = await queue.enqueue(123, 789, "⏳ 2")
        msg3 = await queue.enqueue(456, 123, "⏳ 3")

        assert queue.get_queue_depth() == 3
        assert queue.get_active_countdowns() == 3

        await queue.clear()

        assert queue.get_queue_depth() == 0
        assert queue.get_active_countdowns() == 0
        assert msg1.cancelled
        assert msg2.cancelled
        assert msg3.cancelled

    async def test_concurrent_enqueues(self) -> None:
        """Test concurrent enqueue operations are thread-safe."""
        queue = CountdownMessageQueue()

        async def enqueue_many(chat_id: int, count: int) -> None:
            for i in range(count):
                await queue.enqueue(chat_id=chat_id, message_id=999, text=f"⏳ {i}")

        await asyncio.gather(
            enqueue_many(111, 10),
            enqueue_many(222, 10),
            enqueue_many(333, 10),
        )

        assert queue.get_queue_depth() == 30
        assert queue.get_active_countdowns() == 3

    async def test_queue_full_handling(self) -> None:
        """Test behavior when queue reaches maximum size."""
        queue = CountdownMessageQueue(max_size=5)

        for i in range(5):
            await queue.enqueue(123, i, f"⏳ {i}")

        assert queue.get_queue_depth() == 5

        with pytest.raises(asyncio.QueueFull):
            await asyncio.wait_for(queue.enqueue(123, 999, "⏳ overflow"), timeout=0.1)

