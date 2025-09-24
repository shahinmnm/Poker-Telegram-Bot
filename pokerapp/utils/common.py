"""Shared helpers for cross-cutting utility logic."""

from __future__ import annotations

from typing import Any, Iterable, Set


def normalize_player_ids(players: Iterable[Any]) -> Set[int]:
    """Return a set of integer user identifiers extracted from ``players``.

    ``players`` may contain integers directly or objects exposing a ``user_id``
    attribute.  Any falsy or non-numeric identifiers are ignored so callers can
    pass raw player entities, partially populated DTOs or simple integers
    without worrying about sanitising the data up-front.
    """

    normalized: Set[int] = set()
    for entry in players:
        user_id = entry if isinstance(entry, int) else getattr(entry, "user_id", None)
        if user_id is None:
            continue
        try:
            parsed = int(user_id)
        except (TypeError, ValueError):
            continue
        if parsed:
            normalized.add(parsed)
    return normalized


__all__ = ["normalize_player_ids"]

