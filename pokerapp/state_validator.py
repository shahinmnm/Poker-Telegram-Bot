"""Validation and recovery helpers for persisted game state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from pokerapp.cards import get_cards
from pokerapp.entities import Game, GameState, PlayerState


class ValidationIssue(str, Enum):
    """Enumerates problems detected while validating a persisted game."""

    CORRUPTED_JSON = "corrupted_json"
    INVALID_STAGE = "invalid_stage"
    ORPHANED_PLAYERS = "orphaned_players"
    INCONSISTENT_POT = "inconsistent_pot"
    MISSING_DEALER = "missing_dealer"
    INVALID_DECK = "invalid_deck"


@dataclass(slots=True)
class ValidationResult:
    """Outcome of validating a game snapshot."""

    is_valid: bool
    issues: List[ValidationIssue]
    recoverable: bool
    recovery_action: Optional[str] = None


class GameStateValidator:
    """Validate and recover ``Game`` instances loaded from persistence."""

    _RESET_ACTION = "reset_to_waiting"
    _DELETE_ACTION = "delete_and_recreate"

    def validate_game(self, game: Game) -> ValidationResult:
        """Inspect a game and report any validation issues."""

        issues: List[ValidationIssue] = []

        if not isinstance(getattr(game, "state", None), GameState):
            issues.append(ValidationIssue.INVALID_STAGE)

        state = getattr(game, "state", GameState.INITIAL)
        players = list(getattr(game, "players", []))

        if state is not GameState.INITIAL and not players:
            issues.append(ValidationIssue.ORPHANED_PLAYERS)

        if state is not GameState.INITIAL:
            dealer_index = getattr(game, "dealer_index", None)
            seats = getattr(game, "seats", [])
            dealer_valid = (
                isinstance(dealer_index, int)
                and 0 <= dealer_index < len(seats)
                and seats[dealer_index] is not None
            )
            if not dealer_valid:
                issues.append(ValidationIssue.MISSING_DEALER)

        pot = getattr(game, "pot", 0)
        if pot is None or pot < 0:
            issues.append(ValidationIssue.INCONSISTENT_POT)

        deck = getattr(game, "remain_cards", []) or []
        table_cards = getattr(game, "cards_table", []) or []
        player_count = len(players)
        if state is GameState.INITIAL:
            expected_remaining = 52
        else:
            expected_remaining = max(52 - (2 * player_count) - len(table_cards), 0)
        if len(deck) != expected_remaining:
            issues.append(ValidationIssue.INVALID_DECK)

        is_valid = not issues
        recoverable = True
        recovery_action: Optional[str] = None

        if ValidationIssue.INVALID_STAGE in issues:
            recoverable = False
            recovery_action = self._DELETE_ACTION
        elif issues:
            recovery_action = self._RESET_ACTION

        return ValidationResult(
            is_valid=is_valid,
            issues=issues,
            recoverable=recoverable,
            recovery_action=recovery_action,
        )

    def recover_game(self, game: Game, validation: ValidationResult) -> Game:
        """Apply recovery behaviour based on validation outcome."""

        if validation.is_valid or not validation.issues:
            return game

        if not validation.recoverable:
            return Game()

        return self._reset_to_waiting(game)

    def _reset_to_waiting(self, game: Game) -> Game:
        game.state = GameState.INITIAL
        game.pot = 0
        game.max_round_rate = 0
        game.cards_table = []
        game.remain_cards = get_cards()
        game.current_player_index = -1
        game.small_blind_index = -1
        game.big_blind_index = -1
        game.dealer_index = -1
        game.last_actions = []

        for player in game.players:
            player.cards = []
            player.round_rate = 0
            player.total_bet = 0
            player.has_acted = False
            player.state = PlayerState.ACTIVE
            player.is_dealer = False
            player.is_small_blind = False
            player.is_big_blind = False

        return game


__all__ = [
    "GameStateValidator",
    "ValidationIssue",
    "ValidationResult",
]
