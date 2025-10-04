"""Lightweight runtime statistics helpers with read-lock support."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pokerapp.entities import ChatId, Game, Player, UserId
from pokerapp.lock_manager import LockManager
from pokerapp.table_manager import TableManager

__all__ = ["StatsService"]


class StatsService:
    """Provide table snapshots for quick player statistics queries."""

    def __init__(
        self,
        *,
        table_manager: TableManager,
        lock_manager: Optional[LockManager],
        logger: logging.Logger,
    ) -> None:
        self._table_manager = table_manager
        self._lock_manager = lock_manager
        self._logger = logger

    async def get_player_stats(
        self,
        player_id: UserId,
        chat_id: ChatId,
    ) -> Dict[str, Any]:
        """Return a snapshot of ``player_id`` statistics for ``chat_id``."""

        lock_manager = self._lock_manager
        chat_key = self._safe_int(chat_id)

        if lock_manager is None:
            game = await self._load_game(chat_id)
            player = self._find_player(game, player_id)
            if player is None:
                self._log_player_missing(chat_id, player_id)
                return {}
            snapshot = self._snapshot_player(player)
        else:
            async with lock_manager.table_read_lock(chat_key):
                game = await self._load_game(chat_id)
                player = self._find_player(game, player_id)
                if player is None:
                    self._log_player_missing(chat_id, player_id)
                    return {}
                snapshot = self._snapshot_player(player)

        return self._build_stats(snapshot, player_id, chat_id)

    async def _load_game(self, chat_id: ChatId) -> Game:
        get_game = getattr(self._table_manager, "get_game", None)
        if callable(get_game):
            return await get_game(chat_id)

        loaded = await self._table_manager.load_game_with_version(chat_id)
        if isinstance(loaded, tuple):
            return loaded[0]
        return loaded

    def _find_player(self, game: Game, player_id: UserId) -> Optional[Player]:
        if game is None:
            return None

        try:
            player_int = int(player_id)
        except (TypeError, ValueError):
            player_int = player_id  # type: ignore[assignment]

        for player in getattr(game, "players", []):
            if getattr(player, "user_id", None) == player_int:
                return player
        return None

    @staticmethod
    def _snapshot_player(player: Player) -> Dict[str, Any]:
        return {
            "round_rate": int(getattr(player, "round_rate", 0)),
            "total_bet": int(getattr(player, "total_bet", 0)),
            "state": getattr(getattr(player, "state", None), "name", "UNKNOWN"),
            "cards": list(getattr(player, "cards", []) or []),
            "has_acted": bool(getattr(player, "has_acted", False)),
        }

    def _build_stats(
        self,
        snapshot: Dict[str, Any],
        player_id: UserId,
        chat_id: ChatId,
    ) -> Dict[str, Any]:
        """Hook for augmenting snapshots outside of table locks."""

        return dict(snapshot)

    def _log_player_missing(self, chat_id: ChatId, player_id: UserId) -> None:
        self._logger.warning(
            "Player not found for stats request",
            extra={
                "chat_id": self._safe_int(chat_id),
                "player_id": self._safe_int(player_id),
            },
        )

    def _safe_int(self, value: object) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
