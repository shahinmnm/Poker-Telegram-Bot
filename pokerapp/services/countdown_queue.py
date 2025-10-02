"""Countdown message queue implementation."""

from __future__ import annotations

import asyncio
from asyncio import Queue
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


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
    anchor_key: str = ""
    duration_seconds: float = 0.0
    formatter: Optional[Callable[[float], str]] = None
    on_complete: Optional[Callable[[], Optional[Awaitable[None]]]] = None

    def format_text(self, remaining: float) -> str:
        """Return the formatted countdown text for the remaining time."""

        if self.formatter is not None:
            return self.formatter(remaining)
        return self.text

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

    async def enqueue(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        duration_seconds: float = 0.0,
        formatter: Optional[Callable[[float], str]] = None,
        on_complete: Optional[Callable[[], Optional[Awaitable[None]]]] = None,
        anchor_key: Optional[str] = None,
    ) -> CountdownMessage:
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
        anchor_value = anchor_key or f"{chat_id}:{message_id}"

        msg = CountdownMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            timestamp=time.monotonic(),
            cancelled=False,
            duration_seconds=duration_seconds,
            formatter=formatter,
            on_complete=on_complete,
            anchor_key=anchor_value,
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

    async def remove_anchor(self, anchor_key: str) -> None:
        """Remove and cancel all countdowns associated with ``anchor_key``."""

        async with self._tracking_lock:
            keys_to_remove = [
                key
                for key, message in self._active_countdowns.items()
                if getattr(message, "anchor_key", f"{message.chat_id}:{message.message_id}")
                == anchor_key
            ]

            for key in keys_to_remove:
                message = self._active_countdowns.pop(key, None)
                if message is not None:
                    message.cancelled = True

    async def cancel_countdown_for_chat(self, chat_id: int) -> int:
        """Cancel all active countdowns for a specific chat."""

        cancelled_count = 0

        async with self._tracking_lock:
            keys_to_cancel = [
                key for key in self._active_countdowns.keys() if key[0] == chat_id
            ]
            for key in keys_to_cancel:
                message = self._active_countdowns[key]
                message.cancelled = True
                cancelled_count += 1

        return cancelled_count

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

        await self.clear_all()

    async def clear_all(self) -> int:
        """Cancel all countdowns and drain the queue.

        Returns:
            The number of countdown entries that were cancelled or removed.
        """

        async with self._tracking_lock:
            active = list(self._active_countdowns.values())
            for msg in active:
                msg.cancelled = True
            cancelled = len(active)
            self._active_countdowns.clear()

        drained = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                drained += 1
                try:
                    self._queue.task_done()
                except ValueError:
                    # ``task_done`` raises when called more times than items enqueued.
                    break

        return cancelled + drained

