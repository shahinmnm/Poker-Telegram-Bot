from __future__ import annotations

"""Minimal ORM stubs required for wallet operations in tests."""

from dataclasses import dataclass


@dataclass
class Player:
    """Simplified player model holding chip balance."""

    user_id: int
    chat_id: int
    chips: int
