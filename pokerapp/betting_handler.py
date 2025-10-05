"""Betting handler orchestrating Two-Phase Commit betting actions."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from pokerapp.lock_manager import LockManager
from pokerapp.metrics import ACTION_DURATION
from pokerapp.wallet_service import WalletService

logger = logging.getLogger(__name__)


@dataclass
class BettingResult:
    success: bool
    message: str
    new_state: Optional[Dict[str, Any]] = None
    reservation_id: Optional[str] = None


class BettingHandler:
    """Coordinates the 2PC flow between the wallet and the game engine."""

    _LOCK_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        wallet_service: WalletService,
        game_engine: Any,
        lock_manager: LockManager,
    ) -> None:
        self._wallet = wallet_service
        self._engine = game_engine
        self._locks = lock_manager

    async def handle_betting_action(
        self,
        user_id: int,
        chat_id: int,
        action: str,
        amount: Optional[int] = None,
    ) -> BettingResult:
        """Execute a betting action using the Two-Phase Commit protocol."""

        start_time = time.perf_counter()
        normalized_action = (action or "").strip().lower()
        reservation_id: Optional[str] = None
        committed = False

        try:
            validation = await self._validate_action(
                user_id=user_id,
                chat_id=chat_id,
                action=normalized_action,
                amount=amount,
            )

            if not validation["valid"]:
                return BettingResult(False, validation["error"])

            required_amount = int(validation["required_amount"])

            if required_amount > 0:
                reserve_success, reservation_id, reserve_message = (
                    await self._wallet.reserve_chips(
                        user_id=user_id,
                        chat_id=chat_id,
                        amount=required_amount,
                        metadata={
                            "action": normalized_action,
                            "chat_id": chat_id,
                        },
                    )
                )
                if not reserve_success or reservation_id is None:
                    return BettingResult(False, reserve_message)

            async with self._locks.acquire_table_write_lock(
                chat_id, timeout=self._LOCK_TIMEOUT_SECONDS
            ):
                state = await self._engine.load_game_state(chat_id)
                if not isinstance(state, Mapping):
                    if reservation_id:
                        await self._wallet.rollback_reservation(
                            reservation_id, "game_not_found"
                        )
                    return BettingResult(False, "Game not found or has ended")

                if not self._is_players_turn(state, user_id):
                    if reservation_id:
                        await self._wallet.rollback_reservation(
                            reservation_id, "not_players_turn"
                        )
                    return BettingResult(False, "It is not your turn")

                expected_version = self._extract_version(state)

                if reservation_id is not None:
                    commit_success, commit_message = await self._wallet.commit_reservation(
                        reservation_id
                    )
                    if not commit_success:
                        await self._wallet.rollback_reservation(
                            reservation_id,
                            f"commit_failed:{commit_message}",
                            allow_committed=True,
                        )
                        return BettingResult(False, commit_message)
                    committed = True

                updated_state = await self._apply_action(
                    state, user_id, normalized_action, required_amount
                )

                save_success = await self._engine.save_game_state_with_version(
                    chat_id,
                    updated_state,
                    expected_version=expected_version,
                )

                if not save_success:
                    if reservation_id is not None:
                        await self._wallet.rollback_reservation(
                            reservation_id,
                            "version_conflict",
                            allow_committed=committed,
                        )
                    return BettingResult(
                        False,
                        "State update conflict detected – action cancelled",
                    )

                logger.info(
                    "Betting action succeeded",
                    extra={
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "action": normalized_action,
                        "amount": required_amount,
                        "reservation_id": reservation_id,
                    },
                )

                return BettingResult(
                    True,
                    f"{normalized_action.replace('_', ' ').title()} successful",
                    new_state=dict(updated_state),
                    reservation_id=reservation_id,
                )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception(
                "Betting action failed",
                extra={
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "action": normalized_action,
                    "reservation_id": reservation_id,
                },
            )
            if reservation_id is not None:
                await self._wallet.rollback_reservation(
                    reservation_id,
                    f"exception:{exc}",
                    allow_committed=committed,
                )
            return BettingResult(False, f"Action failed: {exc}")
        finally:
            ACTION_DURATION.labels(action=normalized_action or "unknown").observe(
                time.perf_counter() - start_time
            )

    async def _validate_action(
        self,
        user_id: int,
        chat_id: int,
        action: str,
        amount: Optional[int],
    ) -> Dict[str, Any]:
        engine_state = await self._engine.load_game_state(chat_id)
        if not isinstance(engine_state, Mapping):
            return {"valid": False, "error": "Game state not available"}

        players = list(engine_state.get("players", []))
        player_data = next(
            (p for p in players if int(p.get("user_id", 0)) == int(user_id)),
            None,
        )
        if player_data is None:
            return {"valid": False, "error": "Player not seated at the table"}

        if player_data.get("folded"):
            return {"valid": False, "error": "Player already folded"}

        current_bet = int(engine_state.get("current_bet", 0))
        player_bet = int(player_data.get("current_bet", 0))
        to_call = max(current_bet - player_bet, 0)

        if action == "fold":
            required = 0
        elif action == "check":
            if to_call > 0:
                return {"valid": False, "error": "Cannot check – call or fold"}
            required = 0
        elif action == "call":
            required = to_call
        elif action == "raise":
            if amount is None or amount <= current_bet:
                return {"valid": False, "error": "Invalid raise amount"}
            required = amount - player_bet
        elif action == "all_in":
            required = int(player_data.get("chips", 0))
        else:
            return {"valid": False, "error": f"Unknown action '{action}'"}

        return {"valid": True, "required_amount": max(required, 0)}

    async def _apply_action(
        self,
        state: Mapping[str, Any],
        user_id: int,
        action: str,
        amount: int,
    ) -> Mapping[str, Any]:
        apply_handler = getattr(self._engine, "apply_betting_action", None)
        if apply_handler is None:
            raise AttributeError("Game engine does not implement apply_betting_action")
        updated_state = await apply_handler(state, user_id, action, amount)
        if not isinstance(updated_state, Mapping):
            raise TypeError("apply_betting_action must return mapping state")
        return updated_state

    @staticmethod
    def _extract_version(state: Mapping[str, Any]) -> int:
        try:
            return int(state.get("version", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_players_turn(state: Mapping[str, Any], user_id: int) -> bool:
        current_player = state.get("current_player_id")
        try:
            return int(current_player) == int(user_id)
        except (TypeError, ValueError):
            return False

