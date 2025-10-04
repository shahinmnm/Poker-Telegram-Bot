"""Handlers for betting operations with strict lock ordering."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from pokerapp.entities import Game, PlayerState


class BettingHandler:
    """Coordinates wallet deductions and table updates for bets."""

    def __init__(self, *, lock_manager, table_manager, wallet_service, logger) -> None:
        self._lock_manager = lock_manager
        self._table_manager = table_manager
        self._wallet_service = wallet_service
        self._logger = logger

    async def handle_bet(self, chat_id: int, user_id: int, amount: int) -> bool:
        """Process a bet ensuring wallet→table lock ordering."""

        if amount is None or amount <= 0:
            raise ValueError("Bet amount must be positive")

        await self._deduct_from_wallet(user_id, amount)

        try:
            async with self._lock_manager.acquire_table_write_lock(chat_id):
                game, version = await self._load_game_with_version(chat_id)
                if game is None:
                    raise RuntimeError("No active game found for bet")

                player = self._find_player(game, user_id)
                if player is None:
                    raise RuntimeError("Player not part of the game")

                self._apply_bet(game, player, amount)
                await self._persist_game(chat_id, game, version)
        except Exception:
            # Attempt to refund the wallet before bubbling the error.
            try:
                await self._wallet_service.credit_chips(user_id, amount)
            except Exception:
                self._logger.warning(
                    "Failed to refund chips after bet failure",
                    extra={"chat_id": chat_id, "user_id": user_id, "amount": amount},
                    exc_info=True,
                )
            raise

        return True

    async def _deduct_from_wallet(self, user_id: int, amount: int) -> None:
        async with self._lock_manager.acquire_wallet_lock(user_id):
            await self._wallet_service.deduct_chips(user_id, amount)

    async def _load_game_with_version(self, chat_id: int) -> Tuple[Optional[Game], Optional[Any]]:
        snapshot = await self._table_manager.load_game_with_version(chat_id)
        if isinstance(snapshot, tuple) and len(snapshot) == 2:
            return snapshot[0], snapshot[1]
        return snapshot, None  # type: ignore[return-value]

    async def _persist_game(self, chat_id: int, game: Game, version: Optional[Any]) -> None:
        if version is not None and hasattr(self._table_manager, "save_game_with_version_check"):
            saved = await self._table_manager.save_game_with_version_check(
                chat_id, game, version
            )
            if not saved:
                raise RuntimeError("Concurrent update detected for game state")
            return
        if hasattr(self._table_manager, "save_game"):
            await self._table_manager.save_game(chat_id, game)

    def _find_player(self, game: Game, user_id: int):
        for player in game.players:
            if getattr(player, "user_id", None) == user_id:
                return player
        return None

    def _apply_bet(self, game: Game, player, amount: int) -> None:
        player.round_rate = int(getattr(player, "round_rate", 0)) + int(amount)
        player.total_bet = int(getattr(player, "total_bet", 0)) + int(amount)
        if getattr(player, "state", PlayerState.ACTIVE) == PlayerState.ACTIVE:
            player.has_acted = True
        game.pot = int(getattr(game, "pot", 0)) + int(amount)
        game.max_round_rate = max(
            int(getattr(game, "max_round_rate", 0)), int(getattr(player, "round_rate", 0))
        )
