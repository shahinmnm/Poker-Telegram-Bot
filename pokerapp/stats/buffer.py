"""Statistics batching buffer infrastructure.

This module defines :class:`StatsBatchBuffer`, which provides an asynchronous,
thread-safe buffer for batching player statistics writes. It handles automatic
flushing when the buffer reaches a configured threshold, periodic flushing from
background tasks, retry logic with exponential backoff, and graceful shutdown
behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

SessionMaker = Any
try:
    from sqlalchemy.ext.asyncio import async_sessionmaker
except Exception:  # pragma: no cover - optional dependency
    async_sessionmaker = None  # type: ignore[assignment]
else:  # pragma: no cover - requires SQLAlchemy
    SessionMaker = async_sessionmaker[Any]

logger = logging.getLogger(__name__)


FlushCallback = Callable[[List[Dict[str, Any]]], Awaitable[None]]


class StatsBatchBuffer:
    """Thread-safe batching buffer for statistics records.

    Args:
        session_maker: SQLAlchemy session factory (reserved for future use).
        flush_callback: Async callable invoked with buffered records when the
            buffer flushes.
        config: Configuration dictionary sourced from
            ``config/system_constants.json``.
    """

    def __init__(
        self,
        session_maker: SessionMaker,
        flush_callback: FlushCallback,
        config: Dict[str, Any],
    ) -> None:
        self._session_maker = session_maker
        self._flush_callback = flush_callback
        self._buffer: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

        buffer_cfg = config.get("stats_batch_buffer", {})
        self._max_size: int = int(buffer_cfg.get("max_size", 100))
        self._flush_interval_seconds: float = float(
            buffer_cfg.get("flush_interval_seconds", 5)
        )
        self._flush_on_shutdown: bool = bool(buffer_cfg.get("flush_on_shutdown", True))
        self._metrics_enabled: bool = bool(buffer_cfg.get("enable_metrics", True))
        self._max_retries: int = int(buffer_cfg.get("max_retries", 3))
        self._retry_backoff_base: float = float(buffer_cfg.get("retry_backoff_base", 2))

        self._background_task: Optional[asyncio.Task[None]] = None

        self.metrics: Dict[str, Any] = {
            "total_records_added": 0,
            "total_records_flushed": 0,
            "total_flushes": 0,
            "failed_flushes": 0,
            "avg_batch_size": 0.0,
            "average_batch_size": 0.0,
            "max_buffer_size_reached": 0,
            "last_flush_timestamp": None,
            "last_flush_duration_ms": 0.0,
        }

    async def add(self, records: Sequence[Dict[str, Any]]) -> None:
        """Add records to the buffer and flush if necessary."""

        if not records:
            return

        records_to_add = list(records)
        async with self._lock:
            self._buffer.extend(records_to_add)
            current_size = len(self._buffer)
            if self._metrics_enabled:
                self.metrics["total_records_added"] += len(records_to_add)
                if current_size > self.metrics["max_buffer_size_reached"]:
                    self.metrics["max_buffer_size_reached"] = current_size

            logger.debug("Added %s records to stats buffer (size=%s)", len(records_to_add), current_size)

            should_flush = current_size >= self._max_size

        if should_flush:
            try:
                await self._flush()
            except Exception:  # pragma: no cover - defensive logging
                logger.warning("Automatic flush triggered by threshold failed", exc_info=True)

    async def _flush(self) -> None:
        """Flush buffered records via the configured callback."""

        async with self._lock:
            if not self._buffer:
                return

            records_to_flush = list(self._buffer)
            self._buffer.clear()

        start_time = time.perf_counter()
        attempt = 1
        success = False
        error: Optional[BaseException] = None
        max_attempts = max(0, self._max_retries) + 1

        rebuffered = False

        while attempt <= max_attempts:
            try:
                await self._flush_callback(records_to_flush)
                success = True
                break
            except asyncio.CancelledError:
                if not rebuffered:
                    async with self._lock:
                        self._buffer = records_to_flush + self._buffer
                        if self._metrics_enabled:
                            self.metrics["max_buffer_size_reached"] = max(
                                self.metrics["max_buffer_size_reached"], len(self._buffer)
                            )
                    rebuffered = True
                raise
            except Exception as exc:  # pragma: no cover - logging branch
                error = exc
                if attempt >= max_attempts:
                    break
                logger.warning(
                    "Flush attempt %s/%s failed; retrying",
                    attempt,
                    max_attempts,
                    exc_info=True,
                )
                backoff_seconds = self._retry_backoff_base ** (attempt - 1)
                attempt += 1
                try:
                    await asyncio.sleep(backoff_seconds)
                except asyncio.CancelledError:
                    if not rebuffered:
                        async with self._lock:
                            self._buffer = records_to_flush + self._buffer
                            if self._metrics_enabled:
                                self.metrics["max_buffer_size_reached"] = max(
                                    self.metrics["max_buffer_size_reached"], len(self._buffer)
                                )
                        rebuffered = True
                    raise

        if not success:
            if not rebuffered:
                async with self._lock:
                    # Prepend failed batch to ensure it is flushed before newer records.
                    self._buffer = records_to_flush + self._buffer
                    if self._metrics_enabled:
                        self.metrics["max_buffer_size_reached"] = max(
                            self.metrics["max_buffer_size_reached"], len(self._buffer)
                        )
            if self._metrics_enabled:
                self.metrics["failed_flushes"] += 1
            if error:
                logger.warning("Flush ultimately failed after retries", exc_info=error)
            return

        duration_ms = (time.perf_counter() - start_time) * 1000
        if self._metrics_enabled:
            batch_size = len(records_to_flush)
            self.metrics["total_flushes"] += 1
            self.metrics["total_records_flushed"] += batch_size
            self.metrics["last_flush_timestamp"] = time.time()
            self.metrics["last_flush_duration_ms"] = duration_ms
            total_flushes = self.metrics["total_flushes"]
            if total_flushes:
                average = self.metrics["total_records_flushed"] / total_flushes
            else:
                average = 0.0
            self.metrics["avg_batch_size"] = average
            self.metrics["average_batch_size"] = average

        logger.info(
            "Flushed %s statistics records in %.2f ms", len(records_to_flush), duration_ms
        )

    async def start_background_flusher(self) -> None:
        """Start the periodic background flush task."""

        if self._background_task and not self._background_task.done():
            return

        if self._flush_interval_seconds <= 0:
            logger.debug("Background flusher not started due to non-positive interval")
            return

        async def _run() -> None:
            try:
                while True:
                    await asyncio.sleep(self._flush_interval_seconds)

                    async with self._lock:
                        buffer_size = len(self._buffer)

                    if buffer_size > 0:
                        logger.debug(
                            "Background flusher tick (buffer_size=%s)", buffer_size
                        )
                    try:
                        await self._flush()
                    except Exception:  # pragma: no cover - defensive logging
                        logger.warning("Background flush failed", exc_info=True)
            except asyncio.CancelledError:
                logger.debug("Background flusher task cancelled")
                raise

        self._background_task = asyncio.create_task(_run(), name="stats-buffer-flusher")

    async def shutdown(self) -> None:
        """Gracefully shut down the buffer, ensuring remaining records flush."""

        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
            finally:
                self._background_task = None

        if self._flush_on_shutdown:
            try:
                await self._flush()
            except Exception:  # pragma: no cover - defensive logging
                logger.warning("Flush during shutdown failed", exc_info=True)

        if self._metrics_enabled:
            total_flushed = self.metrics["total_records_flushed"]
            total_flushes = self.metrics["total_flushes"] or 1
            avg_batch = total_flushed / total_flushes
            self.metrics["avg_batch_size"] = avg_batch
            self.metrics["average_batch_size"] = avg_batch
        else:
            total_flushed = 0
            avg_batch = 0.0

        logger.info(
            "StatsBatchBuffer shutdown complete: total_flushed=%s avg_batch_size=%.2f",
            total_flushed,
            avg_batch,
        )
