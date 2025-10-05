from __future__ import annotations

"""
Betting Handler - Atomic Betting with Two-Phase Commit
Handles all betting actions (fold, check, call, raise) with guaranteed atomicity.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pokerapp.wallet_service import WalletService
from pokerapp.game_engine import GameEngine
from pokerapp.lock_manager import LockManager

logger = logging.getLogger(__name__)


@dataclass
class BettingResult:
    """Result of a betting action."""

    success: bool
    message: str
    new_state: Optional[Dict[str, Any]] = None
    reservation_id: Optional[str] = None


class BettingHandler:
    """Handles betting actions with a Two-Phase Commit protocol."""

    def __init__(
        self,
        wallet_service: WalletService,
        game_engine: GameEngine,
        lock_manager: LockManager,
    ) -> None:
        self.wallet = wallet_service
        self.engine = game_engine
        self.locks = lock_manager

    async def handle_betting_action(
        self,
        user_id: int,
        chat_id: int,
        action: str,
        amount: Optional[int] = None,
    ) -> BettingResult:
        """Process a betting action with full atomicity guarantee."""

        reservation_id: Optional[str] = None
        committed_reservation_id: Optional[str] = None
        committed_amount = 0

        try:
            validation_result = await self._validate_action(
                user_id, chat_id, action, amount
            )
            if not validation_result["valid"]:
                return BettingResult(success=False, message=validation_result["error"])

            required_amount: int = validation_result["required_amount"]

            if required_amount > 0:
                success, reservation_id, message = await self.wallet.reserve_chips(
                    user_id=user_id,
                    chat_id=chat_id,
                    amount=required_amount,
                    metadata={
                        "action": action,
                        "stage": validation_result.get("stage"),
                        "timestamp": validation_result.get("timestamp"),
                    },
                )

                if not success:
                    return BettingResult(success=False, message=message)

            async with self.locks.acquire_table_write_lock(
                chat_id,
                timeout=30.0,
            ):
                game_state = await self._load_state_with_version(chat_id)

                if not game_state:
                    if reservation_id:
                        await self.wallet.rollback_reservation(
                            reservation_id, reason="game_not_found"
                        )
                    return BettingResult(
                        success=False,
                        message="Game not found or ended",
                    )

                current_player_id = self._extract_current_player_id(game_state)
                if current_player_id is not None and current_player_id != user_id:
                    if reservation_id:
                        await self.wallet.rollback_reservation(
                            reservation_id, reason="not_players_turn"
                        )
                    return BettingResult(success=False, message="Not your turn")

                if reservation_id:
                    commit_success, commit_message = await self.wallet.commit_reservation(
                        reservation_id
                    )
                    if not commit_success:
                        return BettingResult(
                            success=False,
                            message=f"Failed to commit bet: {commit_message}",
                        )
                    committed_reservation_id = reservation_id
                    reservation_id = None
                    committed_amount = required_amount

                new_state = await self.engine.apply_betting_action(
                    game_state,
                    user_id,
                    action,
                    required_amount,
                )

                expected_version = self._extract_version(game_state)
                save_success = await self.engine.save_game_state_with_version(
                    chat_id, new_state, expected_version=expected_version
                )

                if not save_success:
                    logger.error(
                        "Version conflict detected for chat %s. Triggering refund.",
                        chat_id,
                    )
                    if committed_amount > 0:
                        await self.wallet._credit_to_wallet(
                            user_id, chat_id, committed_amount
                        )
                    return BettingResult(
                        success=False,
                        message="State conflict - action cancelled, funds returned",
                    )

                logger.info(
                    "Betting action successful: user=%s chat=%s action=%s amount=%s",
                    user_id,
                    chat_id,
                    action,
                    required_amount,
                )

                return BettingResult(
                    success=True,
                    message=f"{action.capitalize()} successful",
                    new_state=new_state,
                    reservation_id=committed_reservation_id,
                )

        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "Betting action failed: user=%s chat=%s action=%s error=%s",
                user_id,
                chat_id,
                action,
                exc,
                exc_info=True,
            )

            if reservation_id:
                await self.wallet.rollback_reservation(
                    reservation_id, reason=f"exception: {exc}"
                )
            elif committed_amount > 0:
                await self.wallet._credit_to_wallet(user_id, chat_id, committed_amount)

            return BettingResult(success=False, message=f"Action failed: {exc}")

    async def _validate_action(
        self,
        user_id: int,
        chat_id: int,
        action: str,
        amount: Optional[int],
    ) -> Dict[str, Any]:
        """Validate the betting action before processing."""

        load_state = getattr(self.engine, "load_game_state", None)
        if load_state is None:
            raise AttributeError("GameEngine is missing load_game_state method")

        game_state = await load_state(chat_id)
        if not game_state:
            return {"valid": False, "error": "No active game"}

        players = list(game_state.get("players", []))
        player_data = next((p for p in players if p.get("user_id") == user_id), None)

        if not player_data:
            return {"valid": False, "error": "You are not in this game"}

        if player_data.get("folded"):
            return {"valid": False, "error": "You have already folded"}

        current_bet = int(game_state.get("current_bet", 0))
        player_current_bet = int(player_data.get("current_bet", 0))
        to_call = max(current_bet - player_current_bet, 0)

        if action == "fold":
            required_amount = 0
        elif action == "check":
            if to_call > 0:
                return {"valid": False, "error": "Cannot check - must call or fold"}
            required_amount = 0
        elif action == "call":
            required_amount = to_call
        elif action == "raise":
            if amount is None or amount <= current_bet:
                return {"valid": False, "error": "Invalid raise amount"}
            required_amount = amount - player_current_bet
        elif action == "all_in":
            required_amount = int(player_data.get("chips", 0))
        else:
            return {"valid": False, "error": f"Unknown action: {action}"}

        return {
            "valid": True,
            "error": None,
            "required_amount": required_amount,
            "stage": game_state.get("stage", "unknown"),
            "timestamp": time.time(),
        }

    async def _load_state_with_version(self, chat_id: int) -> Optional[Dict[str, Any]]:
        load_with_version = getattr(self.engine, "load_game_state_with_version", None)
        if load_with_version is None:
            state = await getattr(self.engine, "load_game_state")(chat_id)
            if state is None:
                return None
            if isinstance(state, dict):
                state.setdefault("version", 0)
                return state
            return {"state": state, "version": 0}

        state = await load_with_version(chat_id)
        return state

    @staticmethod
    def _extract_version(state: Dict[str, Any]) -> int:
        version = state.get("version")
        try:
            return int(version)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _extract_current_player_id(state: Dict[str, Any]) -> Optional[int]:
        value = state.get("current_player_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
