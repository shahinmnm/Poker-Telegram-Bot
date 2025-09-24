"""Core game engine utilities for PokerBot.

State machine overview (mirrors :func:`_progress_stage_locked`):

    WAITING ──start_game()──▶ ROUND_PRE_FLOP ─┬─▶ ROUND_FLOP ─┬─▶ ROUND_TURN ─┬─▶ ROUND_RIVER
      ▲                                       │               │               │
      └──────── finalize_game() ◀─────────────┴───────────────┴───────────────┘

`finalize_game` also handles early exits when fewer than two contenders remain
or a table is stopped. A more detailed, annotated diagram lives in
``docs/game_flow.md`` for onboarding and design reference.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

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
from pokerapp.config import get_game_constants
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.lock_manager import LockManager
from pokerapp.table_manager import TableManager
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics
from pokerapp.utils.telegram_safeops import TelegramSafeOps
from pokerapp.matchmaking_service import MatchmakingService
from pokerapp.player_manager import PlayerManager
from pokerapp.stats_reporter import StatsReporter
from pokerapp.stats import PlayerIdentity
from pokerapp.winnerdetermination import (
    HAND_NAMES_TRANSLATIONS,
    HandsOfPoker,
    WinnerDetermination,
)


_CONSTANTS = get_game_constants()
_GAME_CONSTANTS = _CONSTANTS.game
_ENGINE_CONSTANTS = _CONSTANTS.engine
_AUTO_START_DEFAULTS = _GAME_CONSTANTS.get("auto_start", {})


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


class GameEngine:
    """Coordinates game-level constants and helpers."""

    ACTIVE_GAME_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }

    _MAX_TIME_FOR_TURN_SECONDS = _non_negative_float(
        _GAME_CONSTANTS.get("max_time_for_turn_seconds"),
        120.0,
    )
    MAX_TIME_FOR_TURN = datetime.timedelta(seconds=_MAX_TIME_FOR_TURN_SECONDS)

    AUTO_START_MAX_UPDATES_PER_MINUTE = _positive_int(
        _AUTO_START_DEFAULTS.get("max_updates_per_minute"),
        20,
    )
    _AUTO_START_INTERVAL_DEFAULT = 60 / AUTO_START_MAX_UPDATES_PER_MINUTE
    AUTO_START_MIN_UPDATE_INTERVAL = datetime.timedelta(
        seconds=_non_negative_float(
            _AUTO_START_DEFAULTS.get("min_update_interval_seconds"),
            _AUTO_START_INTERVAL_DEFAULT,
        )
    )
    KEY_START_COUNTDOWN_LAST_TEXT = _ENGINE_CONSTANTS.get(
        "key_start_countdown_last_text",
        "start_countdown_last_text",
    )
    KEY_START_COUNTDOWN_LAST_TIMESTAMP = _ENGINE_CONSTANTS.get(
        "key_start_countdown_last_timestamp",
        "start_countdown_last_timestamp",
    )
    KEY_START_COUNTDOWN_CONTEXT = _ENGINE_CONSTANTS.get(
        "key_start_countdown_context",
        "start_countdown_context",
    )
    KEY_STOP_REQUEST = _ENGINE_CONSTANTS.get("key_stop_request", "stop_request")
    STOP_CONFIRM_CALLBACK = _ENGINE_CONSTANTS.get(
        "stop_confirm_callback",
        "stop:confirm",
    )
    STOP_RESUME_CALLBACK = _ENGINE_CONSTANTS.get(
        "stop_resume_callback",
        "stop:resume",
    )

    def __init__(
        self,
        *,
        table_manager: TableManager,
        view: PokerBotViewer,
        winner_determination: WinnerDetermination,
        request_metrics: RequestMetrics,
        round_rate: Any,
        player_manager: PlayerManager,
        matchmaking_service: MatchmakingService,
        stats_reporter: StatsReporter,
        clear_game_messages: Callable[[Game, ChatId], Awaitable[None]],
        build_identity_from_player: Callable[[Player], PlayerIdentity],
        safe_int: Callable[[ChatId], int],
        old_players_key: str,
        telegram_safe_ops: TelegramSafeOps,
        lock_manager: LockManager,
        logger: logging.Logger,
    ) -> None:
        self._table_manager = table_manager
        self._view = view
        self._winner_determination = winner_determination
        self._request_metrics = request_metrics
        self._round_rate = round_rate
        self._player_manager = player_manager
        self._matchmaking_service = matchmaking_service
        self._stats_reporter = stats_reporter
        self._clear_game_messages = clear_game_messages
        self._build_identity_from_player = build_identity_from_player
        self._safe_int = safe_int
        self._old_players_key = old_players_key
        self._telegram_ops = telegram_safe_ops
        self._lock_manager = lock_manager
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

    def _stage_lock_key(self, chat_id: ChatId) -> str:
        return f"stage:{self._safe_int(chat_id)}"

    async def start_game(
        self, context: ContextTypes.DEFAULT_TYPE, game: Game, chat_id: ChatId
    ) -> None:
        """Begin a poker hand, delegating to the matchmaking service."""

        await self._matchmaking_service.start_game(
            context=context,
            game=game,
            chat_id=chat_id,
            build_identity_from_player=self._build_identity_from_player,
        )

    def hand_type_to_label(self, hand_type: Optional[HandsOfPoker]) -> Optional[str]:
        if not hand_type:
            return None
        translation = HAND_NAMES_TRANSLATIONS.get(hand_type, {})
        label = translation.get("fa") or translation.get("en")
        if not label:
            label = hand_type.name.replace("_", " ").title()
        emoji = translation.get("emoji")
        if emoji:
            return f"{emoji} {label}"
        return label

    async def add_cards_to_table(
        self,
        count: int,
        game: Game,
        chat_id: ChatId,
        street_name: str,
        send_message: bool = True,
    ) -> None:
        await self._matchmaking_service.add_cards_to_table(
            count=count,
            game=game,
            chat_id=chat_id,
            street_name=street_name,
            send_message=send_message,
        )

    async def progress_stage(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: ChatId,
        game: Game,
    ) -> bool:
        async with self._lock_manager.guard(
            self._stage_lock_key(chat_id), timeout=10
        ):
            return await self._matchmaking_service.progress_stage(
                context=context,
                chat_id=chat_id,
                game=game,
                finalize_game=self.finalize_game,
            )

    async def finalize_game(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        async with self._lock_manager.guard(
            self._stage_lock_key(chat_id), timeout=10
        ):
            async def _send_with_retry(
                func: Callable[..., Awaitable[None]],
                *args: object,
                retries: int = 3,
            ) -> None:
                for attempt in range(retries):
                    try:
                        await func(*args)
                        return
                    except RetryAfter as exc:
                        await asyncio.sleep(exc.retry_after)
                    except Exception as exc:  # pragma: no cover - defensive logging
                        self._logger.error(
                            "Error sending message attempt",
                            extra={
                                "error_type": type(exc).__name__,
                                "request_params": {"attempt": attempt + 1, "args": args},
                            },
                        )
                        if attempt + 1 >= retries:
                            return

            contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
            game.chat_id = chat_id

            await self._clear_game_messages(game, chat_id)

            hand_id = game.id
            pot_total = game.pot
            payouts: Dict[int, int] = defaultdict(int)
            hand_labels: Dict[int, Optional[str]] = {}

            if not contenders:
                active_players = game.players_by(states=(PlayerState.ACTIVE,))
                if len(active_players) == 1:
                    winner = active_players[0]
                    amount = pot_total
                    if amount > 0:
                        await winner.wallet.inc(amount)
                        payouts[self._safe_int(winner.user_id)] += amount
                    hand_labels[self._safe_int(winner.user_id)] = "پیروزی با فولد رقبا"
                    await self._view.send_message(
                        chat_id,
                        f"🏆 تمام بازیکنان دیگر فولد کردند! {winner.mention_markdown} برنده {amount}$ شد.",
                    )
            else:
                contender_details = self._evaluate_contender_hands(game, contenders)
                winners_by_pot = self._determine_winners(game, contender_details)

                for detail in contender_details:
                    player = detail.get("player")
                    if not player:
                        continue
                    label = self.hand_type_to_label(detail.get("hand_type"))
                    if label:
                        hand_labels[self._safe_int(player.user_id)] = label

                if winners_by_pot:
                    for pot in winners_by_pot:
                        pot_amount = pot.get("amount", 0)
                        winners_info = pot.get("winners", [])
                        if pot_amount > 0 and winners_info:
                            base_share, remainder = divmod(pot_amount, len(winners_info))
                            for index, winner in enumerate(winners_info):
                                player = winner.get("player")
                                if not player:
                                    continue
                                win_amount = base_share + (1 if index < remainder else 0)
                                if win_amount > 0:
                                    await player.wallet.inc(win_amount)
                                    payouts[self._safe_int(player.user_id)] += win_amount
                                winner_label = self.hand_type_to_label(
                                    winner.get("hand_type")
                                )
                                if (
                                    winner_label
                                    and self._safe_int(player.user_id) not in hand_labels
                                ):
                                    hand_labels[self._safe_int(player.user_id)] = winner_label
                else:
                    await self._view.send_message(
                        chat_id,
                        "ℹ️ هیچ برنده‌ای در این دست مشخص نشد. مشکلی در منطق بازی رخ داده است.",
                    )

                await _send_with_retry(
                    self._view.send_showdown_results, chat_id, game, winners_by_pot
                )

            await self._stats_reporter.hand_finished(
                game,
                chat_id,
                payouts=dict(payouts),
                hand_labels=hand_labels,
                pot_total=pot_total,
            )

            game.pot = 0
            game.state = GameState.FINISHED

            remaining_players = []
            for player in game.players:
                if await player.wallet.value() > 0:
                    remaining_players.append(player)
            context.chat_data[self._old_players_key] = [
                player.user_id for player in remaining_players
            ]

            await self._request_metrics.end_cycle(
                self._safe_int(chat_id), cycle_token=game.id
            )
            await self._player_manager.clear_player_anchors(game)
            game.reset()
            await self._table_manager.save_game(chat_id, game)

            await _send_with_retry(self._view.send_new_hand_ready_message, chat_id)
            await self._player_manager.send_join_prompt(game, chat_id)

    def _evaluate_contender_hands(
        self, game: Game, contenders: Iterable[Player]
    ) -> List[Dict[str, object]]:
        details: List[Dict[str, object]] = []
        for player in contenders:
            hand_type, score, best_hand_cards = self._winner_determination.get_hand_value(
                player.cards, game.cards_table
            )
            details.append(
                {
                    "player": player,
                    "total_bet": player.total_bet,
                    "score": score,
                    "hand_cards": best_hand_cards,
                    "hand_type": hand_type,
                }
            )
        return details

    def _determine_winners(
        self, game: Game, contender_details: List[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        if not contender_details or game.pot == 0:
            return []

        bet_tiers = sorted(
            list(
                set(detail["total_bet"] for detail in contender_details if detail["total_bet"] > 0)
            )
        )

        winners_by_pot: List[Dict[str, object]] = []
        last_bet_tier = 0
        calculated_pot_total = 0

        for tier in bet_tiers:
            tier_contribution = tier - last_bet_tier
            eligible_for_this_pot = [
                detail for detail in contender_details if detail["total_bet"] >= tier
            ]

            pot_size = tier_contribution * len(eligible_for_this_pot)
            calculated_pot_total += pot_size

            if pot_size > 0:
                best_score_in_pot = max(detail["score"] for detail in eligible_for_this_pot)

                pot_winners_info = [
                    {
                        "player": detail["player"],
                        "hand_cards": detail["hand_cards"],
                        "hand_type": detail["hand_type"],
                    }
                    for detail in eligible_for_this_pot
                    if detail["score"] == best_score_in_pot
                ]

                winners_by_pot.append({"amount": pot_size, "winners": pot_winners_info})

            last_bet_tier = tier

        discrepancy = game.pot - calculated_pot_total
        if discrepancy > 0 and winners_by_pot:
            winners_by_pot[0]["amount"] += discrepancy
        elif discrepancy < 0:
            self._logger.error(
                "Pot calculation mismatch",
                extra={
                    "chat_id": getattr(game, "chat_id", None),
                    "request_params": {
                        "game_pot": game.pot,
                        "calculated": calculated_pot_total,
                    },
                    "error_type": "PotMismatch",
                },
            )
            try:
                asyncio.create_task(
                    self._view.notify_admin(
                        {
                            "event": "pot_mismatch",
                            "game_pot": game.pot,
                            "calculated": calculated_pot_total,
                        }
                    )
                )
            except Exception:  # pragma: no cover - notify best effort
                pass

        if len(bet_tiers) == 1 and len(winners_by_pot) > 1:
            return [
                {
                    "amount": game.pot,
                    "winners": winners_by_pot[0]["winners"],
                }
            ]

        return winners_by_pot

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

        message_id = await self._telegram_ops.edit_message_text(
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

    async def confirm_stop_vote(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
        voter_id: UserId,
    ) -> None:
        """Register a confirmation vote and cancel the hand if approved."""

        stop_request = self._validate_stop_request(context=context, game=game)

        manager_id = context.chat_data.get("game_manager_id")
        active_ids = set(stop_request.get("active_players", []))
        votes: Set[UserId] = set(stop_request.get("votes", set()))

        self._validate_stop_voter(voter_id, active_ids, manager_id)

        updated_request = await self._update_votes_and_message(
            context=context,
            game=game,
            chat_id=chat_id,
            stop_request=stop_request,
            voter_id=voter_id,
            manager_id=manager_id,
            votes=votes,
        )

        await self._check_if_stop_passes(
            game=game,
            chat_id=chat_id,
            context=context,
            stop_request=updated_request,
            active_ids=active_ids,
        )

    async def resume_stop_vote(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        """Cancel a pending stop request and resume play."""

        stop_request = context.chat_data.get(self.KEY_STOP_REQUEST)
        if not stop_request or stop_request.get("game_id") != game.id:
            raise UserException("درخواست توقفی برای لغو وجود ندارد.")

        message_id = stop_request.get("message_id")
        context.chat_data.pop(self.KEY_STOP_REQUEST, None)

        resume_text = "✅ رأی به ادامه‌ی بازی داده شد. بازی ادامه می‌یابد."
        await self._telegram_ops.edit_message_text(
            chat_id,
            message_id,
            resume_text,
            reply_markup=None,
            request_category=RequestCategory.GENERAL,
        )

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

        await self._refund_players(players_snapshot, original_game_id)

        await self._finalize_stop_request(
            context=context,
            chat_id=chat_id,
            stop_request=stop_request,
        )

        await self._reset_game_state(game=game, chat_id=chat_id, context=context)

    def _validate_stop_request(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
    ) -> Dict[str, object]:
        stop_request = context.chat_data.get(self.KEY_STOP_REQUEST)
        if not stop_request or stop_request.get("game_id") != game.id:
            raise UserException("درخواست توف فعالی وجود ندارد.")
        return stop_request

    def _validate_stop_voter(
        self,
        voter_id: UserId,
        active_ids: Set[UserId],
        manager_id: Optional[UserId],
    ) -> None:
        if voter_id not in active_ids and voter_id != manager_id:
            raise UserException("تنها بازیکنان فعال یا مدیر می‌توانند رأی دهند.")

    async def _update_votes_and_message(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
        stop_request: Dict[str, object],
        voter_id: UserId,
        manager_id: Optional[UserId],
        votes: Set[UserId],
    ) -> Dict[str, object]:
        votes.add(voter_id)
        stop_request["votes"] = votes
        stop_request["manager_override"] = bool(
            manager_id and voter_id == manager_id
        )

        message_text = self.render_stop_request_message(
            game=game,
            stop_request=stop_request,
            context=context,
        )

        message_id = await self._telegram_ops.edit_message_text(
            chat_id,
            stop_request.get("message_id"),
            message_text,
            reply_markup=self.build_stop_request_markup(),
            request_category=RequestCategory.GENERAL,
        )
        stop_request["message_id"] = message_id
        context.chat_data[self.KEY_STOP_REQUEST] = stop_request
        return stop_request

    async def _check_if_stop_passes(
        self,
        *,
        game: Game,
        chat_id: ChatId,
        context: ContextTypes.DEFAULT_TYPE,
        stop_request: Dict[str, object],
        active_ids: Set[UserId],
    ) -> None:
        votes = set(stop_request.get("votes", set()))
        active_votes = len(votes & active_ids)
        required_votes = (len(active_ids) // 2) + 1 if active_ids else 0

        if stop_request.get("manager_override"):
            await self.cancel_hand(game, chat_id, context, stop_request)
            return

        if active_ids and active_votes >= required_votes:
            await self.cancel_hand(game, chat_id, context, stop_request)

    async def _refund_players(
        self, players: Iterable[Player], original_game_id: str
    ) -> None:
        player_list = list(players)
        for player in player_list:
            if player.wallet:
                await player.wallet.cancel(original_game_id)

        await self._stats_reporter.invalidate_players(
            player_list, event_type="hand_finished"
        )

    def _build_stop_cancellation_message(
        self, stop_request: Dict[str, object]
    ) -> str:
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

        return "\n".join([summary_line, details])

    async def _finalize_stop_request(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: ChatId,
        stop_request: Dict[str, object],
    ) -> None:
        message_text = self._build_stop_cancellation_message(stop_request)

        await self._telegram_ops.edit_message_text(
            chat_id,
            stop_request.get("message_id"),
            message_text,
            reply_markup=None,
            request_category=RequestCategory.GENERAL,
        )

        context.chat_data.pop(self.KEY_STOP_REQUEST, None)

    async def _reset_game_state(
        self,
        *,
        game: Game,
        chat_id: ChatId,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        game.pot = 0

        await self._request_metrics.end_cycle(
            self._safe_int(chat_id), cycle_token=game.id
        )
        await self._player_manager.clear_player_anchors(game)
        game.reset()
        await self._table_manager.save_game(chat_id, game)
        await self._view.send_message(chat_id, "🛑 بازی متوقف شد.")
