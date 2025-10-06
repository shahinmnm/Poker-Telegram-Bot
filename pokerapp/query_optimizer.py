"""Query batching utilities for reducing database round trips."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .db_client import OptimizedDatabaseClient


@dataclass(slots=True)
class BatchQuery:
    """Represents a queued request for player data."""

    user_id: int
    include_stats: bool
    include_wallet: bool
    include_history: bool
    future: asyncio.Future


class QueryBatcher:
    """Batcher that merges related player data queries into bulk operations."""

    def __init__(
        self,
        db_client: OptimizedDatabaseClient,
        *,
        batch_window_ms: int = 50,
        logger: Optional[Any] = None,
    ) -> None:
        self.db = db_client
        self.logger = logger
        self.batch_window_ms = batch_window_ms
        self._pending_batches: Dict[str, List[BatchQuery]] = defaultdict(list)
        self._batch_tasks: Dict[str, asyncio.Task] = {}
        self._metrics = {
            "queries_batched": 0,
            "queries_saved": 0,
            "batch_count": 0,
        }

    async def get_player_data(
        self,
        user_id: int,
        *,
        include_stats: bool = True,
        include_wallet: bool = True,
        include_history: bool = False,
    ) -> Dict[str, Any]:
        """Queue a player data request for batching."""

        key = self._batch_key(include_stats, include_wallet, include_history)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        batch = BatchQuery(
            user_id=user_id,
            include_stats=include_stats,
            include_wallet=include_wallet,
            include_history=include_history,
            future=future,
        )
        self._pending_batches[key].append(batch)
        self._metrics["queries_batched"] += 1

        if key not in self._batch_tasks:
            self._batch_tasks[key] = asyncio.create_task(self._flush_after_window(key))

        return await future

    async def batch_get_player_data(self, user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Fetch player data for a list of users immediately."""

        if not user_ids:
            return {}

        data = await self._fetch_player_snapshot(user_ids)
        self._metrics["batch_count"] += 1
        self._metrics["queries_saved"] += max(len(user_ids) - 1, 0)
        return data

    def get_metrics(self) -> Dict[str, int]:
        """Expose batching performance counters."""

        return dict(self._metrics)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _flush_after_window(self, key: str) -> None:
        try:
            await asyncio.sleep(self.batch_window_ms / 1000)
            batch = self._pending_batches.pop(key, [])
            if not batch:
                return

            user_ids = [item.user_id for item in batch]
            include_history = batch[0].include_history
            results = await self._fetch_player_snapshot(user_ids)
            self._metrics["batch_count"] += 1
            self._metrics["queries_saved"] += max(len(batch) - 1, 0)

            history_map: Dict[int, List[Dict[str, Any]]] = {}
            if include_history:
                history_map = await self._fetch_histories(user_ids)

            for item in batch:
                payload = dict(results.get(item.user_id, {"user_id": item.user_id}))
                if not item.include_stats:
                    payload.pop("stats", None)
                if not item.include_wallet:
                    payload.pop("wallet", None)
                if item.include_history and history_map:
                    payload["history"] = history_map.get(item.user_id, [])
                if item.future.done():
                    continue
                item.future.set_result(payload)
        except Exception as exc:  # pragma: no cover - defensive logging
            if self.logger:
                self.logger.exception("query_batch.flush_failed")
            for item in self._pending_batches.pop(key, []):
                if not item.future.done():
                    item.future.set_exception(exc)
        finally:
            self._batch_tasks.pop(key, None)

    async def _fetch_player_snapshot(self, user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        query = """
            SELECT
                s.user_id,
                s.games_played,
                s.games_won,
                s.total_profit,
                s.win_streak,
                w.balance,
                w.last_bonus_time
            FROM player_stats s
            LEFT JOIN wallets w ON s.user_id = w.user_id
            WHERE s.user_id = ANY($1)
        """

        rows = await self.db.fetch(query, user_ids)
        data: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            entry: Dict[str, Any] = {"user_id": row["user_id"]}
            entry["stats"] = {
                "games_played": row.get("games_played"),
                "games_won": row.get("games_won"),
                "total_profit": row.get("total_profit"),
                "win_streak": row.get("win_streak"),
            }
            entry["wallet"] = {
                "balance": row.get("balance"),
                "last_bonus_time": row.get("last_bonus_time"),
            }
            data[row["user_id"]] = entry
        return data

    async def _fetch_histories(self, user_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        query = """
            SELECT *
            FROM (
                SELECT
                    gh.*,
                    ROW_NUMBER() OVER (PARTITION BY gh.user_id ORDER BY gh.timestamp DESC) AS row_num
                FROM game_history gh
                WHERE gh.user_id = ANY($1)
            ) ranked
            WHERE ranked.row_num <= 10
            ORDER BY ranked.user_id, ranked.timestamp DESC
        """
        rows = await self.db.fetch(query, user_ids)
        history: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            entry = dict(row)
            entry.pop("row_num", None)
            history[row["user_id"]].append(entry)
        return history

    def _batch_key(self, include_stats: bool, include_wallet: bool, include_history: bool) -> str:
        return f"stats={include_stats}|wallet={include_wallet}|history={include_history}"


__all__ = ["QueryBatcher"]
