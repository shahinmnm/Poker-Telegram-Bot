"""Lifecycle-oriented player management utilities for PokerBot."""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from pokerapp.entities import ChatId, Game, GameState, Player, UserId
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.table_manager import TableManager


class PlayerManager:
    """Coordinate player seating, role assignment, and anchor maintenance."""

    ROLE_TRANSLATIONS = {
        "dealer": "دیلر",
        "small_blind": "بلایند کوچک",
        "big_blind": "بلایند بزرگ",
        "player": "بازیکن",
    }

    def __init__(
        self,
        *,
        view: PokerBotViewer,
        table_manager: TableManager,
        logger: logging.Logger,
    ) -> None:
        self._view = view
        self._table_manager = table_manager
        self._logger = logger

    # ------------------------------------------------------------------
    # Seating helpers
    # ------------------------------------------------------------------
    def seat_player(self, game: Game, player: Player, *, seat_index: Optional[int] = None) -> int:
        """Place ``player`` into ``game`` at ``seat_index`` (or next available)."""

        assigned = game.add_player(player, seat_index=seat_index)
        self._logger.debug(
            "Player seated", extra={"user_id": getattr(player, "user_id", None), "seat": assigned}
        )
        return assigned

    def remove_player(self, game: Game, user_id: UserId) -> bool:
        """Remove the player with ``user_id`` from ``game`` seats."""

        removed = game.remove_player_by_user(user_id)
        if removed:
            self._logger.debug("Player removed", extra={"user_id": user_id})
        return removed

    def assign_role_labels(self, game: Game) -> None:
        """Assign localized role labels to players based on current blinds."""

        players = list(getattr(game, "players", []))
        if not players:
            return

        dealer_index = getattr(game, "dealer_index", -1)
        small_blind_index = getattr(game, "small_blind_index", -1)
        big_blind_index = getattr(game, "big_blind_index", -1)

        for player in players:
            seat_index = getattr(player, "seat_index", None)
            is_valid_seat = isinstance(seat_index, int) and seat_index >= 0

            is_dealer = is_valid_seat and seat_index == dealer_index
            is_small_blind = is_valid_seat and seat_index == small_blind_index
            is_big_blind = is_valid_seat and seat_index == big_blind_index

            roles: list[str] = []
            if is_dealer:
                roles.append(self.ROLE_TRANSLATIONS["dealer"])
            if is_small_blind:
                roles.append(self.ROLE_TRANSLATIONS["small_blind"])
            if is_big_blind:
                roles.append(self.ROLE_TRANSLATIONS["big_blind"])
            if not roles:
                roles.append(self.ROLE_TRANSLATIONS["player"])

            role_label = "، ".join(dict.fromkeys(roles))

            player.role_label = role_label
            player.anchor_role = role_label
            player.is_dealer = is_dealer
            player.is_small_blind = is_small_blind
            player.is_big_blind = is_big_blind

            seat_number = (seat_index + 1) if is_valid_seat else "?"
            display_name = getattr(player, "display_name", None) or getattr(
                player, "mention_markdown", getattr(player, "user_id", "?")
            )

            self._logger.debug(
                "Assigned role_label", extra={"player": display_name, "seat": seat_number, "role": role_label}
            )

    # ------------------------------------------------------------------
    # Anchor and prompt management
    # ------------------------------------------------------------------
    async def clear_player_anchors(self, game: Game) -> None:
        """Remove all persisted player anchors for the provided ``game``."""

        clear_method = getattr(self._view, "clear_all_player_anchors", None)
        if callable(clear_method):
            await clear_method(game)
            self._logger.debug("Cleared player anchors", extra={"game_id": getattr(game, "id", None)})

    async def send_join_prompt(self, game: Game, chat_id: ChatId) -> None:
        """Send the join prompt if it is not already visible."""

        if game.state != GameState.INITIAL or game.ready_message_main_id:
            return

        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game")]]
        )
        message_id = await self._view.send_message_return_id(
            chat_id,
            "برای نشستن سر میز دکمه را بزن",
            reply_markup=markup,
        )
        if message_id:
            game.ready_message_main_id = message_id
            game.ready_message_main_text = "برای نشستن سر میز دکمه را بزن"
            if self._table_manager is not None:
                await self._table_manager.save_game(chat_id, game)

    async def cleanup_ready_prompt(self, game: Game, chat_id: ChatId) -> None:
        """Delete the ready message if present and reset prompt metadata."""

        if not game.ready_message_main_id:
            return

        try:
            await self._view.delete_message(chat_id, game.ready_message_main_id)
        except Exception as exc:  # pragma: no cover - logging path
            self._logger.warning(
                "Failed to delete ready message",
                extra={
                    "chat_id": chat_id,
                    "message_id": game.ready_message_main_id,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            game.ready_message_main_id = None
        game.ready_message_main_text = ""

    async def clear_seat_announcement(self, game: Game, chat_id: ChatId) -> None:
        """Remove any outstanding seat announcement message for ``game``."""

        message_id = getattr(game, "seat_announcement_message_id", None)
        if not message_id:
            return

        try:
            await self._view.delete_message(chat_id, message_id)
        except Exception:  # pragma: no cover - best-effort cleanup
            self._logger.debug(
                "Failed to delete seat announcement",
                extra={"chat_id": chat_id, "message_id": message_id},
            )
        finally:
            game.seat_announcement_message_id = None

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    @staticmethod
    def extract_user_ids(players: Iterable[Player]) -> Iterable[int]:
        for player in players:
            yield int(getattr(player, "user_id", 0) or 0)
