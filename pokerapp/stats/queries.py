"""High-performance query interface for materialized player statistics.

This module provides optimized queries against the ``player_stats`` table,
leveraging indexes and avoiding N+1 query patterns.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlayerStatsSnapshot:
    """Immutable snapshot of player statistics."""

    user_id: int
    username: Optional[str]
    total_hands: int
    hands_won: int
    hands_lost: int
    total_winnings: int
    total_buyins: int
    biggest_win: int
    biggest_loss: int
    current_streak: int
    best_streak: int
    worst_streak: int
    last_played_at: Optional[datetime]

    @property
    def win_rate(self) -> float:
        """Calculate win rate as percentage (0-100)."""

        if self.total_hands == 0:
            return 0.0
        return (self.hands_won / self.total_hands) * 100

    @property
    def net_profit(self) -> int:
        """Calculate net profit (winnings - buyins)."""

        return self.total_winnings - self.total_buyins

    @property
    def roi(self) -> float:
        """Calculate return on investment as percentage."""

        if self.total_buyins == 0:
            return 0.0
        return (self.net_profit / self.total_buyins) * 100


class PlayerStatsQuery:
    """Query builder for player statistics with performance optimizations."""

    def __init__(self, db_connection: sqlite3.Connection) -> None:
        """Initialize query builder with a SQLite connection."""

        self.conn = db_connection
        self._previous_row_factory = getattr(self.conn, "row_factory", None)
        self.conn.row_factory = self._dict_factory

    def close(self) -> None:
        """Restore the original row factory if it was set."""

        if self._previous_row_factory is not None:
            self.conn.row_factory = self._previous_row_factory

    @staticmethod
    def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> Dict[str, object]:
        """Convert SQLite rows to dictionaries."""

        return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

    def get_player_stats(self, user_id: int) -> Optional[PlayerStatsSnapshot]:
        """Retrieve stats for a single player."""

        start = perf_counter()
        cursor = self.conn.execute(
            """
            SELECT
                user_id, username, total_hands, hands_won, hands_lost,
                total_winnings, total_buyins, biggest_win, biggest_loss,
                current_streak, best_streak, worst_streak, last_played_at
            FROM player_stats
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        duration_ms = (perf_counter() - start) * 1000
        logger.debug(
            "Fetched player stats",
            extra={
                "event_type": "player_stats_query",
                "user_id": user_id,
                "duration_ms": round(duration_ms, 3),
            },
        )

        if not row:
            return None

        return self._row_to_snapshot(row)

    def get_leaderboard(
        self,
        order_by: str = "total_winnings",
        limit: int = 10,
        offset: int = 0,
        min_hands: int = 0,
    ) -> List[PlayerStatsSnapshot]:
        """Retrieve leaderboard rankings."""

        valid_orders = {
            "total_winnings": "total_winnings DESC",
            "hands_won": "hands_won DESC",
            "win_rate": "CAST(hands_won AS REAL) / NULLIF(total_hands, 0) DESC",
        }
        order_clause = valid_orders.get(order_by, "total_winnings DESC")

        start = perf_counter()
        cursor = self.conn.execute(
            f"""
            SELECT
                user_id, username, total_hands, hands_won, hands_lost,
                total_winnings, total_buyins, biggest_win, biggest_loss,
                current_streak, best_streak, worst_streak, last_played_at
            FROM player_stats
            WHERE total_hands >= ?
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            (min_hands, limit, offset),
        )
        rows = cursor.fetchall()
        duration_ms = (perf_counter() - start) * 1000
        logger.debug(
            "Fetched leaderboard stats",
            extra={
                "event_type": "player_stats_leaderboard_query",
                "order_by": order_clause,
                "limit": limit,
                "offset": offset,
                "min_hands": min_hands,
                "result_count": len(rows),
                "duration_ms": round(duration_ms, 3),
            },
        )
        return [self._row_to_snapshot(row) for row in rows]

    def get_recent_players(
        self,
        hours: int = 24,
        limit: int = 20,
    ) -> List[PlayerStatsSnapshot]:
        """Retrieve players who played within the specified time window."""

        cutoff = datetime.utcnow() - timedelta(hours=hours)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

        start = perf_counter()
        cursor = self.conn.execute(
            """
            SELECT
                user_id, username, total_hands, hands_won, hands_lost,
                total_winnings, total_buyins, biggest_win, biggest_loss,
                current_streak, best_streak, worst_streak, last_played_at
            FROM player_stats
            WHERE last_played_at >= ?
            ORDER BY last_played_at DESC
            LIMIT ?
            """,
            (cutoff_str, limit),
        )
        rows = cursor.fetchall()
        duration_ms = (perf_counter() - start) * 1000
        logger.debug(
            "Fetched recent players",
            extra={
                "event_type": "player_stats_recent_query",
                "hours": hours,
                "limit": limit,
                "result_count": len(rows),
                "duration_ms": round(duration_ms, 3),
            },
        )
        return [self._row_to_snapshot(row) for row in rows]

    def get_total_stats(self) -> Dict[str, Optional[int]]:
        """Retrieve aggregate statistics across all players."""

        start = perf_counter()
        cursor = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_players,
                SUM(total_hands) AS total_hands_played,
                SUM(total_winnings) AS total_winnings,
                MAX(biggest_win) AS biggest_win_ever,
                MIN(biggest_loss) AS biggest_loss_ever
            FROM player_stats
            """
        )
        result = cursor.fetchone()
        duration_ms = (perf_counter() - start) * 1000
        logger.debug(
            "Fetched aggregate stats",
            extra={
                "event_type": "player_stats_aggregate_query",
                "duration_ms": round(duration_ms, 3),
            },
        )
        return result

    @staticmethod
    def _row_to_snapshot(row: Dict[str, object]) -> PlayerStatsSnapshot:
        """Convert database row to :class:`PlayerStatsSnapshot`."""

        last_played = row.get("last_played_at")
        last_played_dt: Optional[datetime]
        if isinstance(last_played, datetime):
            last_played_dt = last_played
        elif isinstance(last_played, str) and last_played:
            last_played_dt = datetime.fromisoformat(last_played)
        else:
            last_played_dt = None

        return PlayerStatsSnapshot(
            user_id=int(row["user_id"]),
            username=row.get("username"),
            total_hands=int(row["total_hands"]),
            hands_won=int(row["hands_won"]),
            hands_lost=int(row["hands_lost"]),
            total_winnings=int(row["total_winnings"]),
            total_buyins=int(row["total_buyins"]),
            biggest_win=int(row["biggest_win"]),
            biggest_loss=int(row["biggest_loss"]),
            current_streak=int(row["current_streak"]),
            best_streak=int(row["best_streak"]),
            worst_streak=int(row["worst_streak"]),
            last_played_at=last_played_dt,
        )


__all__ = ["PlayerStatsSnapshot", "PlayerStatsQuery"]
