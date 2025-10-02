"""Countdown message queue implementation."""

from __future__ import annotations

import asyncio
from asyncio import Queue
from dataclasses import dataclass
from typing import Optional


@dataclass(eq=False)
class CountdownMessage:
    """Represents a single countdown update to be sent to Telegram.

    Attributes:
        chat_id: Telegram chat identifier.
        message_id: Message to edit with countdown text.
        text: Formatted countdown text (e.g., "⏳ Game starts in 5 seconds...").
        timestamp: Monotonic time when message was created.
        cancelled: Flag indicating if this message should be skipped.
    """

    chat_id: int
    message_id: int
    text: str
    timestamp: float
    cancelled: bool = False

    def __eq__(self, other: object) -> bool:
        """Compare countdown messages by their chat and message identifiers."""
        if not isinstance(other, CountdownMessage):
            return NotImplemented
        return (self.chat_id, self.message_id) == (other.chat_id, other.message_id)

    def __hash__(self) -> int:
        """Allow ``CountdownMessage`` to be used in sets and dictionaries."""
        return hash((self.chat_id, self.message_id))


class CountdownMessageQueue:
    """Lock-free message queue for countdown updates."""

    def __init__(self, max_size: int = 1000) -> None:
        """Initialize the countdown queue.

        Args:
            max_size: Maximum number of messages in queue (prevents memory issues).
        """
        self._queue: Queue[CountdownMessage] = Queue(maxsize=max_size)
        self._active_countdowns: dict[tuple[int, int], CountdownMessage] = {}
        self._tracking_lock = asyncio.Lock()

    async def enqueue(self, chat_id: int, message_id: int, text: str) -> CountdownMessage:
        """Enqueue a countdown update.

        Args:
            chat_id: Telegram chat ID.
            message_id: Message to update.
            text: New text content.

        Returns:
            CountdownMessage object that was enqueued.

        Raises:
            asyncio.QueueFull: If queue is at max capacity.
        """
        import time

        key = (chat_id, message_id)
        msg = CountdownMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            timestamp=time.monotonic(),
            cancelled=False,
        )

        if self._queue.full():
            raise asyncio.QueueFull

        async with self._tracking_lock:
            if key in self._active_countdowns:
                old_msg = self._active_countdowns[key]
                old_msg.cancelled = True

            self._active_countdowns[key] = msg

        await self._queue.put(msg)
        return msg

    async def dequeue(self) -> Optional[CountdownMessage]:
        """Dequeue the next countdown message.

        Returns:
            CountdownMessage if available, ``None`` if queue is empty.
        """
        try:
            msg = await asyncio.wait_for(self._queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

        key = (msg.chat_id, msg.message_id)
        async with self._tracking_lock:
            if key in self._active_countdowns:
                tracked_msg = self._active_countdowns[key]
                if tracked_msg is msg:
                    del self._active_countdowns[key]

        return msg

    def cancel_countdown(self, chat_id: int, message_id: int) -> bool:
        """Cancel an active countdown by marking its messages as cancelled.

        Args:
            chat_id: Telegram chat ID.
            message_id: Message ID to cancel.

        Returns:
            ``True`` if countdown was found and cancelled, ``False`` otherwise.
        """
        key = (chat_id, message_id)
        if key in self._active_countdowns:
            self._active_countdowns[key].cancelled = True
            return True
        return False

    def get_queue_depth(self) -> int:
        """Get current number of messages waiting in queue."""
        return self._queue.qsize()

    def get_active_countdowns(self) -> int:
        """Get number of active (non-cancelled) countdowns."""
        return len(self._active_countdowns)

    async def clear(self) -> None:
        """Clear all messages from queue and cancel active countdowns."""
        async with self._tracking_lock:
            for msg in self._active_countdowns.values():
                msg.cancelled = True
            self._active_countdowns.clear()

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - defensive guard
                break

