"""
Background jobs for maintenance tasks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pokerapp.pokerbotmodel import PokerBotModel


class StaleUserCleanupJob:
    """Periodic cleanup of stale ready markers across all games."""

    def __init__(self, model: "PokerBotModel", interval_seconds: int = 300):
        self._model = model
        self._interval = interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the cleanup job."""

        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        self._model._logger.info(
            "Started stale user cleanup job",
            extra={"interval_seconds": self._interval},
        )

    async def stop(self) -> None:
        """Stop the cleanup job gracefully."""

        self._running = False
        task = self._task
        if task is None:
            self._model._logger.info("Stopped stale user cleanup job")
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._model._logger.info("Stopped stale user cleanup job")

    async def _cleanup_loop(self) -> None:
        """Main cleanup loop."""

        while self._running:
            try:
                await self._run_cleanup_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._model._logger.error(
                    "Cleanup cycle failed",
                    extra={"error": str(exc)},
                    exc_info=True,
                )

            if not self._running:
                break

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _run_cleanup_cycle(self) -> None:
        """Run a single cleanup cycle across all active games."""

        start_time = datetime.utcnow()

        active_games = await self._model._table_manager.get_active_game_ids()

        total_pruned = 0
        games_affected = 0

        for chat_id in active_games:
            try:
                load_result = await self._model._table_manager.load_game(chat_id)
                game = load_result[0] if isinstance(load_result, tuple) else load_result
                if not game:
                    continue

                ready_users = getattr(game, "ready_users", set())
                before_count = len(ready_users)
                ready_players = await self._model._prune_ready_seats(game, chat_id)
                after_count = len(ready_players)

                if before_count > after_count:
                    games_affected += 1
                    total_pruned += before_count - after_count
                    await self._model._table_manager.save_game(chat_id, game)

            except Exception as exc:  # pragma: no cover - defensive logging
                self._model._logger.warning(
                    "Failed to prune game",
                    extra={"chat_id": chat_id, "error": str(exc)},
                )

        duration = (datetime.utcnow() - start_time).total_seconds()

        self._model._logger.info(
            "Cleanup cycle completed",
            extra={
                "duration_seconds": round(duration, 2),
                "games_scanned": len(active_games),
                "games_affected": games_affected,
                "total_pruned": total_pruned,
                "avg_prune_per_game": round(
                    total_pruned / max(games_affected, 1), 2
                ),
            },
        )
