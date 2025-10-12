"""
Health check endpoints for monitoring subsystem status.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pokerapp.entities import Game
    from pokerapp.pokerbotmodel import PokerBotModel


class PruningHealthCheck:
    """Monitor pruning subsystem health."""

    def __init__(self, model: "PokerBotModel"):
        self._model = model
        self._last_prune_times: Dict[int, float] = {}
        self._last_prune_durations: Dict[int, float] = {}
        self._prune_error_count = 0
        self._prune_success_count = 0

    def record_prune(self, chat_id: int, success: bool, duration_ms: float) -> None:
        """Record pruning operation result."""

        if success:
            self._prune_success_count += 1
            self._last_prune_times[chat_id] = time.time()
            self._last_prune_durations[chat_id] = duration_ms
        else:
            self._prune_error_count += 1

    def get_health_status(self) -> Dict[str, Any]:
        """
        Return health status for monitoring.

        Returns:
            {
                "status": "healthy" | "degraded" | "unhealthy",
                "total_operations": int,
                "error_rate": float,
                "active_games": int,
                "stale_games": int,
                "last_check": ISO timestamp
            }
        """

        total_ops = self._prune_success_count + self._prune_error_count
        error_rate = (
            self._prune_error_count / total_ops if total_ops > 0 else 0.0
        )

        now = time.time()
        stale_threshold = 300  # 5 minutes
        stale_games = sum(
            1 for last_time in self._last_prune_times.values() if now - last_time > stale_threshold
        )

        if error_rate > 0.1:
            status = "unhealthy"
        elif error_rate > 0.05 or stale_games > 5:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            "total_operations": total_ops,
            "success_count": self._prune_success_count,
            "error_count": self._prune_error_count,
            "error_rate": round(error_rate, 4),
            "active_games": len(self._last_prune_times),
            "stale_games": stale_games,
            "last_check": datetime.utcnow().isoformat(),
        }

    async def check_prune_performance(self, chat_id: int) -> Dict[str, Any]:
        """
        Run a test prune operation and measure performance.

        Returns performance metrics for a single game.
        """

        try:
            load_result = await self._model._table_manager.load_game(chat_id)
            game: Optional["Game"] = None
            if isinstance(load_result, tuple):
                game = load_result[0]
            else:
                game = load_result

            if not game:
                return {"chat_id": chat_id, "status": "error", "error": "Game not found"}

            start = time.perf_counter()
            ready_players = await self._model._prune_ready_seats(game, chat_id)
            duration_ms = (time.perf_counter() - start) * 1000

            self.record_prune(chat_id, True, duration_ms)

            return {
                "chat_id": chat_id,
                "duration_ms": round(duration_ms, 2),
                "ready_count": len(ready_players),
                "player_count": len(game.players),
                "status": "success",
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            self.record_prune(chat_id, False, 0.0)
            return {
                "chat_id": chat_id,
                "status": "error",
                "error": str(exc),
            }
