"""Async update scheduler for Telegram messaging.

This module centralises all per-chat throttling, deduplication and batching of
Telegram Bot API calls.  The scheduler is intentionally framework agnostic so
that it can be unit-tested without touching the actual API.  The high level
flow is as follows:

* Each chat gets its own worker task and lock.  Updates for one chat therefore
  never block another chat, which keeps the bot responsive when many tables
  are active at the same time.
* An update is uniquely identified by a ``key`` (e.g. ``"turn:<game_id>"``).
  Multiple updates for the same key are coalesced – only the most recent
  payload is delivered.  Older requests simply await the result of the latest
  payload.  This prevents rapid-fire edits of the same message.
* Before an update is queued the scheduler compares the requested state with
  the last successfully delivered state.  If nothing has changed the update is
  skipped immediately without touching the API.
* A small delay (``flush_interval``) between non-urgent updates keeps each chat
  comfortably below Telegram's documented webhook limits (about 30 updates per
  second) while still feeling instant to players.  Critical updates can be
  flagged as ``urgent`` and bypass the delay.

The scheduler only cares about *when* something should be sent.  The caller is
responsible for providing a coroutine factory that performs the actual
Telegram call.  This keeps responsibilities separated between rendering logic
and transport policy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, Hashable, Optional, Tuple


logger = logging.getLogger(__name__)


UpdateCallable = Callable[[], Awaitable[Any]]


@dataclass
class MessageUpdate:
    """Container describing a scheduled update.

    Attributes
    ----------
    chat_id:
        Telegram chat where the update will be delivered.
    key:
        Logical identifier of the update inside the chat (e.g. ``"turn:42"``).
    state_signature:
        Hashable representation of the state that will be rendered.  Used to
        detect duplicates.  ``None`` can be used to disable diffing.
    coroutine_factory:
        Callable returning the coroutine that will be awaited when the update
        is flushed.
    description:
        Human readable string included in logs and useful when debugging.
    urgent:
        When ``True`` the scheduler will not inject the normal cooldown after
        sending this update.  Use for player facing events that must feel
        immediate (cards dealt, turn switches, …).
    fallback_result:
        Value returned to callers when the update is skipped because the state
        is already up to date.  For message edits this is typically the current
        ``message_id``.
    """

    chat_id: int
    key: str
    state_signature: Optional[Hashable]
    coroutine_factory: UpdateCallable
    description: str = ""
    urgent: bool = False
    fallback_result: Any = None


@dataclass
class _PendingUpdate:
    update: MessageUpdate
    future: "asyncio.Future[Any]"
    dirty: bool = False
    executing: bool = False
    urgent: bool = False


@dataclass
class _ChatQueue:
    pending: Dict[str, _PendingUpdate] = field(default_factory=dict)
    order: Deque[str] = field(default_factory=deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    worker: Optional[asyncio.Task] = None
    retry_at: float = 0.0


class UpdateScheduler:
    """Coalesce and rate-control outgoing Telegram updates per chat.

    Parameters
    ----------
    flush_interval:
        Delay (in seconds) inserted between two non-urgent updates in the same
        chat.  Values between 0.4–0.8 work well for card games: they stay well
        under the API limits but are still perceived as instant by the players.
    loop:
        Optional event loop.  When omitted ``asyncio.get_running_loop()`` is
        used lazily.
    """

    def __init__(self, *, flush_interval: float = 0.6, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._flush_interval = flush_interval
        self._loop = loop
        self._chat_queues: Dict[int, _ChatQueue] = {}
        self._last_sent: Dict[Tuple[int, str], Optional[Hashable]] = {}
        self._closed = False

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _chat_state(self, chat_id: int) -> _ChatQueue:
        if chat_id not in self._chat_queues:
            self._chat_queues[chat_id] = _ChatQueue()
        return self._chat_queues[chat_id]

    async def enqueue(self, update: MessageUpdate) -> Any:
        """Queue ``update`` and return the result once it has been flushed."""

        if self._closed:
            raise RuntimeError("UpdateScheduler is closed")

        chat_state = self._chat_state(update.chat_id)

        async with chat_state.lock:
            last_signature = self._last_sent.get((update.chat_id, update.key))
            if last_signature is not None and last_signature == update.state_signature:
                logger.debug(
                    "Skipping update %s in chat %s – state unchanged", update.key, update.chat_id
                )
                return update.fallback_result

            pending = chat_state.pending.get(update.key)
            if pending:
                pending.update = update
                pending.dirty = True
                pending.urgent = pending.urgent or update.urgent
                chat_state.event.set()
                logger.debug(
                    "Coalesced update %s in chat %s", update.key, update.chat_id
                )
                return await pending.future

            loop = self._get_loop()
            future: "asyncio.Future[Any]" = loop.create_future()
            chat_state.pending[update.key] = _PendingUpdate(
                update=update, future=future, dirty=True, urgent=update.urgent
            )
            chat_state.order.append(update.key)
            chat_state.event.set()

            if chat_state.worker is None or chat_state.worker.done():
                chat_state.worker = loop.create_task(self._run_chat_worker(update.chat_id, chat_state))

            logger.debug(
                "Scheduled new update %s in chat %s", update.key, update.chat_id
            )
            return await future

    async def _run_chat_worker(self, chat_id: int, chat_state: _ChatQueue) -> None:
        """Worker processing queue for a single chat."""

        while not self._closed:
            await chat_state.event.wait()

            while True:
                async with chat_state.lock:
                    now = time.monotonic()
                    if chat_state.retry_at > now:
                        delay = chat_state.retry_at - now
                    else:
                        delay = 0.0

                    if delay > 0:
                        chat_state.event.clear()

                    if not chat_state.order:
                        chat_state.event.clear()
                        break

                    if delay > 0:
                        # Exit inner loop and sleep outside the lock.
                        break

                    key = chat_state.order.popleft()
                    pending = chat_state.pending.get(key)
                    if not pending:
                        continue

                    pending.executing = True
                    update = pending.update
                    pending.dirty = False
                    urgent = pending.urgent or update.urgent
                    pending.urgent = False

                if delay > 0:
                    await asyncio.sleep(delay)
                    continue

                try:
                    result = await update.coroutine_factory()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pylint: disable=broad-except
                    retry_after = getattr(exc, "retry_after", None)
                    if retry_after:
                        logger.warning(
                            "RetryAfter received for update %s in chat %s", update.key, chat_id
                        )
                        await asyncio.sleep(float(retry_after))
                        async with chat_state.lock:
                            chat_state.retry_at = time.monotonic()
                            pending.executing = False
                            pending.dirty = True
                            if key not in chat_state.order:
                                chat_state.order.appendleft(key)
                            chat_state.event.set()
                        continue

                    logger.error(
                        "Update %s in chat %s failed: %s",
                        update.key,
                        chat_id,
                        exc,
                        exc_info=True,
                    )
                    if not pending.future.done():
                        pending.future.set_exception(exc)
                    async with chat_state.lock:
                        pending.executing = False
                        chat_state.pending.pop(key, None)
                    continue

                async with chat_state.lock:
                    pending.executing = False
                    self._last_sent[(chat_id, update.key)] = update.state_signature
                    if pending.dirty:
                        # A newer update arrived while we were executing.
                        if key not in chat_state.order:
                            chat_state.order.append(key)
                        pending.urgent = pending.urgent or urgent
                        chat_state.event.set()
                        continue

                    chat_state.pending.pop(key, None)

                if not pending.future.done():
                    pending.future.set_result(result if result is not None else update.fallback_result)

                if not urgent and self._flush_interval > 0:
                    await asyncio.sleep(self._flush_interval)

            # Allow worker to exit if queue is empty.
            async with chat_state.lock:
                if not chat_state.order:
                    chat_state.event.clear()
                    if not chat_state.pending:
                        chat_state.retry_at = 0.0
                        break

    async def close(self) -> None:
        """Cancel all workers and prevent new updates from being queued."""

        self._closed = True
        for chat_state in self._chat_queues.values():
            if chat_state.worker and not chat_state.worker.done():
                chat_state.worker.cancel()
                try:
                    await chat_state.worker
                except asyncio.CancelledError:
                    pass

