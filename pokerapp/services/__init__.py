"""Service layer utilities for Poker Telegram Bot."""

from importlib import import_module
from typing import Any

__all__ = ["WalletService"]


def __getattr__(name: str) -> Any:
    if name == "WalletService":
        module = import_module("pokerapp.services.wallet_service")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

