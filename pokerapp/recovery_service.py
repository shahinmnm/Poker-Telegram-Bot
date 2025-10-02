"""Startup recovery helpers for persisted poker games."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from redis.asyncio import Redis

from pokerapp.entities import ChatId
from pokerapp.services.countdown_queue import CountdownMessageQueue
from pokerapp.table_manager import TableManager

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from pokerapp.lock_manager import LockManager

class RecoveryService:
    """Coordinate recovery of persisted state after a restart."""

    def __init__(
        self,
        *,
        redis: Redis,
        table_manager: TableManager,
        lock_manager: Optional["LockManager"] = None,
        countdown_queue: Optional[CountdownMessageQueue] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._redis = redis
        self._table_manager = table_manager
        self._lock_manager = lock_manager
        self._countdown_queue = countdown_queue
        self._logger = logger or logging.getLogger(__name__)

    async def run_startup_recovery(self) -> Dict[str, Any]:
        """Execute the startup recovery workflow and return summary stats."""

        stats: Dict[str, Any] = {
            "games_scanned": 0,
            "games_recovered": 0,
            "games_deleted": 0,
            "locks_cleared": 0,
            "countdowns_cleared": 0,
        }

        self._logger.info(
            "Starting bot recovery sequence",
            extra={"event_type": "startup_recovery_start"},
        )

        try:
            game_stats = await self._recover_all_games()
        except Exception:
            self._logger.exception("Failed to recover games during startup")
        else:
            stats.update(game_stats)

        try:
            stats["locks_cleared"] = await self._clear_orphaned_locks()
        except Exception:
            self._logger.exception("Failed to clear orphaned locks")

        try:
            stats["countdowns_cleared"] = await self._clear_countdown_queue()
        except Exception:
            self._logger.exception("Failed to clear countdown queue")

        stats_with_event = {**stats, "event_type": "startup_recovery_complete"}
        self._logger.info("Startup recovery completed", extra=stats_with_event)
        return stats

    async def _recover_all_games(self) -> Dict[str, int]:
        stats = {"games_scanned": 0, "games_recovered": 0, "games_deleted": 0}
        pattern = self._table_manager._game_key("*")

        async for raw_key in self._redis.scan_iter(match=pattern):
            stats["games_scanned"] += 1
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            chat_id = self._extract_chat_id(key)
            if chat_id is None:
                continue

            try:
                game, validation = await self._table_manager.load_game(
                    chat_id, validate=True
                )
            except Exception:
                self._logger.exception(
                    "Failed to load game during recovery", extra={"chat_id": chat_id}
                )
                await self._safe_delete(raw_key)
                stats["games_deleted"] += 1
                continue

            if game is None:
                stats["games_deleted"] += 1
                continue

            if validation and not validation.is_valid:
                if validation.recoverable:
                    stats["games_recovered"] += 1
                else:
                    stats["games_deleted"] += 1

        return stats

    async def _clear_orphaned_locks(self) -> int:
        if self._lock_manager is None:
            return 0

        cleared = await self._lock_manager.clear_all_locks()
        self._logger.info(
            "Cleared orphaned locks",
            extra={"locks_cleared": cleared, "event_type": "locks_cleared"},
        )
        return cleared

    async def _clear_countdown_queue(self) -> int:
        if self._countdown_queue is None:
            return 0

        cleared = await self._countdown_queue.clear_all()
        self._logger.info(
            "Cleared countdown queue",
            extra={
                "countdowns_cleared": cleared,
                "event_type": "countdown_queue_cleared",
            },
        )
        return cleared

    def _extract_chat_id(self, key: str) -> Optional[Union[ChatId, int]]:
        parts = key.split(":")
        if len(parts) < 3:
            return None
        raw_id = parts[1]
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return raw_id

    async def _safe_delete(self, key: Union[str, bytes]) -> None:
        try:
            await self._redis.delete(key)
        except Exception:
            key_value = key.decode() if isinstance(key, bytes) else str(key)
            self._logger.exception(
                "Failed to delete corrupted key", extra={"key": key_value}
            )


__all__ = ["RecoveryService"]
