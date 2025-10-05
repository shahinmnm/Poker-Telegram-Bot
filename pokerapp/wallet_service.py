from __future__ import annotations

"""
Wallet Service - Two-Phase Commit Implementation
Provides atomic chip reservation and commitment for betting operations.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

try:  # pragma: no cover - dependency optional in some environments
    from prometheus_client import Counter, Histogram
except Exception:  # pragma: no cover - fallback when prometheus_client missing
    class _Metric:  # type: ignore[override]
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def labels(self, *args: object, **kwargs: object) -> "_Metric":
            return self

        def inc(self, amount: float = 1.0) -> None:
            return None

        def observe(self, value: float) -> None:
            return None

    Counter = Histogram = _Metric  # type: ignore[assignment]


logger = logging.getLogger(__name__)

# Prometheus metrics
wallet_reserve_counter = Counter(
    "poker_wallet_reserve_total",
    "Total chip reservations",
    ["status"],  # success, insufficient_funds, error
)
wallet_commit_counter = Counter(
    "poker_wallet_commit_total",
    "Total reservation commits",
    ["status"],  # success, not_found, error
)
wallet_rollback_counter = Counter(
    "poker_wallet_rollback_total",
    "Total reservation rollbacks",
    ["status"],  # success, not_found, error
)
wallet_dlq_counter = Counter(
    "poker_wallet_dlq_total",
    "Failed refunds sent to DLQ",
)
wallet_operation_duration = Histogram(
    "poker_wallet_operation_duration_seconds",
    "Duration of wallet operations",
    ["operation"],
)


class ReservationStatus(Enum):
    """Status of a chip reservation."""

    PENDING = "pending"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


@dataclass
class ChipReservation:
    """Represents a pending chip reservation."""

    reservation_id: str
    user_id: int
    chat_id: int
    amount: int
    timestamp: float
    status: ReservationStatus
    metadata: Dict[str, Any]


class WalletService:
    """Manages wallet operations with Two-Phase Commit support."""

    def __init__(self, db_session: Any, redis_client: Any, dlq_handler: Optional[Any] = None) -> None:
        self.db = db_session
        self.redis = redis_client
        self.dlq = dlq_handler
        self._reservations: Dict[str, ChipReservation] = {}
        self._reservation_ttl = 300  # 5 minutes

    async def reserve_chips(
        self,
        user_id: int,
        chat_id: int,
        amount: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str], str]:
        """Phase 1: Reserve chips from user's wallet."""

        start_time = time.time()
        reservation_id = f"res_{user_id}_{chat_id}_{int(time.time() * 1000)}"

        try:
            current_balance = await self._get_user_balance(user_id, chat_id)

            if current_balance < amount:
                wallet_reserve_counter.labels(status="insufficient_funds").inc()
                logger.warning(
                    "Insufficient funds for reservation %s: need=%s, have=%s",
                    reservation_id,
                    amount,
                    current_balance,
                )
                return False, None, f"Insufficient chips: need {amount}, have {current_balance}"

            reservation = ChipReservation(
                reservation_id=reservation_id,
                user_id=user_id,
                chat_id=chat_id,
                amount=amount,
                timestamp=time.time(),
                status=ReservationStatus.PENDING,
                metadata=dict(metadata or {}),
            )

            await self._deduct_from_wallet(user_id, chat_id, amount)

            self._reservations[reservation_id] = reservation
            await self._persist_reservation(reservation)

            asyncio.create_task(self._auto_expire_reservation(reservation_id))

            wallet_reserve_counter.labels(status="success").inc()
            logger.info(
                "Reserved %s chips for user %s (reservation_id=%s)",
                amount,
                user_id,
                reservation_id,
            )

            return True, reservation_id, "Reservation successful"

        except Exception as exc:  # pragma: no cover - defensive logging
            wallet_reserve_counter.labels(status="error").inc()
            logger.error(
                "Reservation failed for %s: %s",
                reservation_id,
                exc,
                exc_info=True,
            )
            return False, None, f"Reservation error: {exc}"

        finally:
            duration = time.time() - start_time
            wallet_operation_duration.labels(operation="reserve").observe(duration)

    async def commit_reservation(self, reservation_id: str) -> Tuple[bool, str]:
        """Phase 2: Commit the reservation (finalize the bet)."""

        start_time = time.time()

        try:
            reservation = self._reservations.get(reservation_id)

            if not reservation:
                wallet_commit_counter.labels(status="not_found").inc()
                logger.error("Reservation not found: %s", reservation_id)
                return False, "Reservation not found or expired"

            if reservation.status is not ReservationStatus.PENDING:
                logger.warning(
                    "Invalid reservation status for %s: %s",
                    reservation_id,
                    reservation.status,
                )
                return False, f"Reservation already {reservation.status.value}"

            reservation.status = ReservationStatus.COMMITTED
            await self._persist_reservation(reservation)

            del self._reservations[reservation_id]

            wallet_commit_counter.labels(status="success").inc()
            logger.info("Committed reservation %s", reservation_id)

            return True, "Reservation committed"

        except Exception as exc:  # pragma: no cover - defensive logging
            wallet_commit_counter.labels(status="error").inc()
            logger.error(
                "Commit failed for %s: %s",
                reservation_id,
                exc,
                exc_info=True,
            )
            return False, f"Commit error: {exc}"

        finally:
            duration = time.time() - start_time
            wallet_operation_duration.labels(operation="commit").observe(duration)

    async def rollback_reservation(
        self,
        reservation_id: str,
        reason: str = "explicit_rollback",
    ) -> Tuple[bool, str]:
        """Abort and return reserved funds to the user."""

        start_time = time.time()

        try:
            reservation = self._reservations.get(reservation_id)

            if not reservation:
                wallet_rollback_counter.labels(status="not_found").inc()
                logger.warning(
                    "Rollback requested for unknown reservation: %s",
                    reservation_id,
                )
                return False, "Reservation not found"

            if reservation.status is not ReservationStatus.PENDING:
                logger.warning(
                    "Cannot rollback reservation %s with status %s",
                    reservation_id,
                    reservation.status,
                )
                return False, f"Reservation is {reservation.status.value}"

            try:
                await self._credit_to_wallet(
                    reservation.user_id, reservation.chat_id, reservation.amount
                )

                reservation.status = ReservationStatus.ROLLED_BACK
                await self._persist_reservation(reservation)
                del self._reservations[reservation_id]

                wallet_rollback_counter.labels(status="success").inc()
                logger.info(
                    "Rolled back reservation %s due to %s",
                    reservation_id,
                    reason,
                )

                return True, "Reservation rolled back"

            except Exception as refund_error:
                await self._send_to_dlq(reservation, refund_error, reason)
                wallet_dlq_counter.inc()
                logger.critical(
                    "REFUND FAILED for %s, sent to DLQ: %s",
                    reservation_id,
                    refund_error,
                )
                return False, "Refund failed - queued for manual resolution"

        except Exception as exc:  # pragma: no cover - defensive logging
            wallet_rollback_counter.labels(status="error").inc()
            logger.error(
                "Rollback error for %s: %s",
                reservation_id,
                exc,
                exc_info=True,
            )
            return False, f"Rollback error: {exc}"

        finally:
            duration = time.time() - start_time
            wallet_operation_duration.labels(operation="rollback").observe(duration)

    # -------------------- PRIVATE HELPERS --------------------

    async def _get_user_balance(self, user_id: int, chat_id: int) -> int:
        """Get current wallet balance from database."""

        from pokerapp.models import Player  # Local import to avoid circular deps

        query = self.db.query(Player).filter_by(user_id=user_id, chat_id=chat_id)
        player = await query.first()
        return int(getattr(player, "chips", 0)) if player else 0

    async def _deduct_from_wallet(self, user_id: int, chat_id: int, amount: int) -> None:
        """Atomically deduct chips from wallet."""

        from pokerapp.models import Player  # Local import to avoid circular deps

        query = (
            self.db.query(Player)
            .filter_by(user_id=user_id, chat_id=chat_id)
            .with_for_update()
        )
        player = await query.first()

        if not player or getattr(player, "chips", 0) < amount:
            raise ValueError("Insufficient funds or player not found")

        player.chips -= amount
        await self.db.commit()

    async def _credit_to_wallet(self, user_id: int, chat_id: int, amount: int) -> None:
        """Atomically credit chips to wallet."""

        from pokerapp.models import Player  # Local import to avoid circular deps

        query = (
            self.db.query(Player)
            .filter_by(user_id=user_id, chat_id=chat_id)
            .with_for_update()
        )
        player = await query.first()

        if not player:
            raise ValueError("Player not found")

        player.chips += amount
        await self.db.commit()

    async def _persist_reservation(self, reservation: ChipReservation) -> None:
        """Store reservation in Redis for durability."""

        key = f"poker:reservation:{reservation.reservation_id}"
        data = {
            "user_id": reservation.user_id,
            "chat_id": reservation.chat_id,
            "amount": reservation.amount,
            "timestamp": reservation.timestamp,
            "status": reservation.status.value,
            "metadata": reservation.metadata,
        }
        await self.redis.setex(key, self._reservation_ttl, repr(data))

    async def _auto_expire_reservation(self, reservation_id: str) -> None:
        """Automatically rollback expired reservations."""

        await asyncio.sleep(self._reservation_ttl)

        reservation = self._reservations.get(reservation_id)
        if reservation and reservation.status is ReservationStatus.PENDING:
            logger.warning("Auto-expiring reservation %s", reservation_id)
            await self.rollback_reservation(reservation_id, reason="timeout")

    async def _send_to_dlq(
        self,
        reservation: ChipReservation,
        error: Exception,
        context: str,
    ) -> None:
        """Send failed refund to Dead Letter Queue for manual resolution."""

        if not self.dlq:
            logger.critical(
                "NO DLQ CONFIGURED - manual refund required: user_id=%s amount=%s",
                reservation.user_id,
                reservation.amount,
            )
            return

        dlq_entry = {
            "reservation_id": reservation.reservation_id,
            "user_id": reservation.user_id,
            "chat_id": reservation.chat_id,
            "amount": reservation.amount,
            "error": str(error),
            "context": context,
            "timestamp": time.time(),
        }
        await self.dlq.push(dlq_entry)
