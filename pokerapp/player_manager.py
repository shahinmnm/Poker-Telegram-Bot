"""Lifecycle-oriented player management utilities for PokerBot."""

from __future__ import annotations

import inspect
import logging
from typing import Iterable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from pokerapp.entities import ChatId, Game, GameState, Player, UserException, UserId
from pokerapp.config import get_game_constants
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.table_manager import TableManager
from pokerapp.utils.request_metrics import RequestCategory


_CONSTANTS = get_game_constants()
_ROLES_RESOURCE = _CONSTANTS.roles
if isinstance(_ROLES_RESOURCE, dict):
    _RAW_ROLE_TRANSLATIONS = _ROLES_RESOURCE.get("roles", {})
    if not isinstance(_RAW_ROLE_TRANSLATIONS, dict):
        _RAW_ROLE_TRANSLATIONS = {}
    _DEFAULT_LANGUAGE = _ROLES_RESOURCE.get("default_language", "fa")
    if not isinstance(_DEFAULT_LANGUAGE, str) or not _DEFAULT_LANGUAGE:
        _DEFAULT_LANGUAGE = "fa"
else:
    _RAW_ROLE_TRANSLATIONS = {}
    _DEFAULT_LANGUAGE = "fa"

_LANGUAGE_ORDER = tuple(dict.fromkeys([_DEFAULT_LANGUAGE, "fa", "en"]))


def _resolve_role_label(key: str, fallback: str) -> str:
    entry = _RAW_ROLE_TRANSLATIONS.get(key, {})
    if isinstance(entry, dict):
        for lang in _LANGUAGE_ORDER:
            text = entry.get(lang)
            if isinstance(text, str) and text:
                return text
    elif isinstance(entry, str) and entry:
        return entry
    return fallback


_ROLE_LABELS = {
    "dealer": _resolve_role_label("dealer", "Dealer"),
    "small_blind": _resolve_role_label("small_blind", "Small blind"),
    "big_blind": _resolve_role_label("big_blind", "Big blind"),
    "player": _resolve_role_label("player", "Player"),
}


class PlayerManager:
    """Coordinate player seating, role assignment, and anchor maintenance."""

    ROLE_TRANSLATIONS = _ROLE_LABELS

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
    async def reseat_players(self, game: Game) -> None:
        """Normalize seat assignments and persist the table layout."""

        total_seats = len(getattr(game, "seats", []))
        if total_seats == 0:
            return

        normalized_seats: list[Optional[Player]] = [None] * total_seats
        next_free_index = 0

        for player in list(game.players):
            seat_index = getattr(player, "seat_index", None)
            target_index: Optional[int]
            if (
                isinstance(seat_index, int)
                and 0 <= seat_index < total_seats
                and normalized_seats[seat_index] is None
            ):
                target_index = seat_index
            else:
                target_index = None

            if target_index is None:
                while next_free_index < total_seats and normalized_seats[next_free_index] is not None:
                    next_free_index += 1
                if next_free_index >= total_seats:
                    break
                target_index = next_free_index
                next_free_index += 1

            normalized_seats[target_index] = player
            player.seat_index = target_index

        game.seats = normalized_seats

        chat_id = getattr(game, "chat_id", None)
        if chat_id is None or self._table_manager is None:
            return

        save_method = getattr(self._table_manager, "save_game", None)
        if not callable(save_method):
            return

        maybe_coro = save_method(chat_id, game)
        if inspect.isawaitable(maybe_coro):
            await maybe_coro

    def seat_player(self, game: Game, player: Player, *, seat_index: Optional[int] = None) -> int:
        """Place ``player`` into ``game`` at ``seat_index`` (or next available) with validation and persistence."""

        user_id = getattr(player, "user_id", None)

        chat_id = getattr(game, "chat_id", None)
        if chat_id is None:
            for candidate_chat_id, candidate_game in getattr(self._table_manager, "_tables", {}).items():
                if candidate_game is game:
                    chat_id = candidate_chat_id
                    break

        def _record_last_seat_error(exc: Exception, *, message: str) -> None:
            redis_conn = getattr(self._table_manager, "_redis", None)
            if not redis_conn or chat_id is None:
                self._logger.error(
                    message,
                    extra={
                        "user_id": user_id,
                        "seat_index": seat_index,
                        "chat_id": chat_id,
                        "exception": str(exc),
                    },
                    exc_info=True,
                )
                return

            try:
                import json
                from datetime import datetime

                seated_count_getter = getattr(game, "seated_count", None)
                seated_count = None
                if callable(seated_count_getter):
                    try:
                        seated_count = seated_count_getter()
                    except Exception:  # noqa: BLE001 - best-effort retrieval only
                        seated_count = None

                payload = {
                    "user_id": user_id,
                    "seat": seat_index,
                    "seated_count": seated_count,
                    "chat_id": chat_id,
                    "exception": str(exc),
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
                redis_conn.set(f"chat:{chat_id}:last_seat_error", json.dumps(payload))
            except Exception as redis_exc:  # noqa: BLE001 - logging is best-effort
                self._logger.warning(
                    "Failed to record last_seat_error in Redis",
                    extra={"chat_id": chat_id, "error": str(redis_exc)},
                )

            self._logger.error(
                message,
                extra={
                    "user_id": user_id,
                    "seat_index": seat_index,
                    "chat_id": chat_id,
                    "exception": str(exc),
                },
                exc_info=True,
            )

        if user_id is None or (isinstance(user_id, str) and not user_id.strip()):
            self._logger.warning(
                "Rejected seating due to invalid user_id",
                extra={
                    "user_id": user_id,
                    "seat_index": seat_index,
                    "chat_id": chat_id,
                },
            )
            return -1

        existing_players = list(getattr(game, "players", []))
        if any(getattr(existing, "user_id", None) == user_id for existing in existing_players):
            self._logger.warning(
                "Rejected seating due to duplicate user_id",
                extra={
                    "user_id": user_id,
                    "seat_index": seat_index,
                    "chat_id": chat_id,
                },
            )
            return -1

        if getattr(player, "wallet", None) is None:
            wallet_redis = getattr(self._table_manager, "_wallet_redis", None) or getattr(
                self._table_manager, "_redis", None
            )
            try:
                from pokerapp.pokerbotmodel import WalletManagerModel

                player.wallet = WalletManagerModel(user_id, wallet_redis)
            except Exception as exc:  # noqa: BLE001 - ensure wallet failures are surfaced
                _record_last_seat_error(exc, message="Failed to assign wallet to player")
                raise

        try:
            assigned = game.add_player(player, seat_index=seat_index)
        except UserException as exc:
            _record_last_seat_error(exc, message="Failed to seat player due to user error")
            raise
        except Exception as exc:  # noqa: BLE001 - propagate unexpected errors with context
            _record_last_seat_error(exc, message="Unexpected error while seating player")
            raise

        self._logger.debug(
            "Player seated",
            extra={"user_id": user_id, "seat": assigned, "chat_id": chat_id},
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

        ready_message_id = getattr(game, "ready_message_main_id", None)
        players = list(getattr(game, "players", []))
        player_ready_ids = [
            getattr(player, "ready_message_id", None) for player in players
        ]
        has_player_ready = any(player_ready_ids)

        stored_game_id = getattr(game, "ready_message_game_id", None)
        current_game_id = getattr(game, "id", None)
        stale_prompt = False

        if ready_message_id is not None or has_player_ready:
            if stored_game_id is None or current_game_id is None:
                stale_prompt = True
            elif stored_game_id != current_game_id:
                stale_prompt = True

        if stale_prompt:
            for player in players:
                player.ready_message_id = None

            if getattr(game, "ready_users", None):
                game.ready_users.clear()
            remover = getattr(game, "remove_player_by_user", None)
            if callable(remover):
                for player in players:
                    if player is None:
                        continue
                    user_id = getattr(player, "user_id", None)
                    if user_id is not None:
                        remover(user_id)

            game.ready_message_main_id = None
            game.ready_message_main_text = ""
            game.ready_message_game_id = None
            game.ready_message_stage = None

            if self._table_manager is not None:
                save_method = getattr(self._table_manager, "save_game", None)
                if callable(save_method):
                    maybe_coro = save_method(chat_id, game)
                    if inspect.isawaitable(maybe_coro):
                        await maybe_coro

            self._logger.info(
                "Sent new ready prompt due to stale message",
                extra={"chat_id": chat_id, "game_id": current_game_id},
            )

            ready_message_id = None

        if not stale_prompt and (game.state != GameState.INITIAL or ready_message_id):
            return

        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game")]]
        )
        message_id = await self._view.send_message_return_id(
            chat_id,
            "برای نشستن سر میز دکمه را بزن",
            reply_markup=markup,
            request_category=RequestCategory.START_GAME,
        )
        if message_id:
            game.ready_message_main_id = message_id
            game.ready_message_main_text = "برای نشستن سر میز دکمه را بزن"
            game.ready_message_game_id = getattr(game, "id", None)
            game.ready_message_stage = game.state
            if self._table_manager is not None:
                await self._table_manager.save_game(chat_id, game)

    async def cleanup_ready_prompt(
        self, game: Game, chat_id: ChatId, *, persist: bool = True
    ) -> None:
        """Delete the ready message if present and reset prompt metadata."""

        state_changed = False
        message_id = getattr(game, "ready_message_main_id", None)
        if message_id:
            try:
                await self._view.delete_message(chat_id, message_id)
            except Exception as exc:  # pragma: no cover - logging path
                self._logger.warning(
                    "Failed to delete ready message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(exc).__name__,
                    },
                )

            state_changed = True

        players = getattr(game, "players", [])
        for player in players:
            player_message_id = getattr(player, "ready_message_id", None)
            if player_message_id:
                try:
                    await self._view.delete_message(chat_id, player_message_id)
                except Exception:  # pragma: no cover - best-effort cleanup
                    self._logger.debug(
                        "Failed to delete ready prompt for player",
                        extra={
                            "chat_id": chat_id,
                            "message_id": player_message_id,
                            "player_id": getattr(player, "user_id", None),
                        },
                    )
                state_changed = True
            player.ready_message_id = None

        if getattr(game, "ready_message_main_id", None) is not None:
            state_changed = True
        game.ready_message_main_id = None
        if getattr(game, "ready_message_game_id", None) is not None:
            state_changed = True
        game.ready_message_game_id = None
        if getattr(game, "ready_message_stage", None) is not None:
            state_changed = True
        game.ready_message_stage = None
        if getattr(game, "ready_message_main_text", None):
            state_changed = True
        game.ready_message_main_text = ""

        game_id = getattr(game, "id", None)
        self._logger.info(
            "Cleared ready prompt IDs for game %s",
            game_id,
            extra={"chat_id": chat_id, "game_id": game_id},
        )

        if persist and state_changed and self._table_manager is not None:
            save_method = getattr(self._table_manager, "save_game", None)
            if callable(save_method):
                maybe_coro = save_method(chat_id, game)
                if inspect.isawaitable(maybe_coro):
                    await maybe_coro

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
