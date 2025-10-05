"""Wallet service implementing the Two-Phase Commit reservation protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Mapping, Optional, Protocol, Tuple

from pokerapp.metrics import (
    WALLET_COMMIT_COUNTER,
    WALLET_DLQ_COUNTER,
    WALLET_OPERATION_DURATION,
    WALLET_RESERVE_COUNTER,
    WALLET_ROLLBACK_COUNTER,
)
from pokerapp.redis_client import RedisClient

logger = logging.getLogger(__name__)


class WalletRepository(Protocol):
    """Persistence boundary for mutating wallet balances."""

    async def get_balance(self, user_id: int, chat_id: int) -> int:
        ...

    async def debit(
        self, user_id: int, chat_id: int, amount: int, *, metadata: Mapping[str, Any]
    ) -> None:
        ...

    async def credit(
        self, user_id: int, chat_id: int, amount: int, *, metadata: Mapping[str, Any]
    ) -> None:
        ...


class DeadLetterQueue(Protocol):
    """DLQ interface used when automated refunds fail."""

    async def push(self, payload: Mapping[str, Any]) -> None:
        ...


class ReservationStatus(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class ReservationRecord:
    reservation_id: str
    user_id: int
    chat_id: int
    amount: int
    status: ReservationStatus
    metadata: Mapping[str, Any]
    created_at: float


_CREATE_RESERVATION_SCRIPT = """-- reservation_create
local exists = redis.call('EXISTS', KEYS[1])
if exists == 1 then
  return 0
end
redis.call('HSET', KEYS[1],
  'user_id', ARGV[1],
  'chat_id', ARGV[2],
  'amount', ARGV[3],
  'status', ARGV[4],
  'metadata', ARGV[5],
  'created_at', ARGV[6]
)
redis.call('PEXPIRE', KEYS[1], ARGV[7])
return 1
"""


_COMMIT_RESERVATION_SCRIPT = """-- reservation_commit
local status = redis.call('HGET', KEYS[1], 'status')
if not status then
  return 'missing'
end
if status == 'committed' then
  return 'committed'
end
if status ~= 'pending' then
  return status
end
redis.call('HSET', KEYS[1], 'status', 'committed')
redis.call('PEXPIRE', KEYS[1], ARGV[1])
return 'ok'
"""


_ROLLBACK_RESERVATION_SCRIPT = """-- reservation_rollback
local status = redis.call('HGET', KEYS[1], 'status')
if not status then
  return 'missing'
end
if status == 'rolled_back' then
  return 'rolled_back'
end
if status == 'committed' then
  if ARGV[1] == '1' then
    redis.call('HSET', KEYS[1], 'status', 'rolled_back')
    redis.call('HSET', KEYS[1], 'rollback_reason', ARGV[2])
    redis.call('PEXPIRE', KEYS[1], ARGV[3])
    return 'compensated'
  end
  return 'committed'
end
if status ~= 'pending' then
  return status
end
redis.call('HSET', KEYS[1], 'status', 'rolled_back')
redis.call('HSET', KEYS[1], 'rollback_reason', ARGV[2])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return 'rolled_back'
"""


class WalletService:
    """Coordinates wallet reservations for the Two-Phase Commit workflow."""

    _RESERVATION_KEY_TEMPLATE = "wallet:reservation:{reservation_id}"

    def __init__(
        self,
        wallet_repository: WalletRepository,
        redis_client: RedisClient,
        *,
        dlq: Optional[DeadLetterQueue] = None,
        reservation_ttl_seconds: int = 300,
        redis_timeout: float = 5.0,
        wallet_timeout: float = 5.0,
    ) -> None:
        self._wallet_repository = wallet_repository
        self._redis = redis_client
        self._dlq = dlq
        self._reservation_ttl = reservation_ttl_seconds
        self._reservation_grace_period = 30
        self._committed_ttl = 3600
        self._redis_timeout = redis_timeout
        self._wallet_timeout = wallet_timeout
        self._watchdogs: Dict[str, asyncio.Task[None]] = {}

    async def reserve_chips(
        self,
        user_id: int,
        chat_id: int,
        amount: int,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[bool, Optional[str], str]:
        """Phase 1 – reserve chips outside of the table lock."""

        start_time = time.perf_counter()
        metadata = dict(metadata or {})
        reservation_id = uuid.uuid4().hex

        if amount <= 0:
            WALLET_RESERVE_COUNTER.labels(status="success").inc()
            WALLET_OPERATION_DURATION.labels(operation="reserve").observe(
                time.perf_counter() - start_time
            )
            return True, reservation_id, "No chips reserved"

        balance: Optional[int] = None
        try:
            balance = await self._with_timeout(
                self._wallet_repository.get_balance(user_id, chat_id),
                self._wallet_timeout,
                "wallet_get_balance",
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            WALLET_RESERVE_COUNTER.labels(status="error").inc()
            WALLET_OPERATION_DURATION.labels(operation="reserve").observe(
                time.perf_counter() - start_time
            )
            logger.error(
                "Failed to query balance for user_id=%s chat_id=%s: %s",
                user_id,
                chat_id,
                exc,
                exc_info=True,
            )
            return False, None, "Unable to access wallet"

        if balance is None or balance < amount:
            WALLET_RESERVE_COUNTER.labels(status="insufficient_funds").inc()
            WALLET_OPERATION_DURATION.labels(operation="reserve").observe(
                time.perf_counter() - start_time
            )
            logger.info(
                "Reservation rejected due to insufficient funds",
                extra={
                    "reservation_id": reservation_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "requested": amount,
                    "balance": balance,
                },
            )
            return False, None, "Insufficient chips for reservation"

        debit_performed = False
        try:
            await self._with_timeout(
                self._wallet_repository.debit(
                    user_id, chat_id, amount, metadata=metadata
                ),
                self._wallet_timeout,
                "wallet_debit",
            )
            debit_performed = True

            record = {
                "user_id": str(user_id),
                "chat_id": str(chat_id),
                "amount": str(amount),
                "status": ReservationStatus.PENDING.value,
                "metadata": json.dumps(metadata, default=str),
                "created_at": str(time.time()),
            }
            ttl_ms = (self._reservation_ttl + self._reservation_grace_period) * 1000
            create_result = await self._with_timeout(
                self._redis.eval(
                    _CREATE_RESERVATION_SCRIPT,
                    [self._reservation_key(reservation_id)],
                    [
                        record["user_id"],
                        record["chat_id"],
                        record["amount"],
                        record["status"],
                        record["metadata"],
                        record["created_at"],
                        str(ttl_ms),
                    ],
                ),
                self._redis_timeout,
                "redis_create_reservation",
            )

            if int(create_result or 0) != 1:
                await self._with_timeout(
                    self._wallet_repository.credit(
                        user_id, chat_id, amount, metadata={"reason": "reservation_conflict"}
                    ),
                    self._wallet_timeout,
                    "wallet_credit_on_conflict",
                )
                WALLET_RESERVE_COUNTER.labels(status="error").inc()
                WALLET_OPERATION_DURATION.labels(operation="reserve").observe(
                    time.perf_counter() - start_time
                )
                logger.error(
                    "Duplicate reservation detected for %s", reservation_id
                )
                return False, None, "Unable to create reservation"

            self._schedule_watchdog(reservation_id)
            WALLET_RESERVE_COUNTER.labels(status="success").inc()
            logger.info(
                "Reserved %s chips for user_id=%s chat_id=%s reservation_id=%s",
                amount,
                user_id,
                chat_id,
                reservation_id,
            )
            return True, reservation_id, "Reservation successful"
        except Exception as exc:
            if debit_performed:
                try:
                    await self._with_timeout(
                        self._wallet_repository.credit(
                            user_id,
                            chat_id,
                            amount,
                            metadata={"reason": "reservation_failure"},
                        ),
                        self._wallet_timeout,
                        "wallet_credit_on_failure",
                    )
                except Exception:  # pragma: no cover - best effort compensation
                    logger.exception(
                        "Failed to compensate wallet after reservation error",
                        extra={"reservation_id": reservation_id},
                    )

            WALLET_RESERVE_COUNTER.labels(status="error").inc()
            logger.exception(
                "Reservation error for reservation_id=%s", reservation_id
            )
            return False, None, f"Reservation error: {exc}"
        finally:
            WALLET_OPERATION_DURATION.labels(operation="reserve").observe(
                time.perf_counter() - start_time
            )

    async def commit_reservation(self, reservation_id: str) -> Tuple[bool, str]:
        """Phase 2 – finalize the reservation once the table lock is held."""

        start_time = time.perf_counter()
        try:
            result = await self._with_timeout(
                self._redis.eval(
                    _COMMIT_RESERVATION_SCRIPT,
                    [self._reservation_key(reservation_id)],
                    [str(self._committed_ttl * 1000)],
                ),
                self._redis_timeout,
                "redis_commit_reservation",
            )

            if result == "missing":
                WALLET_COMMIT_COUNTER.labels(status="not_found").inc()
                logger.warning("Reservation %s not found during commit", reservation_id)
                return False, "Reservation not found"

            if result == "committed":
                WALLET_COMMIT_COUNTER.labels(status="success").inc()
                logger.info(
                    "Commit idempotent for reservation_id=%s", reservation_id
                )
                self._cancel_watchdog(reservation_id)
                return True, "Reservation already committed"

            if result != "ok":
                WALLET_COMMIT_COUNTER.labels(status="error").inc()
                logger.error(
                    "Unexpected reservation status during commit: %s", result
                )
                return False, f"Unable to commit reservation ({result})"

            WALLET_COMMIT_COUNTER.labels(status="success").inc()
            self._cancel_watchdog(reservation_id)
            logger.info("Committed reservation %s", reservation_id)
            return True, "Reservation committed"
        except Exception as exc:  # pragma: no cover - defensive logging
            WALLET_COMMIT_COUNTER.labels(status="error").inc()
            logger.exception(
                "Commit failure for reservation_id=%s", reservation_id
            )
            return False, f"Commit error: {exc}"
        finally:
            WALLET_OPERATION_DURATION.labels(operation="commit").observe(
                time.perf_counter() - start_time
            )

    async def rollback_reservation(
        self,
        reservation_id: str,
        reason: str,
        *,
        allow_committed: bool = False,
    ) -> Tuple[bool, str]:
        """Abort a reservation and return the chips to the player."""

        start_time = time.perf_counter()
        try:
            script_result = await self._with_timeout(
                self._redis.eval(
                    _ROLLBACK_RESERVATION_SCRIPT,
                    [self._reservation_key(reservation_id)],
                    [
                        "1" if allow_committed else "0",
                        reason,
                        str(self._committed_ttl * 1000),
                    ],
                ),
                self._redis_timeout,
                "redis_rollback_reservation",
            )

            if script_result in {"missing", None}:
                WALLET_ROLLBACK_COUNTER.labels(status="not_found").inc()
                logger.warning(
                    "Rollback requested for missing reservation_id=%s", reservation_id
                )
                return False, "Reservation not found"

            if script_result == "committed" and not allow_committed:
                WALLET_ROLLBACK_COUNTER.labels(status="error").inc()
                logger.error(
                    "Rollback attempted on committed reservation without permission"
                )
                return False, "Reservation already committed"

            if script_result == "rolled_back":
                record = await self._load_reservation(reservation_id)
                await self._credit_reservation_amount(record, reason)
                WALLET_ROLLBACK_COUNTER.labels(status="success").inc()
                self._cancel_watchdog(reservation_id)
                logger.info(
                    "Rolled back reservation %s for reason=%s",
                    reservation_id,
                    reason,
                )
                return True, "Reservation rolled back"

            if script_result == "compensated":
                record = await self._load_reservation(reservation_id)
                await self._credit_reservation_amount(record, reason)
                WALLET_ROLLBACK_COUNTER.labels(status="success").inc()
                self._cancel_watchdog(reservation_id)
                logger.info(
                    "Reservation %s compensated due to %s",
                    reservation_id,
                    reason,
                )
                return True, "Reservation compensated"

            if script_result == "rolled_back":
                WALLET_ROLLBACK_COUNTER.labels(status="success").inc()
                return True, "Reservation already rolled back"

            WALLET_ROLLBACK_COUNTER.labels(status="error").inc()
            logger.error(
                "Unexpected rollback result %s for reservation_id=%s",
                script_result,
                reservation_id,
            )
            return False, f"Unable to rollback reservation ({script_result})"
        except Exception as exc:  # pragma: no cover - defensive logging
            WALLET_ROLLBACK_COUNTER.labels(status="error").inc()
            logger.exception(
                "Rollback failure for reservation_id=%s", reservation_id
            )
            return False, f"Rollback error: {exc}"
        finally:
            WALLET_OPERATION_DURATION.labels(operation="rollback").observe(
                time.perf_counter() - start_time
            )

    async def _credit_reservation_amount(
        self, record: Optional[ReservationRecord], reason: str
    ) -> None:
        if record is None:
            logger.error("Cannot credit reservation – record missing")
            return

        try:
            await self._with_timeout(
                self._wallet_repository.credit(
                    record.user_id,
                    record.chat_id,
                    record.amount,
                    metadata={"reason": reason, **dict(record.metadata)},
                ),
                self._wallet_timeout,
                "wallet_credit_on_rollback",
            )
        except Exception as exc:
            await self._handle_refund_failure(record, exc, reason)

    async def _handle_refund_failure(
        self, record: ReservationRecord, error: Exception, reason: str
    ) -> None:
        WALLET_DLQ_COUNTER.inc()
        logger.error(
            "Refund failed for reservation_id=%s – routing to DLQ",
            record.reservation_id,
            exc_info=True,
        )

        if self._dlq is None:
            return

        payload = {
            "reservation_id": record.reservation_id,
            "user_id": record.user_id,
            "chat_id": record.chat_id,
            "amount": record.amount,
            "error": str(error),
            "reason": reason,
            "metadata": dict(record.metadata),
            "timestamp": time.time(),
        }

        try:
            await self._with_timeout(
                self._dlq.push(payload),
                self._redis_timeout,
                "dlq_push",
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Failed to push reservation to DLQ", extra=payload)

    async def _load_reservation(
        self, reservation_id: str
    ) -> Optional[ReservationRecord]:
        try:
            raw = await self._with_timeout(
                self._redis.hgetall(self._reservation_key(reservation_id)),
                self._redis_timeout,
                "redis_load_reservation",
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Failed to load reservation %s", reservation_id)
            return None

        if not raw:
            return None

        metadata_str = raw.get("metadata")
        metadata: Mapping[str, Any]
        if isinstance(metadata_str, bytes):
            metadata_str = metadata_str.decode("utf-8", "ignore")
        try:
            metadata = json.loads(metadata_str) if metadata_str else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}

        try:
            amount = int(raw.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0

        try:
            created_at = float(raw.get("created_at", 0.0))
        except (TypeError, ValueError):
            created_at = 0.0

        status_value = raw.get("status", ReservationStatus.PENDING.value)
        if isinstance(status_value, bytes):
            status_value = status_value.decode("utf-8", "ignore")

        try:
            status = ReservationStatus(status_value)
        except ValueError:
            status = ReservationStatus.PENDING

        user_value = raw.get("user_id", 0)
        chat_value = raw.get("chat_id", 0)
        try:
            user_id = int(user_value)
        except (TypeError, ValueError):
            user_id = 0
        try:
            chat_id = int(chat_value)
        except (TypeError, ValueError):
            chat_id = 0

        return ReservationRecord(
            reservation_id=reservation_id,
            user_id=user_id,
            chat_id=chat_id,
            amount=amount,
            status=status,
            metadata=metadata,
            created_at=created_at,
        )

    def _schedule_watchdog(self, reservation_id: str) -> None:
        if reservation_id in self._watchdogs:
            return
        self._watchdogs[reservation_id] = asyncio.create_task(
            self._auto_rollback(reservation_id)
        )

    def _cancel_watchdog(self, reservation_id: str) -> None:
        task = self._watchdogs.pop(reservation_id, None)
        if task is not None:
            task.cancel()

    async def _auto_rollback(self, reservation_id: str) -> None:
        try:
            await asyncio.sleep(self._reservation_ttl)
            await self.rollback_reservation(
                reservation_id,
                "timeout",
                allow_committed=False,
            )
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise
        except Exception:  # pragma: no cover - watchdog safety net
            logger.exception(
                "Auto rollback failed for reservation_id=%s", reservation_id
            )

    def _reservation_key(self, reservation_id: str) -> str:
        return self._RESERVATION_KEY_TEMPLATE.format(reservation_id=reservation_id)

    async def _with_timeout(
        self, awaitable: Awaitable[Any], timeout: float, label: str
    ) -> Any:
        try:
            if hasattr(asyncio, "timeout"):
                async with asyncio.timeout(timeout):  # type: ignore[attr-defined]
                    return await awaitable
            return await asyncio.wait_for(awaitable, timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Operation timed out: {label}") from exc

