"""Betting handler orchestrating Two-Phase Commit betting actions."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from pokerapp.lock_manager import LockManager
from pokerapp.metrics import (
    ACTION_DURATION,
    LOCK_QUEUE_DEPTH,
    LOCK_RETRY_TOTAL,
    LOCK_WAIT_DURATION,
)
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
        *,
        config: Optional[Any] = None,
        retry_settings: Optional[Mapping[str, Any]] = None,
        enable_smart_retry: Optional[bool] = None,
    ) -> None:
        self._wallet = wallet_service
        self._engine = game_engine
        self._locks = lock_manager
        self._retry_policy = self._initialise_retry_policy(
            config=config, overrides=retry_settings
        )
        if enable_smart_retry is not None:
            self._smart_retry_enabled = bool(enable_smart_retry)
        else:
            env_flag = os.getenv("ENABLE_SMART_RETRY")
            if env_flag is not None:
                self._smart_retry_enabled = env_flag.strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            else:
                self._smart_retry_enabled = self._retry_policy["max_attempts"] > 0
        self._reservation_ttl_seconds = float(
            getattr(wallet_service, "_reservation_ttl", 300)
        )

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
        reservation_started_at: Optional[float] = None
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
                (
                    reserve_success,
                    reservation_id,
                    reserve_message,
                    reservation_started_at,
                ) = await self._reserve_chips(
                    user_id=user_id,
                    chat_id=chat_id,
                    action=normalized_action,
                    amount=required_amount,
                )
                if not reserve_success or reservation_id is None:
                    return BettingResult(False, reserve_message)

            result, committed = await self._commit_with_retry(
                user_id=user_id,
                chat_id=chat_id,
                normalized_action=normalized_action,
                required_amount=required_amount,
                reservation_id=reservation_id,
                reservation_started_at=reservation_started_at,
            )
            return result
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
                await self._rollback(
                    reservation_id,
                    f"exception:{exc}",
                    allow_committed=committed,
                )
            return BettingResult(False, f"Action failed: {exc}")
        finally:
            ACTION_DURATION.labels(action=normalized_action or "unknown").observe(
                time.perf_counter() - start_time
            )

    def _initialise_retry_policy(
        self,
        *,
        config: Optional[Any],
        overrides: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        defaults: Dict[str, Any] = {
            "max_attempts": 3,
            "backoff_delays_seconds": [1, 2, 4, 8],
            "queue_depth_threshold": 5,
            "estimated_wait_threshold_seconds": 25.0,
            "grace_buffer_seconds": 30.0,
        }

        policy = dict(defaults)

        config_obj = config
        if config_obj is None:
            try:  # pragma: no cover - config optional in some tests
                from pokerapp.config import Config as _Config

                config_obj = _Config()
            except Exception:  # pragma: no cover - fallback to defaults
                config_obj = None

        if config_obj is not None:
            system_constants = getattr(config_obj, "system_constants", None)
            if isinstance(system_constants, Mapping):
                candidate = system_constants.get("lock_retry")
                if isinstance(candidate, Mapping):
                    policy.update(candidate)

        if isinstance(overrides, Mapping):
            policy.update(overrides)

        try:
            policy["max_attempts"] = max(0, int(policy.get("max_attempts", 0)))
        except (TypeError, ValueError):
            policy["max_attempts"] = defaults["max_attempts"]

        try:
            policy["queue_depth_threshold"] = max(
                0, int(policy.get("queue_depth_threshold", 0))
            )
        except (TypeError, ValueError):
            policy["queue_depth_threshold"] = defaults["queue_depth_threshold"]

        try:
            policy["estimated_wait_threshold_seconds"] = max(
                0.0,
                float(policy.get("estimated_wait_threshold_seconds", 0.0)),
            )
        except (TypeError, ValueError):
            policy["estimated_wait_threshold_seconds"] = defaults[
                "estimated_wait_threshold_seconds"
            ]

        try:
            policy["grace_buffer_seconds"] = max(
                0.0, float(policy.get("grace_buffer_seconds", 0.0))
            )
        except (TypeError, ValueError):
            policy["grace_buffer_seconds"] = defaults["grace_buffer_seconds"]

        backoff_values = policy.get(
            "backoff_delays_seconds", defaults["backoff_delays_seconds"]
        )
        if not isinstance(backoff_values, (list, tuple)):
            backoff_values = defaults["backoff_delays_seconds"]
        policy["backoff_delays_seconds"] = [
            max(0.0, float(value))
            for value in backoff_values
            if isinstance(value, (int, float))
        ]
        if not policy["backoff_delays_seconds"]:
            policy["backoff_delays_seconds"] = defaults["backoff_delays_seconds"]

        return policy

    async def _reserve_chips(
        self,
        *,
        user_id: int,
        chat_id: int,
        action: str,
        amount: int,
    ) -> tuple[bool, Optional[str], str, Optional[float]]:
        reservation_started_at = time.monotonic()
        reserve_success, reservation_id, reserve_message = await self._wallet.reserve_chips(
            user_id=user_id,
            chat_id=chat_id,
            amount=amount,
            metadata={"action": action, "chat_id": chat_id},
        )
        if not reserve_success or reservation_id is None:
            return reserve_success, reservation_id, reserve_message, None
        return reserve_success, reservation_id, reserve_message, reservation_started_at

    async def _commit_with_retry(
        self,
        *,
        user_id: int,
        chat_id: int,
        normalized_action: str,
        required_amount: int,
        reservation_id: Optional[str],
        reservation_started_at: Optional[float],
    ) -> tuple[BettingResult, bool]:
        max_retries = int(self._retry_policy.get("max_attempts", 0))
        committed = False

        if not self._smart_retry_enabled or max_retries <= 0:
            attempt_start = time.perf_counter()
            try:
                async with self._locks.acquire_table_write_lock(
                    chat_id, timeout=self._LOCK_TIMEOUT_SECONDS
                ):
                    wait_time = time.perf_counter() - attempt_start
                    LOCK_WAIT_DURATION.observe(wait_time)
                    return await self._commit_and_save(
                        user_id=user_id,
                        chat_id=chat_id,
                        action=normalized_action,
                        required_amount=required_amount,
                        reservation_id=reservation_id,
                    )
            except TimeoutError:
                wait_time = time.perf_counter() - attempt_start
                LOCK_WAIT_DURATION.observe(wait_time)
                LOCK_RETRY_TOTAL.labels(outcome="timeout").inc()
                await self._rollback(reservation_id, "lock_timeout")
                return (
                    BettingResult(False, "Table busy - please try again"),
                    committed,
                )

        for attempt in range(max_retries + 1):
            attempt_start = time.perf_counter()
            try:
                async with self._locks.acquire_table_write_lock(
                    chat_id, timeout=self._LOCK_TIMEOUT_SECONDS
                ):
                    wait_time = time.perf_counter() - attempt_start
                    LOCK_WAIT_DURATION.observe(wait_time)
                    result, committed = await self._commit_and_save(
                        user_id=user_id,
                        chat_id=chat_id,
                        action=normalized_action,
                        required_amount=required_amount,
                        reservation_id=reservation_id,
                    )
                    if attempt > 0:
                        LOCK_RETRY_TOTAL.labels(outcome="success").inc()
                    return result, committed
            except TimeoutError:
                wait_time = time.perf_counter() - attempt_start
                LOCK_WAIT_DURATION.observe(wait_time)

                if attempt == max_retries:
                    LOCK_RETRY_TOTAL.labels(outcome="timeout").inc()
                    LOCK_RETRY_TOTAL.labels(outcome="max_retries").inc()
                    await self._rollback(
                        reservation_id,
                        "max_retries_exceeded",
                        allow_committed=committed,
                    )
                    return (
                        BettingResult(False, "Table busy - please try again"),
                        committed,
                    )

                queue_depth = await self._get_queue_depth(chat_id)
                LOCK_QUEUE_DEPTH.observe(queue_depth)
                estimated_wait = await self._estimate_queue_wait(queue_depth)
                reservation_age = self._get_reservation_age(reservation_started_at)
                time_until_expiry = (
                    float("inf")
                    if reservation_id is None
                    else max(0.0, self._reservation_ttl_seconds - reservation_age)
                )

                if self._should_retry(
                    estimated_wait=estimated_wait,
                    time_until_expiry=time_until_expiry,
                    queue_depth=queue_depth,
                    attempt=attempt,
                ):
                    delay = self._select_backoff_delay(attempt)
                    if delay > 0:
                        logger.info(
                            "Lock retry %d/%d: queue_depth=%s, estimated_wait=%.1fs, backoff=%.1fs",
                            attempt + 1,
                            max_retries,
                            queue_depth,
                            estimated_wait,
                            delay,
                        )
                        await asyncio.sleep(delay)
                    continue

                abort_reason = self._classify_abort_reason(
                    queue_depth=queue_depth,
                    estimated_wait=estimated_wait,
                    time_until_expiry=time_until_expiry,
                )
                LOCK_RETRY_TOTAL.labels(outcome="abandoned").inc()
                await self._rollback(reservation_id, abort_reason or "retry_aborted")
                message = self._format_abort_message(abort_reason, queue_depth)
                return BettingResult(False, message), committed

        # Fallback (should be unreachable)
        LOCK_RETRY_TOTAL.labels(outcome="timeout").inc()
        await self._rollback(reservation_id, "unexpected_retry_exit")
        return BettingResult(False, "Table busy - please try again"), committed

    async def _get_queue_depth(self, chat_id: int) -> int:
        getter = getattr(self._locks, "get_lock_queue_depth", None)
        if getter is None:
            return 0
        result = getter(chat_id)
        if asyncio.iscoroutine(result):
            return await result
        try:
            return int(result)
        except (TypeError, ValueError):
            return 0

    async def _estimate_queue_wait(self, queue_depth: int) -> float:
        estimator = getattr(self._locks, "estimate_wait_time", None)
        if estimator is None:
            return max(0.0, float(queue_depth) * 5.0)
        result = estimator(queue_depth)
        if asyncio.iscoroutine(result):
            result = await result
        try:
            return max(0.0, float(result))
        except (TypeError, ValueError):
            return 0.0

    def _get_reservation_age(self, reservation_started_at: Optional[float]) -> float:
        if reservation_started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - reservation_started_at)

    async def _rollback(
        self,
        reservation_id: Optional[str],
        reason: str,
        *,
        allow_committed: bool = False,
    ) -> None:
        if not reservation_id:
            return
        try:
            await self._wallet.rollback_reservation(
                reservation_id,
                reason,
                allow_committed=allow_committed,
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception(
                "Failed to rollback reservation",
                extra={"reservation_id": reservation_id, "reason": reason},
            )

    def _select_backoff_delay(self, attempt: int) -> float:
        schedule = self._retry_policy.get("backoff_delays_seconds", [])
        if not schedule:
            return 0.0
        index = min(attempt, len(schedule) - 1)
        try:
            return max(0.0, float(schedule[index]))
        except (TypeError, ValueError):
            return 0.0

    def _should_retry(
        self,
        *,
        estimated_wait: float,
        time_until_expiry: float,
        queue_depth: int,
        attempt: int,
    ) -> bool:
        if queue_depth > int(self._retry_policy.get("queue_depth_threshold", 0)):
            return False
        if estimated_wait > time_until_expiry:
            return False
        if estimated_wait > float(
            self._retry_policy.get("estimated_wait_threshold_seconds", 0.0)
        ):
            return False
        if time_until_expiry < (
            estimated_wait
            + float(self._retry_policy.get("grace_buffer_seconds", 0.0))
        ):
            return False
        return True

    def _classify_abort_reason(
        self,
        *,
        queue_depth: int,
        estimated_wait: float,
        time_until_expiry: float,
    ) -> str:
        threshold_depth = int(self._retry_policy.get("queue_depth_threshold", 0))
        if queue_depth > threshold_depth:
            return "queue_congested"
        if time_until_expiry <= 0:
            return "reservation_expired"
        if estimated_wait > time_until_expiry:
            return "reservation_expiring"
        if estimated_wait > float(
            self._retry_policy.get("estimated_wait_threshold_seconds", 0.0)
        ):
            return "wait_too_long"
        if time_until_expiry < (
            estimated_wait
            + float(self._retry_policy.get("grace_buffer_seconds", 0.0))
        ):
            return "insufficient_grace"
        return "abandoned"

    def _format_abort_message(self, reason: str, queue_depth: int) -> str:
        if reason == "queue_congested":
            return f"Table very busy (queue: {queue_depth}) - try again later"
        if reason in {"reservation_expired", "reservation_expiring"}:
            return "Reservation expired while waiting for the table"
        return "Table busy - please try again"

    async def _commit_and_save(
        self,
        *,
        user_id: int,
        chat_id: int,
        action: str,
        required_amount: int,
        reservation_id: Optional[str],
    ) -> tuple[BettingResult, bool]:
        state = await self._engine.load_game_state(chat_id)
        if not isinstance(state, Mapping):
            if reservation_id:
                await self._wallet.rollback_reservation(
                    reservation_id, "game_not_found"
                )
            return BettingResult(False, "Game not found or has ended"), False

        if not self._is_players_turn(state, user_id):
            if reservation_id:
                await self._wallet.rollback_reservation(
                    reservation_id, "not_players_turn"
                )
            return BettingResult(False, "It is not your turn"), False

        expected_version = self._extract_version(state)
        committed = False

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
                return BettingResult(False, commit_message), committed
            committed = True

        updated_state = await self._apply_action(
            state, user_id, action, required_amount
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
            return (
                BettingResult(
                    False,
                    "State update conflict detected – action cancelled",
                ),
                committed,
            )

        logger.info(
            "Betting action succeeded",
            extra={
                "user_id": user_id,
                "chat_id": chat_id,
                "action": action,
                "amount": required_amount,
                "reservation_id": reservation_id,
            },
        )

        return (
            BettingResult(
                True,
                f"{action.replace('_', ' ').title()} successful",
                new_state=dict(updated_state),
                reservation_id=reservation_id,
            ),
            committed,
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

