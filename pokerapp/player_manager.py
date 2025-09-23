"""Player identity management utilities for PokerBot."""

from __future__ import annotations

import inspect
import logging
from typing import Dict, List, Optional

from telegram import User

import redis.asyncio as aioredis

from pokerapp.entities import ChatId, Game, Player
from pokerapp.stats import BaseStatsService, NullStatsService, PlayerIdentity
from pokerapp.table_manager import TableManager


class PlayerManager:
    """Handle player identity bookkeeping and role labeling."""

    ROLE_TRANSLATIONS = {
        "dealer": "دیلر",
        "small_blind": "بلایند کوچک",
        "big_blind": "بلایند بزرگ",
        "player": "بازیکن",
    }

    def __init__(
        self,
        *,
        table_manager: TableManager,
        kv: aioredis.Redis,
        stats_service: BaseStatsService,
        logger: logging.Logger,
    ) -> None:
        self._table_manager = table_manager
        self._kv = kv
        self._stats_service = stats_service
        self._logger = logger
        self._private_chat_ids: Dict[int, int] = {}

    @staticmethod
    def _safe_int(value: object) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    @property
    def private_chat_ids(self) -> Dict[int, int]:
        return self._private_chat_ids

    def _stats_enabled(self) -> bool:
        return not isinstance(self._stats_service, NullStatsService)

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

            roles: List[str] = []
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
                "Assigned role_label: player=%s seat=%s role=%s",
                display_name,
                seat_number,
                role_label,
            )

    async def register_player_identity(
        self,
        user: User,
        *,
        private_chat_id: Optional[int] = None,
        display_name: Optional[str] = None,
    ) -> None:
        player_id = self._safe_int(user.id)
        if private_chat_id:
            self._private_chat_ids[player_id] = private_chat_id

            table_manager = self._table_manager
            if table_manager is not None:
                try:
                    game = None
                    chat_id: Optional[ChatId] = None

                    tables = getattr(table_manager, "_tables", None)
                    if isinstance(tables, dict):
                        for candidate_chat_id, candidate_game in tables.items():
                            if candidate_game is None:
                                continue
                            players = getattr(candidate_game, "players", [])
                            for candidate_player in players:
                                if getattr(candidate_player, "user_id", None) == player_id:
                                    game = candidate_game
                                    chat_id = candidate_chat_id
                                    break
                            if game is not None:
                                break

                    if game is None:
                        finder = getattr(table_manager, "find_game_by_user", None)
                        if finder is not None:
                            try:
                                result = finder(player_id)
                                if inspect.isawaitable(result):
                                    game, chat_id = await result
                                elif result:
                                    game, chat_id = result
                            except LookupError:
                                game = None
                                chat_id = None

                    if game is not None:
                        updated = False
                        for player in getattr(game, "players", []):
                            if getattr(player, "user_id", None) == player_id:
                                if getattr(player, "private_chat_id", None) != private_chat_id:
                                    player.private_chat_id = private_chat_id
                                    updated = True
                                break

                        if updated and chat_id is not None:
                            saver = getattr(table_manager, "save_game", None)
                            if saver is not None:
                                try:
                                    save_result = saver(chat_id, game)
                                    if inspect.isawaitable(save_result):
                                        await save_result
                                except Exception:
                                    self._logger.exception(
                                        "Failed to persist game after updating private chat id",
                                        extra={"chat_id": chat_id, "user_id": player_id},
                                    )
                except Exception:
                    self._logger.exception(
                        "Failed to update player private chat id in active game",
                        extra={"user_id": player_id},
                    )
        if not self._stats_enabled():
            return
        identity = PlayerIdentity(
            user_id=self._safe_int(user.id),
            display_name=display_name
            or user.full_name
            or user.first_name
            or str(user.id),
            username=user.username,
            full_name=user.full_name,
            private_chat_id=private_chat_id,
        )
        await self._stats_service.register_player_profile(identity)

    def build_identity_from_player(self, player: Player) -> PlayerIdentity:
        display_name = getattr(player, "display_name", None) or player.mention_markdown
        username = getattr(player, "username", None)
        full_name = getattr(player, "full_name", None)
        private_chat_id = getattr(player, "private_chat_id", None)
        return PlayerIdentity(
            user_id=self._safe_int(player.user_id),
            display_name=display_name,
            username=username,
            full_name=full_name,
            private_chat_id=private_chat_id,
        )
