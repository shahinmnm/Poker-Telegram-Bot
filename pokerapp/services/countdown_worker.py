"""Countdown worker responsible for processing countdown messages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from telegram.error import TelegramError

from pokerapp.services.countdown_queue import CountdownMessage, CountdownMessageQueue
from pokerapp.utils.telegram_safeops import TelegramSafeOps


class CountdownWorker:
    """Background worker that updates countdown messages in Telegram."""

    def __init__(
        self,
        queue: CountdownMessageQueue,
        safe_ops: TelegramSafeOps,
        edit_interval: float = 1.0,
    ) -> None:
        self._queue = queue
        self._safe_ops = safe_ops
        self._edit_interval = max(edit_interval, 0.0)
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._shutdown_event = asyncio.Event()
        self._logger = logging.getLogger(__name__)

    async def start(self) -> None:
        """Start the countdown processing worker."""

        if self._worker_task and not self._worker_task.done():
            return

        if self._shutdown_event.is_set():
            self._shutdown_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        self._worker_task = loop.create_task(self._worker_loop(), name="countdown-worker")
        self._logger.info("Countdown worker started")

    async def stop(self) -> None:
        """Stop the countdown worker gracefully."""

        self._shutdown_event.set()

        if self._worker_task is None:
            return

        task = self._worker_task
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                self._logger.warning("Countdown worker stop timed out")
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                self._logger.exception("Error while stopping countdown worker")
        self._worker_task = None
        self._logger.info("Countdown worker stopped")

    async def _worker_loop(self) -> None:
        """Continuously consume messages from the queue until shutdown."""

        try:
            while not self._shutdown_event.is_set():
                try:
                    msg = await asyncio.wait_for(self._queue.dequeue(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    continue
                try:
                    await self._process_countdown(msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger.exception(
                        "Unexpected error processing countdown",
                        extra={
                            "chat_id": getattr(msg, "chat_id", None),
                            "message_id": getattr(msg, "message_id", None),
                        },
                    )
        except asyncio.CancelledError:
            self._logger.debug("Countdown worker loop cancelled")
            raise

    async def _process_countdown(self, msg: CountdownMessage) -> None:
        """Process a single countdown message."""

        if msg.cancelled:
            self._logger.debug(
                "Countdown message cancelled before processing",
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
            return

        remaining = float(getattr(msg, "duration_seconds", 0.0))
        if remaining <= 0:
            await self._invoke_completion(msg)
            self._logger.debug(
                "Countdown completed instantly",
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
            return

        loop = asyncio.get_running_loop()
        end_time = loop.time() + remaining
        last_edit = 0.0

        self._logger.debug(
            "Starting countdown",
            extra={"chat_id": msg.chat_id, "message_id": msg.message_id, "remaining": remaining},
        )

        while not msg.cancelled and not self._shutdown_event.is_set():
            now = loop.time()
            remaining = max(0.0, end_time - now)
            if remaining <= 0:
                break

            if now - last_edit >= self._edit_interval:
                try:
                    await self._safe_ops.edit_message_text(
                        chat_id=msg.chat_id,
                        message_id=msg.message_id,
                        text=self._format_message_text(msg, remaining),
                        from_countdown=True,
                    )
                except TelegramError:
                    self._logger.exception(
                        "TelegramError while editing countdown message",
                        extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                    )
                    return
                last_edit = now
            sleep_for = min(0.1, remaining)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                raise

        if msg.cancelled:
            self._logger.debug(
                "Countdown cancelled",
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
            return

        if self._shutdown_event.is_set():
            self._logger.debug(
                "Countdown interrupted by shutdown",
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
            return

        await self._invoke_completion(msg)
        self._logger.debug(
            "Countdown completed",
            extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
        )

    async def _invoke_completion(self, msg: CountdownMessage) -> None:
        """Invoke the ``on_complete`` callback if present."""

        callback = getattr(msg, "on_complete", None)
        if not callable(callback):
            return
        try:
            result: Any = callback()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # pragma: no cover - defensive
            self._logger.exception(
                "Error running countdown completion callback",
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )

    def _format_message_text(self, msg: CountdownMessage, remaining: float) -> str:
        formatter = getattr(msg, "format_text", None)
        if callable(formatter):
            try:
                return formatter(remaining)
            except Exception:  # pragma: no cover - defensive
                self._logger.exception(
                    "Error formatting countdown text",
                    extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
        return getattr(msg, "text", str(int(remaining)))

__all__ = ["CountdownWorker"]
