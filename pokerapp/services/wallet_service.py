"""Wallet service helpers used by gameplay handlers."""

from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

import redis.asyncio as aioredis

from pokerapp.entities import Money, UserException

if TYPE_CHECKING:  # pragma: no cover - typing aid only
    from pokerapp.pokerbotmodel import WalletManagerModel


class WalletService:
    """High level wallet operations with redis-backed persistence."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        *,
        wallet_factory: Optional[Callable[[int], "WalletManagerModel"]] = None,
    ) -> None:
        self._redis = redis_client
        self._wallet_factory: Callable[[int], "WalletManagerModel"]
        if wallet_factory is None:
            self._wallet_factory = self._default_wallet_factory
        else:
            self._wallet_factory = wallet_factory

    def _default_wallet_factory(self, user_id: int):
        from pokerapp.pokerbotmodel import WalletManagerModel  # local import to avoid cycles

        return WalletManagerModel(user_id, self._redis)

    def _resolve_wallet(self, user_id: int):
        return self._wallet_factory(int(user_id))

    async def deduct_chips(self, user_id: int, amount: Money) -> int:
        """Deduct ``amount`` chips from ``user_id``'s balance."""

        if amount < 0:
            raise ValueError("Amount to deduct must be non-negative")

        wallet = self._resolve_wallet(user_id)
        if amount == 0:
            balance = await wallet.value()
            return int(balance)

        try:
            balance = await wallet.dec(int(amount))
        except UserException:
            # Propagate user-facing validation errors without modification.
            raise

        return int(balance)

    async def credit_chips(self, user_id: int, amount: Money) -> int:
        """Increase ``user_id``'s balance by ``amount`` chips."""

        if amount < 0:
            raise ValueError("Amount to credit must be non-negative")

        wallet = self._resolve_wallet(user_id)
        if amount == 0:
            balance = await wallet.value()
            return int(balance)

        balance = await wallet.inc(int(amount))
        return int(balance)

    async def get_balance(self, user_id: int) -> int:
        """Return the current balance for ``user_id``."""

        wallet = self._resolve_wallet(user_id)
        balance = await wallet.value()
        return int(balance)
