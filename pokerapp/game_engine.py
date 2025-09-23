"""Core game engine utilities for PokerBot."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.helpers import mention_markdown as format_mention_markdown

from pokerapp.entities import (
    ChatId,
    Game,
    GameState,
    MessageId,
    Player,
    PlayerState,
    UserException,
    UserId,
)
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.stats import BaseStatsService, NullStatsService, PlayerIdentity
from pokerapp.table_manager import TableManager
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics
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
    KEY_STOP_REQUEST = "stop_request"
    STOP_CONFIRM_CALLBACK = "stop:confirm"
    STOP_RESUME_CALLBACK = "stop:resume"

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
        safe_edit_message_text: Callable[..., Awaitable[Optional[MessageId]]],
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
        self._safe_edit_message_text = safe_edit_message_text
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

    async def stop_game(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
        requester_id: UserId,
    ) -> None:
        """Validate and submit a stop request for the active hand."""

        if game.state == GameState.INITIAL:
            raise UserException("بازی فعالی برای توقف وجود ندارد.")

        if not any(player.user_id == requester_id for player in game.seated_players()):
            raise UserException("فقط بازیکنان حاضر می‌توانند درخواست توقف بدهند.")

        await self.request_stop(
            context=context,
            game=game,
            chat_id=chat_id,
            requester_id=requester_id,
        )
        await self._table_manager.save_game(chat_id, game)

    async def request_stop(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
        requester_id: UserId,
    ) -> None:
        """Create or update a stop request vote and announce it to the chat."""

        active_players = [
            player
            for player in game.seated_players()
            if player.state in (PlayerState.ACTIVE, PlayerState.ALL_IN)
        ]
        if not active_players:
            raise UserException("هیچ بازیکن فعالی برای رأی‌گیری وجود ندارد.")

        stop_request = context.chat_data.get(self.KEY_STOP_REQUEST)
        if not stop_request or stop_request.get("game_id") != game.id:
            stop_request = {
                "game_id": game.id,
                "active_players": [player.user_id for player in active_players],
                "votes": set(),
                "initiator": requester_id,
                "message_id": None,
                "manager_override": False,
            }
        else:
            stop_request.setdefault("votes", set())
            stop_request.setdefault("active_players", [])
            stop_request.setdefault("manager_override", False)
            stop_request["active_players"] = [
                player.user_id for player in active_players
            ]

        votes: Set[UserId] = set(stop_request.get("votes", set()))
        if requester_id in stop_request["active_players"]:
            votes.add(requester_id)
        stop_request["votes"] = votes

        message_text = self.render_stop_request_message(
            game=game,
            stop_request=stop_request,
            context=context,
        )

        message_id = await self._safe_edit_message_text(
            chat_id,
            stop_request.get("message_id"),
            message_text,
            reply_markup=self.build_stop_request_markup(),
            request_category=RequestCategory.GENERAL,
        )
        stop_request["message_id"] = message_id
        context.chat_data[self.KEY_STOP_REQUEST] = stop_request

    def build_stop_request_markup(self) -> InlineKeyboardMarkup:
        """Return the inline keyboard used for stop confirmations."""

        keyboard = [
            [
                InlineKeyboardButton(
                    text="تأیید توقف", callback_data=self.STOP_CONFIRM_CALLBACK
                ),
                InlineKeyboardButton(
                    text="ادامه بازی", callback_data=self.STOP_RESUME_CALLBACK
                ),
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    def render_stop_request_message(
        self,
        *,
        game: Game,
        stop_request: Dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> str:
        """Build the Markdown message describing the current stop vote."""

        active_ids = set(stop_request.get("active_players", []))
        votes = set(stop_request.get("votes", set()))
        active_players = [
            player
            for player in game.seated_players()
            if player.user_id in active_ids
        ]
        initiator_id = stop_request.get("initiator")
        initiator_player = next(
            (player for player in game.seated_players() if player.user_id == initiator_id),
            None,
        )
        if initiator_player:
            initiator_text = initiator_player.mention_markdown
        else:
            initiator_text = format_mention_markdown(initiator_id, str(initiator_id))

        manager_id = context.chat_data.get("game_manager_id")
        manager_player = None
        if manager_id:
            manager_player = next(
                (player for player in game.seated_players() if player.user_id == manager_id),
                None,
            )

        required_votes = (len(active_players) // 2) + 1 if active_players else 0
        confirmed_votes = len(votes & {player.user_id for player in active_players})

        active_lines = []
        for player in active_players:
            mark = "✅" if player.user_id in votes else "⬜️"
            active_lines.append(f"{mark} {player.mention_markdown}")
        if not active_lines:
            active_lines.append("—")

        lines = [
            "🛑 *درخواست توقف بازی*",
            f"درخواست توسط {initiator_text}",
            "",
            "بازیکنان فعال:",
            *active_lines,
            "",
        ]

        if active_players:
            lines.append(f"آراء تأیید: {confirmed_votes}/{required_votes}")
        else:
            lines.append("آراء تأیید: 0/0")

        if manager_player:
            lines.extend(
                [
                    "",
                    f"👤 مدیر بازی: {manager_player.mention_markdown}",
                    "او می‌تواند به تنهایی رأی توقف را تأیید کند.",
                ]
            )

        if votes - {player.user_id for player in active_players}:
            extra_voters = votes - {player.user_id for player in active_players}
            voter_mentions = []
            for voter_id in extra_voters:
                player = next(
                    (p for p in game.seated_players() if p.user_id == voter_id),
                    None,
                )
                if player:
                    voter_mentions.append(player.mention_markdown)
                else:
                    voter_mentions.append(
                        format_mention_markdown(voter_id, str(voter_id))
                    )
            lines.extend(
                [
                    "",
                    "رأی سایر افراد:",
                    *voter_mentions,
                ]
            )

        return "\n".join(lines)

    async def cancel_hand(
        self,
        game: Game,
        chat_id: ChatId,
        context: ContextTypes.DEFAULT_TYPE,
        stop_request: Dict[str, object],
    ) -> None:
        """Cancel the current hand, refund players, and reset the game."""

        original_game_id = game.id
        players_snapshot = list(game.seated_players())

        for player in players_snapshot:
            if player.wallet:
                await player.wallet.cancel(original_game_id)

        game.pot = 0

        active_ids = set(stop_request.get("active_players", []))
        votes = set(stop_request.get("votes", set()))
        manager_override = stop_request.get("manager_override", False)

        approved_votes = len(votes & active_ids)
        required_votes = (len(active_ids) // 2) + 1 if active_ids else 0

        if manager_override:
            summary_line = "🛑 *مدیر بازی بازی را متوقف کرد.*"
        else:
            summary_line = "🛑 *بازی با رأی اکثریت متوقف شد.*"

        details = (
            f"آراء تأیید: {approved_votes}/{required_votes}"
            if active_ids
            else "هیچ رأی فعالی ثبت نشد."
        )

        await self._safe_edit_message_text(
            chat_id,
            stop_request.get("message_id"),
            "\n".join([summary_line, details]),
            reply_markup=None,
            request_category=RequestCategory.GENERAL,
        )

        context.chat_data.pop(self.KEY_STOP_REQUEST, None)

        await self._request_metrics.end_cycle(
            self._safe_int(chat_id), cycle_token=game.id
        )
        await self._clear_player_anchors(game)
        game.reset()
        await self._table_manager.save_game(chat_id, game)
        await self._view.send_message(chat_id, "🛑 بازی متوقف شد.")
