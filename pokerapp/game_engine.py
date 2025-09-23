"""Core game engine utilities for PokerBot."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Awaitable, Callable

from telegram.ext import ContextTypes

from pokerapp.entities import ChatId, Game, GameState, Player
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.stats import BaseStatsService, NullStatsService, PlayerIdentity
from pokerapp.table_manager import TableManager
from pokerapp.utils.request_metrics import RequestMetrics
from pokerapp.winnerdetermination import WinnerDetermination


class GameEngine:
    """Coordinates game-level constants and helpers."""

    ACTIVE_GAME_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }

    MAX_TIME_FOR_TURN = datetime.timedelta(minutes=2)
    AUTO_START_MAX_UPDATES_PER_MINUTE = 20
    AUTO_START_MIN_UPDATE_INTERVAL = datetime.timedelta(
        seconds=60 / AUTO_START_MAX_UPDATES_PER_MINUTE
    )
    KEY_START_COUNTDOWN_LAST_TEXT = "start_countdown_last_text"
    KEY_START_COUNTDOWN_LAST_TIMESTAMP = "start_countdown_last_timestamp"
    KEY_START_COUNTDOWN_CONTEXT = "start_countdown_context"

    def __init__(
        self,
        *,
        table_manager: TableManager,
        view: PokerBotViewer,
        winner_determination: WinnerDetermination,
        request_metrics: RequestMetrics,
        round_rate: Any,
        stats_service: BaseStatsService,
        assign_role_labels: Callable[[Game], None],
        clear_player_anchors: Callable[[Game], Awaitable[None]],
        send_turn_message: Callable[[Game, Player, ChatId], Awaitable[None]],
        get_stage_lock: Callable[[ChatId], Awaitable[asyncio.Lock]],
        build_identity_from_player: Callable[[Player], PlayerIdentity],
        safe_int: Callable[[ChatId], int],
        old_players_key: str,
        logger: logging.Logger,
    ) -> None:
        self._table_manager = table_manager
        self._view = view
        self._winner_determination = winner_determination
        self._request_metrics = request_metrics
        self._round_rate = round_rate
        self._stats = stats_service
        self._assign_role_labels = assign_role_labels
        self._clear_player_anchors = clear_player_anchors
        self._send_turn_message = send_turn_message
        self._get_stage_lock = get_stage_lock
        self._build_identity_from_player = build_identity_from_player
        self._safe_int = safe_int
        self._old_players_key = old_players_key
        self._logger = logger

    @staticmethod
    def state_token(state: Any) -> str:
        """Return a token representing the provided state."""

        name = getattr(state, "name", None)
        if isinstance(name, str):
            return name
        value = getattr(state, "value", None)
        if isinstance(value, str):
            return value
        return str(state)

    def _stats_enabled(self) -> bool:
        return not isinstance(self._stats, NullStatsService)

    async def start_game(
        self, context: ContextTypes.DEFAULT_TYPE, game: Game, chat_id: ChatId
    ) -> None:
        """Begin a poker hand, assigning blinds and notifying players."""

        await self._cancel_ready_message(chat_id, game)

        # Ensure dealer_index is initialized before use
        if not hasattr(game, "dealer_index"):
            game.dealer_index = -1

        new_dealer_index = game.advance_dealer()
        if new_dealer_index == -1:
            new_dealer_index = game.next_occupied_seat(-1)
            game.dealer_index = new_dealer_index

        if game.dealer_index == -1:
            self._logger.warning("Cannot start game without an occupied dealer seat")
            return

        if self._stats_enabled():
            identities = [
                self._build_identity_from_player(player)
                for player in game.seated_players()
            ]
            await self._stats.start_hand(
                hand_id=game.id,
                chat_id=self._safe_int(chat_id),
                players=identities,
            )

        game.state = GameState.ROUND_PRE_FLOP
        await self._request_metrics.start_cycle(
            self._safe_int(chat_id), game.id
        )

        if game.seat_announcement_message_id:
            try:
                await self._view.delete_message(
                    chat_id, game.seat_announcement_message_id
                )
            except Exception as exc:  # pragma: no cover - logging path
                self._logger.debug(
                    "Failed to delete seat announcement",
                    extra={
                        "chat_id": chat_id,
                        "message_id": game.seat_announcement_message_id,
                        "error_type": type(exc).__name__,
                    },
                )
            game.seat_announcement_message_id = None

        await self._clear_player_anchors(game)

        await self._divide_cards(game, chat_id)

        current_player = await self._round_rate.set_blinds(game, chat_id)
        self._assign_role_labels(game)

        game.chat_id = chat_id

        stage_lock = await self._get_stage_lock(chat_id)
        async with stage_lock:
            await self._view.send_player_role_anchors(game=game, chat_id=chat_id)

        action_str = "بازی شروع شد"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 5:
            game.last_actions.pop(0)
        if current_player:
            await self._send_turn_message(
                game,
                current_player,
                chat_id,
            )

        context.chat_data[self._old_players_key] = [
            p.user_id for p in game.players
        ]

    async def _cancel_ready_message(self, chat_id: ChatId, game: Game) -> None:
        self._logger.info(
            "[Game] start_hand invoked",
            extra={
                "chat_id": chat_id,
                "game_id": getattr(game, "id", None),
            },
        )
        if not game.ready_message_main_id:
            return

        deleted_ready_message = False
        try:
            await self._view.delete_message(chat_id, game.ready_message_main_id)
            deleted_ready_message = True
        except Exception as exc:  # pragma: no cover - logging path
            self._logger.warning(
                "Failed to delete ready message",
                extra={
                    "chat_id": chat_id,
                    "message_id": game.ready_message_main_id,
                    "error_type": type(exc).__name__,
                },
            )
        if deleted_ready_message:
            game.ready_message_main_id = None
        game.ready_message_main_text = ""

    async def divide_cards(self, game: Game, chat_id: ChatId) -> None:
        """Distribute hole cards to players for the upcoming hand."""

        await self._divide_cards(game, chat_id)

    async def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        for player in game.seated_players():
            if len(game.remain_cards) < 2:
                await self._view.send_message(
                    chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود."
                )
                await self._request_metrics.end_cycle(
                    self._safe_int(chat_id), cycle_token=game.id
                )
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards
