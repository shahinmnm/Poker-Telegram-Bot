"""Player identity management utilities for PokerBot."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

from telegram import Update, User
from telegram.ext import ContextTypes

import redis.asyncio as aioredis

from pokerapp.entities import ChatId, Game, Player
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.stats import BaseStatsService, NullStatsService, PlayerIdentity
from pokerapp.table_manager import TableManager
from pokerapp.utils.cache import AdaptivePlayerReportCache
from pokerapp.utils.player_report_cache import (
    PlayerReportCache as RedisPlayerReportCache,
)


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
        player_report_cache: AdaptivePlayerReportCache,
        shared_report_cache: Optional[RedisPlayerReportCache],
        shared_report_ttl: int,
        view: PokerBotViewer,
        build_private_menu: Callable[[], object],
        logger: logging.Logger,
    ) -> None:
        self._table_manager = table_manager
        self._kv = kv
        self._stats_service = stats_service
        self._player_report_cache = player_report_cache
        self._shared_report_cache = shared_report_cache
        self._shared_report_ttl = max(int(shared_report_ttl or 0), 0)
        self._view = view
        self._build_private_menu = build_private_menu
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

    async def send_statistics_report(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat.type != chat.PRIVATE:
            await self._view.send_message(
                chat.id,
                "ℹ️ برای مشاهده آمار دقیق، لطفاً در چت خصوصی ربات از دکمه «📊 آمار بازی» استفاده کنید.",
            )
            return

        await self.register_player_identity(user, private_chat_id=chat.id)

        if not self._stats_enabled():
            await self._view.send_message(
                chat.id,
                "⚙️ سیستم آمار در حال حاضر غیرفعال است. لطفاً بعداً دوباره تلاش کنید.",
                reply_markup=self._build_private_menu(),
            )
            return

        user_id_int = self._safe_int(user.id)

        formatted_report: Optional[str] = None
        if self._shared_report_cache is not None:
            cached_payload = await self._shared_report_cache.get_report(user_id_int)
            if isinstance(cached_payload, dict):
                formatted_report = cached_payload.get("formatted")
                if formatted_report:
                    await self._view.send_message(
                        chat.id,
                        formatted_report,
                        reply_markup=self._build_private_menu(),
                    )
                    return

        async def _load_report() -> Optional[Any]:
            return await self._stats_service.build_player_report(user_id_int)

        report = await self._player_report_cache.get_with_context(
            user_id_int,
            _load_report,
        )
        if report is None or (
            report.stats.total_games <= 0 and not report.recent_games
        ):
            await self._view.send_message(
                chat.id,
                "ℹ️ هنوز داده‌ای برای نمایش وجود ندارد. پس از شرکت در چند دست بازی دوباره تلاش کنید.",
                reply_markup=self._build_private_menu(),
            )
            return

        formatted = self._stats_service.format_report(report)
        if self._shared_report_cache is not None:
            await self._shared_report_cache.set_report(
                user_id_int,
                {"formatted": formatted},
                ttl_seconds=self._shared_report_ttl,
            )
        await self._view.send_message(
            chat.id,
            formatted,
            reply_markup=self._build_private_menu(),
        )

    async def send_wallet_balance(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat = update.effective_chat
        user = update.effective_user

        if chat.type == chat.PRIVATE:
            await self.register_player_identity(user, private_chat_id=chat.id)
        else:
            await self.register_player_identity(user)

        from pokerapp.pokerbotmodel import WalletManagerModel

        wallet = WalletManagerModel(user.id, self._kv)
        balance = await wallet.value()

        reply_markup = self._build_private_menu() if chat.type == chat.PRIVATE else None
        await self._view.send_message(
            chat.id,
            f"💰 موجودی فعلی شما: {balance}$",
            reply_markup=reply_markup,
        )

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

    async def _update_private_chat_id_in_active_game(
        self, player_id: int, private_chat_id: int
    ) -> None:
        """Locate active game containing player and update their private chat ID."""

        table_manager = self._table_manager
        if table_manager is None:
            return

        try:
            game = None
            chat_id: Optional[ChatId] = None

            tables = getattr(table_manager, "_tables", None)
            if isinstance(tables, dict):
                for candidate_chat_id, candidate_game in tables.items():
                    if candidate_game is None:
                        continue
                    for candidate_player in getattr(candidate_game, "players", []):
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

            if game is None:
                return

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

    async def _register_stats_identity(
        self,
        user: User,
        private_chat_id: Optional[int],
        display_name: Optional[str],
    ) -> None:
        """Register or update player identity in the stats service."""

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
            await self._update_private_chat_id_in_active_game(player_id, private_chat_id)

        await self._register_stats_identity(user, private_chat_id, display_name)

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
