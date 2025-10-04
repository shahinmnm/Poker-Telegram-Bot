"""Immutable snapshots for post-lock messaging operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class StageProgressSnapshot:
    """Frozen state captured inside stage lock for deferred messaging."""

    chat_id: int
    pot: int
    stage: str
    community_cards: Tuple[str, ...]
    message_ids_to_delete: Tuple[int, ...]
    new_message_text: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict for future task queue migration."""

        return {
            "chat_id": self.chat_id,
            "pot": self.pot,
            "stage": self.stage,
            "community_cards": list(self.community_cards),
            "message_ids_to_delete": list(self.message_ids_to_delete),
            "new_message_text": self.new_message_text,
        }


@dataclass(frozen=True)
class FinalizationSnapshot:
    """Frozen state for post-lock game cleanup."""

    chat_id: int
    winner_user_id: int
    winner_username: str
    pot: int
    winning_hand: str
    message_ids_to_delete: Tuple[int, ...]
    stats_payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "winner_user_id": self.winner_user_id,
            "winner_username": self.winner_username,
            "pot": self.pot,
            "winning_hand": self.winning_hand,
            "message_ids_to_delete": list(self.message_ids_to_delete),
            "stats_payload": self.stats_payload,
        }
