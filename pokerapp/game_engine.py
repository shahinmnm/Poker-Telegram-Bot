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
from typing import (
    Any,
    Awaitable,
    Callable,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)
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
from pokerapp.config import GameConstants, get_game_constants
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.lock_manager import LockManager
from pokerapp.table_manager import TableManager
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics
from pokerapp.utils.telegram_safeops import TelegramSafeOps
from pokerapp.utils.cache import AdaptivePlayerReportCache
from pokerapp.utils.common import normalize_player_ids
from pokerapp.matchmaking_service import MatchmakingService
from pokerapp.player_manager import PlayerManager
from pokerapp.stats_reporter import StatsReporter
from pokerapp.stats import PlayerIdentity
from pokerapp.winnerdetermination import (
    HAND_LANGUAGE_ORDER,
    HAND_NAMES_TRANSLATIONS,
    HandsOfPoker,
    WinnerDetermination,
)


_CONSTANTS = get_game_constants()
_GAME_CONSTANTS = _CONSTANTS.game
_ENGINE_CONSTANTS = _CONSTANTS.engine
_TRANSLATIONS_ROOT = _CONSTANTS.translations
_REDIS_KEY_SECTIONS = _CONSTANTS.redis_keys
if isinstance(_REDIS_KEY_SECTIONS, dict):
    _ENGINE_REDIS_KEYS = _REDIS_KEY_SECTIONS.get("engine", {})
    if not isinstance(_ENGINE_REDIS_KEYS, dict):
        _ENGINE_REDIS_KEYS = {}
else:
    _ENGINE_REDIS_KEYS = {}


def _compute_language_order(translations_root: Any) -> Tuple[str, ...]:
    default_language = "fa"
    if isinstance(translations_root, dict):
        candidate = translations_root.get("default_language")
        if isinstance(candidate, str) and candidate:
            default_language = candidate
    return tuple(dict.fromkeys([default_language, "fa", "en"]))


_LANGUAGE_ORDER = _compute_language_order(_TRANSLATIONS_ROOT)


def _select_translation(
    entry: Any,
    default: str,
    *,
    language_order: Optional[Iterable[str]] = None,
) -> str:
    languages = tuple(language_order or _LANGUAGE_ORDER)
    if isinstance(entry, dict):
        for language in languages:
            text = entry.get(language)
            if isinstance(text, str) and text:
                return text
    if isinstance(entry, str) and entry:
        return entry
    return default


_AUTO_START_DEFAULTS = _GAME_CONSTANTS.get("auto_start", {})


def _coerce_numeric(
    value: Any,
    default: float,
    *,
    converter: Callable[[Any], float | int],
    is_valid: Callable[[float], bool],
) -> float:
    """Return ``default`` when conversion or validation fails."""

    try:
        parsed = converter(value)
    except (TypeError, ValueError):
        return default
    if not is_valid(parsed):
        return default
    return parsed


def _positive_int(value: Any, default: int) -> int:
    return int(
        _coerce_numeric(value, float(default), converter=int, is_valid=lambda parsed: parsed > 0)
    )


def _non_negative_float(value: Any, default: float) -> float:
    return _coerce_numeric(
        value,
        default,
        converter=float,
        is_valid=lambda parsed: parsed >= 0,
    )


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
    STAGE_LOCK_PREFIX = _ENGINE_REDIS_KEYS.get("stage_lock_prefix", "stage:")
    KEY_STOP_REQUEST = _ENGINE_REDIS_KEYS.get(
        "stop_request",
        _ENGINE_CONSTANTS.get("key_stop_request", "stop_request"),
    )
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
        constants: Optional[GameConstants] = None,
        adaptive_player_report_cache: Optional[AdaptivePlayerReportCache] = None,
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
        self.constants = constants or _CONSTANTS
        self._adaptive_player_report_cache = adaptive_player_report_cache
        self._initialize_stop_translations()

    def _log_extra(
        self,
        *,
        stage: str,
        game: Optional[Game] = None,
        chat_id: Optional[ChatId] = None,
        env_config_missing: Optional[Any] = None,
        **extra_fields: Any,
    ) -> Dict[str, Any]:
        resolved_chat_id: Optional[int]
        if chat_id is not None:
            try:
                resolved_chat_id = self._safe_int(chat_id)
            except Exception:  # pragma: no cover - defensive fallback
                resolved_chat_id = chat_id  # type: ignore[assignment]
        elif game is not None and getattr(game, "chat_id", None) is not None:
            candidate = getattr(game, "chat_id")
            try:
                resolved_chat_id = self._safe_int(candidate)
            except Exception:  # pragma: no cover - fallback to candidate
                resolved_chat_id = candidate  # type: ignore[assignment]
        else:
            resolved_chat_id = None

        dealer_index = -1
        players_ready = 0
        if game is not None:
            dealer_index = getattr(game, "dealer_index", -1)
            if hasattr(game, "seated_players"):
                try:
                    players_ready = len(game.seated_players())
                except Exception:  # pragma: no cover - fallback to attribute
                    players_ready = getattr(game, "seated_count", lambda: 0)()

        extra: Dict[str, Any] = {
            "category": "engine",
            "stage": stage,
            "chat_id": resolved_chat_id,
            "game_id": getattr(game, "id", None) if game is not None else None,
            "dealer_index": dealer_index,
            "players_ready": players_ready,
            "env_config_missing": list(env_config_missing or []),
        }

        if self._logger.isEnabledFor(logging.DEBUG) and game is not None:
            snapshot = []
            try:
                players = game.seated_players()
            except Exception:  # pragma: no cover - fallback to attribute
                players = list(getattr(game, "players", []))
            for player in players:
                snapshot.append(
                    {
                        "user_id": getattr(player, "user_id", None),
                        "seat_index": getattr(player, "seat_index", None),
                        "stack": getattr(player, "stack", None),
                        "total_bet": getattr(player, "total_bet", None),
                        "state": getattr(getattr(player, "state", None), "name", None),
                    }
                )
            extra.update(
                {
                    "debug_stage": getattr(getattr(game, "state", None), "name", None),
                    "debug_pot": getattr(game, "pot", None),
                    "debug_player_snapshot": snapshot,
                }
            )

        extra.update(extra_fields)
        return extra

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

    def _build_telegram_log_extra(
        self,
        *,
        chat_id: ChatId,
        operation: str,
        message_id: Optional[MessageId],
        game_id: Optional[str],
        request_category: RequestCategory | str | None = None,
        event_type: Optional[str] = None,
        user_id: Optional[UserId] = None,
    ) -> Dict[str, Any]:
        category: Optional[str]
        if isinstance(request_category, RequestCategory):
            category = request_category.value
        else:
            category = request_category
        return {
            "chat_id": self._safe_int(chat_id),
            "message_id": message_id,
            "game_id": game_id,
            "user_id": self._safe_int(user_id) if user_id is not None else None,
            "event_type": event_type or f"telegram_{operation}",
            "request_category": category or RequestCategory.GENERAL.value,
            "operation": operation,
        }

    def _initialize_stop_translations(self) -> None:
        translations_root = getattr(self.constants, "translations", {})
        if not isinstance(translations_root, dict):
            translations_root = {}

        language_order = _compute_language_order(translations_root)

        stop_translations = translations_root.get("stop_vote", {})
        if not isinstance(stop_translations, dict):
            stop_translations = {}

        stop_buttons = stop_translations.get("buttons", {})
        if not isinstance(stop_buttons, dict):
            stop_buttons = {}

        stop_messages = stop_translations.get("messages", {})
        if not isinstance(stop_messages, dict):
            stop_messages = {}

        stop_errors = stop_translations.get("errors", {})
        if not isinstance(stop_errors, dict):
            stop_errors = {}

        translation_entries = [
            ("STOP_CONFIRM_BUTTON_TEXT", stop_buttons, "confirm", "Confirm stop"),
            ("STOP_RESUME_BUTTON_TEXT", stop_buttons, "resume", "Resume game"),
            (
                "STOP_TITLE_TEMPLATE",
                stop_messages,
                "title",
                "🛑 *Stop game request*",
            ),
            (
                "STOP_INITIATED_BY_TEMPLATE",
                stop_messages,
                "initiated_by",
                "Requested by {initiator}",
            ),
            (
                "STOP_ACTIVE_PLAYERS_LABEL",
                stop_messages,
                "active_players_label",
                "Active players:",
            ),
            (
                "STOP_ACTIVE_PLAYER_LINE_TEMPLATE",
                stop_messages,
                "active_player_line",
                "{mark} {player}",
            ),
            (
                "STOP_VOTE_COUNTS_TEMPLATE",
                stop_messages,
                "vote_counts",
                "Approval votes: {confirmed}/{required}",
            ),
            (
                "STOP_MANAGER_LABEL_TEMPLATE",
                stop_messages,
                "manager_label",
                "👤 Game manager: {manager}",
            ),
            (
                "STOP_MANAGER_OVERRIDE_HINT",
                stop_messages,
                "manager_override_hint",
                "They can approve the stop vote alone.",
            ),
            (
                "STOP_OTHER_VOTES_LABEL",
                stop_messages,
                "other_votes_label",
                "Other voters:",
            ),
            (
                "STOP_RESUME_NOTICE",
                stop_messages,
                "resume_text",
                "✅ The game will continue.",
            ),
            (
                "STOP_MANAGER_OVERRIDE_SUMMARY",
                stop_messages,
                "manager_override_summary",
                "🛑 *The manager stopped the game.*",
            ),
            (
                "STOP_MAJORITY_SUMMARY",
                stop_messages,
                "majority_stop_summary",
                "🛑 *The game was stopped by majority vote.*",
            ),
            (
                "STOP_VOTE_SUMMARY_TEMPLATE",
                stop_messages,
                "vote_summary",
                "Approval votes: {approved}/{required}",
            ),
            (
                "STOP_NO_VOTES_TEXT",
                stop_messages,
                "no_votes",
                "No active votes were recorded.",
            ),
            (
                "STOP_NO_ACTIVE_PLAYERS_PLACEHOLDER",
                stop_messages,
                "no_active_players_placeholder",
                "—",
            ),
            (
                "STOPPED_NOTIFICATION",
                stop_messages,
                "stopped_notification",
                "🛑 The game has been stopped.",
            ),
            (
                "ERROR_NO_ACTIVE_GAME",
                stop_errors,
                "no_active_game",
                "There is no active game to stop.",
            ),
            (
                "ERROR_NOT_IN_GAME",
                stop_errors,
                "not_in_game",
                "Only seated players can request to stop the game.",
            ),
            (
                "ERROR_NO_ACTIVE_PLAYERS",
                stop_errors,
                "no_active_players",
                "There are no active players to vote.",
            ),
            (
                "ERROR_NO_REQUEST_TO_RESUME",
                stop_errors,
                "no_request_to_resume",
                "There is no stop request to resume.",
            ),
            (
                "ERROR_NO_ACTIVE_REQUEST",
                stop_errors,
                "no_active_request",
                "There is no active stop request.",
            ),
            (
                "ERROR_NOT_ALLOWED_TO_VOTE",
                stop_errors,
                "not_allowed_to_vote",
                "Only active players or the manager may vote.",
            ),
        ]

        for attribute, source, key, default in translation_entries:
            setattr(
                self,
                attribute,
                _select_translation(
                    source.get(key), default, language_order=language_order
                ),
            )

    def _stage_lock_key(self, chat_id: ChatId) -> str:
        return f"{self.STAGE_LOCK_PREFIX}{self._safe_int(chat_id)}"

    def _invalidate_adaptive_report_cache(
        self, players: Iterable[Player], *, event_type: str
    ) -> None:
        if self._adaptive_player_report_cache is None:
            return
        player_ids = normalize_player_ids(players)
        if not player_ids:
            return
        self._adaptive_player_report_cache.invalidate_on_event(
            player_ids, event_type
        )

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
        default_label = hand_type.name.replace("_", " ").title()
        emoji: Optional[str] = None
        label = default_label
        if isinstance(translation, dict):
            emoji_candidate = translation.get("emoji")
            if isinstance(emoji_candidate, str) and emoji_candidate:
                emoji = emoji_candidate
            language_entries = {
                key: value
                for key, value in translation.items()
                if key != "emoji" and isinstance(value, str) and value
            }
            if language_entries:
                label = _select_translation(
                    language_entries,
                    default_label,
                    language_order=HAND_LANGUAGE_ORDER or _LANGUAGE_ORDER,
                )
        elif isinstance(translation, str) and translation:
            label = translation
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
            game.chat_id = chat_id
            players_snapshot = list(game.players)

            await self._clear_game_messages(game, chat_id)

            pot_total = game.pot
            game_id = getattr(game, "id", None)

            payouts, hand_labels, announcements = await self._determine_winners(
                game=game,
                chat_id=chat_id,
            )

            await self._execute_payouts(game=game, payouts=payouts)

            await self._notify_results(
                chat_id=chat_id,
                announcements=announcements,
            )

            await self._record_hand_results(
                game=game,
                chat_id=chat_id,
                payouts=dict(payouts),
                hand_labels=hand_labels,
                pot_total=pot_total,
                players_snapshot=players_snapshot,
            )

            await self._reset_game_state(
                game=game,
                context=context,
                chat_id=chat_id,
                game_id=game_id,
            )

    async def _determine_winners(
        self,
        *,
        game: Game,
        chat_id: ChatId,
    ) -> Tuple[
        DefaultDict[int, int],
        Dict[int, Optional[str]],
        List[Dict[str, Any]],
    ]:
        payouts: DefaultDict[int, int] = defaultdict(int)
        hand_labels: Dict[int, Optional[str]] = {}
        announcements: List[Dict[str, Any]] = []

        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        folded_player_ids = [
            player.user_id for player in game.players_by(states=(PlayerState.FOLD,))
        ]

        if not contenders:
            announcements.extend(
                await self._process_fold_win(
                    game,
                    folded_player_ids,
                    payouts=payouts,
                    hand_labels=hand_labels,
                    chat_id=chat_id,
                )
            )
            return payouts, hand_labels, announcements

        contender_details = self._evaluate_contender_hands(game, contenders)
        winners_by_pot = self._determine_pot_winners(game, contender_details)
        winner_data = {
            "contender_details": contender_details,
            "winners_by_pot": winners_by_pot,
        }
        announcements.extend(
            await self._process_showdown_results(
                game,
                winner_data,
                payouts=payouts,
                hand_labels=hand_labels,
                chat_id=chat_id,
            )
        )
        return payouts, hand_labels, announcements

    async def _execute_payouts(
        self,
        *,
        game: Game,
        payouts: DefaultDict[int, int],
    ) -> None:
        await self._distribute_payouts(game, payouts)

    async def _notify_results(
        self,
        *,
        chat_id: ChatId,
        announcements: Iterable[Dict[str, Any]],
    ) -> None:
        for announcement in announcements:
            call = announcement.get("call")
            if call is None:
                continue
            await self._telegram_ops.send_message_safe(
                call=call,
                chat_id=chat_id,
                operation=announcement.get("operation"),
                log_extra=announcement.get("log_extra"),
            )

    async def _record_hand_results(
        self,
        *,
        game: Game,
        chat_id: ChatId,
        payouts: Dict[int, int],
        hand_labels: Dict[int, Optional[str]],
        pot_total: int,
        players_snapshot: Iterable[Player],
    ) -> None:
        self._invalidate_adaptive_report_cache(
            players_snapshot, event_type="hand_finished"
        )

        await self._stats_reporter.hand_finished(
            game,
            chat_id,
            payouts=payouts,
            hand_labels=hand_labels,
            pot_total=pot_total,
        )

    async def _reset_game_state(
        self,
        *,
        game: Game,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: ChatId,
        game_id: Optional[int],
    ) -> None:
        await self._reset_core_game_state(
            game,
            context=context,
            chat_id=chat_id,
            send_stop_notification=False,
        )

        await self._telegram_ops.send_message_safe(
            call=lambda: self._view.send_new_hand_ready_message(chat_id),
            chat_id=chat_id,
            operation="send_new_hand_ready_message",
            log_extra=self._build_telegram_log_extra(
                chat_id=chat_id,
                message_id=None,
                game_id=game_id,
                operation="send_new_hand_ready_message",
                request_category=RequestCategory.GENERAL,
            ),
        )
        await self._player_manager.send_join_prompt(game, chat_id)

    async def _process_fold_win(
        self,
        game: Game,
        folded_player_ids: Iterable[UserId],
        *,
        payouts: Dict[int, int],
        hand_labels: Dict[int, Optional[str]],
        chat_id: ChatId,
    ) -> List[Dict[str, Any]]:
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        folded_count = len(tuple(folded_player_ids))
        if len(active_players) != 1:
            return []

        winner = active_players[0]
        amount = game.pot
        winner_id = self._safe_int(winner.user_id)

        if amount > 0:
            payouts[winner_id] += amount

        hand_labels[winner_id] = "پیروزی با فولد رقبا"

        fold_phrase = "تمام بازیکنان دیگر فولد کردند"
        if folded_count:
            fold_phrase = f"{fold_phrase} ({folded_count} نفر)"

        message_text = (
            f"🏆 {fold_phrase}! {winner.mention_markdown} برنده {amount}$ شد."
        )
        return [
            {
                "call": lambda text=message_text: self._view.send_message(
                    chat_id, text
                ),
                "operation": "announce_fold_win_message",
                "log_extra": self._build_telegram_log_extra(
                    chat_id=chat_id,
                    message_id=None,
                    game_id=getattr(game, "id", None),
                    operation="announce_fold_win_message",
                    request_category=RequestCategory.GENERAL,
                ),
            }
        ]

    async def _process_showdown_results(
        self,
        game: Game,
        winner_data: Dict[str, object],
        *,
        payouts: Dict[int, int],
        hand_labels: Dict[int, Optional[str]],
        chat_id: ChatId,
    ) -> List[Dict[str, Any]]:
        contender_details = list(winner_data.get("contender_details", []))
        winners_by_pot = list(winner_data.get("winners_by_pot", []))

        announcements: List[Dict[str, Any]] = []

        for detail in contender_details:
            player = detail.get("player") if isinstance(detail, dict) else None
            if not player:
                continue
            label = self.hand_type_to_label(detail.get("hand_type"))
            if label:
                hand_labels[self._safe_int(player.user_id)] = label

        if winners_by_pot:
            for pot in winners_by_pot:
                if not isinstance(pot, dict):
                    continue
                pot_amount = pot.get("amount", 0)
                winners_info = pot.get("winners", [])
                if pot_amount > 0 and winners_info:
                    base_share, remainder = divmod(pot_amount, len(winners_info))
                    for index, winner in enumerate(winners_info):
                        player = winner.get("player") if isinstance(winner, dict) else None
                        if not player:
                            continue
                        win_amount = base_share + (1 if index < remainder else 0)
                        if win_amount > 0:
                            payouts[self._safe_int(player.user_id)] += win_amount
                        winner_label = self.hand_type_to_label(winner.get("hand_type"))
                        player_id = self._safe_int(player.user_id)
                        if winner_label and player_id not in hand_labels:
                            hand_labels[player_id] = winner_label
        if not winners_by_pot:
            message_text = (
                "ℹ️ هیچ برنده‌ای در این دست مشخص نشد. مشکلی در منطق بازی رخ داده است."
            )
            announcements.append(
                {
                    "call": lambda text=message_text: self._view.send_message(
                        chat_id, text
                    ),
                    "operation": "announce_showdown_warning",
                    "log_extra": self._build_telegram_log_extra(
                        chat_id=chat_id,
                        message_id=None,
                        game_id=getattr(game, "id", None),
                        operation="announce_showdown_warning",
                        request_category=RequestCategory.GENERAL,
                    ),
                }
            )

        announcements.append(
            {
                "call": lambda winners=winners_by_pot: self._view.send_showdown_results(
                    chat_id, game, winners
                ),
                "operation": "send_showdown_results",
                "log_extra": self._build_telegram_log_extra(
                    chat_id=chat_id,
                    message_id=None,
                    game_id=getattr(game, "id", None),
                    operation="send_showdown_results",
                    request_category=RequestCategory.GENERAL,
                ),
            }
        )

        return announcements

    async def _distribute_payouts(
        self,
        game: Game,
        payouts: Dict[int, int],
    ) -> None:
        if not payouts:
            return

        for player in game.players:
            player_id = self._safe_int(getattr(player, "user_id", 0))
            amount = payouts.get(player_id, 0)
            if amount > 0:
                await player.wallet.inc(amount)

    async def _reset_core_game_state(
        self,
        game: Game,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: ChatId,
        send_stop_notification: bool = False,
    ) -> None:
        game.pot = 0
        game.state = GameState.FINISHED

        remaining_players = []
        for player in game.players:
            wallet = getattr(player, "wallet", None)
            if wallet is None:
                continue
            try:
                balance = await wallet.value()
            except TypeError:
                balance = 0
            if isinstance(balance, (int, float)) and balance > 0:
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
        if send_stop_notification:
            await self._telegram_ops.send_message_safe(
                call=lambda text=self.STOPPED_NOTIFICATION: self._view.send_message(
                    chat_id, text
                ),
                chat_id=chat_id,
                operation="send_stop_notification",
                log_extra=self._build_telegram_log_extra(
                    chat_id=chat_id,
                    message_id=None,
                    game_id=getattr(game, "id", None),
                    operation="send_stop_notification",
                    request_category=RequestCategory.GENERAL,
                ),
            )

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

    def _determine_pot_winners(
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
            chat_identifier = getattr(game, "chat_id", None)
            self._logger.error(
                "Pot calculation mismatch",
                extra=self._log_extra(
                    stage="payout-resolution",
                    game=game,
                    chat_id=chat_identifier,
                    request_category="engine",
                    event_type="pot_calculation_mismatch",
                    request_params={
                        "game_pot": game.pot,
                        "calculated": calculated_pot_total,
                    },
                    error_type="PotMismatch",
                    user_id=None,
                ),
            )
            notify_payload = {
                "event": "pot_mismatch",
                "game_pot": game.pot,
                "calculated": calculated_pot_total,
            }
            log_extra = self._build_telegram_log_extra(
                chat_id=chat_identifier,
                message_id=None,
                game_id=getattr(game, "id", None),
                operation="notify_admin_pot_mismatch",
                request_category=RequestCategory.GENERAL,
            )
            try:
                asyncio.create_task(
                    self._telegram_ops.send_message_safe(
                        call=lambda data=notify_payload: self._view.notify_admin(data),
                        chat_id=chat_identifier,
                        operation="notify_admin_pot_mismatch",
                        log_extra=log_extra,
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
            raise UserException(self.ERROR_NO_ACTIVE_GAME)

        if not any(player.user_id == requester_id for player in game.seated_players()):
            raise UserException(self.ERROR_NOT_IN_GAME)

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
            raise UserException(self.ERROR_NO_ACTIVE_PLAYERS)

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

        current_message_id = stop_request.get("message_id")
        message_id = await self._telegram_ops.edit_message_text(
            chat_id,
            current_message_id,
            message_text,
            reply_markup=self.build_stop_request_markup(),
            request_category=RequestCategory.GENERAL,
            log_extra=self._build_telegram_log_extra(
                chat_id=chat_id,
                message_id=current_message_id,
                game_id=getattr(game, "id", None),
                operation="stop_vote_request_message",
                request_category=RequestCategory.GENERAL,
            ),
        )
        stop_request["message_id"] = message_id
        context.chat_data[self.KEY_STOP_REQUEST] = stop_request

    def build_stop_request_markup(self) -> InlineKeyboardMarkup:
        """Return the inline keyboard used for stop confirmations."""

        keyboard = [
            [
                InlineKeyboardButton(
                    text=self.STOP_CONFIRM_BUTTON_TEXT,
                    callback_data=self.STOP_CONFIRM_CALLBACK,
                ),
                InlineKeyboardButton(
                    text=self.STOP_RESUME_BUTTON_TEXT,
                    callback_data=self.STOP_RESUME_CALLBACK,
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
            active_lines.append(
                self.STOP_ACTIVE_PLAYER_LINE_TEMPLATE.format(
                    mark=mark,
                    player=player.mention_markdown,
                )
            )
        if not active_lines:
            active_lines.append(self.STOP_NO_ACTIVE_PLAYERS_PLACEHOLDER)

        lines = [
            self.STOP_TITLE_TEMPLATE,
            self.STOP_INITIATED_BY_TEMPLATE.format(initiator=initiator_text),
            "",
            self.STOP_ACTIVE_PLAYERS_LABEL,
            *active_lines,
            "",
            self.STOP_VOTE_COUNTS_TEMPLATE.format(
                confirmed=confirmed_votes,
                required=required_votes,
            ),
        ]

        if manager_player:
            lines.extend(
                [
                    "",
                    self.STOP_MANAGER_LABEL_TEMPLATE.format(
                        manager=manager_player.mention_markdown
                    ),
                    self.STOP_MANAGER_OVERRIDE_HINT,
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
                    self.STOP_OTHER_VOTES_LABEL,
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
            raise UserException(self.ERROR_NO_REQUEST_TO_RESUME)

        message_id = stop_request.get("message_id")
        context.chat_data.pop(self.KEY_STOP_REQUEST, None)

        await self._telegram_ops.edit_message_text(
            chat_id,
            message_id,
            self.STOP_RESUME_NOTICE,
            reply_markup=None,
            request_category=RequestCategory.GENERAL,
            log_extra=self._build_telegram_log_extra(
                chat_id=chat_id,
                message_id=message_id,
                game_id=getattr(game, "id", None),
                operation="stop_vote_resume_message",
                request_category=RequestCategory.GENERAL,
            ),
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

        await self._reset_game_state_after_stop(
            game=game, chat_id=chat_id, context=context
        )

    def _validate_stop_request(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
    ) -> Dict[str, object]:
        stop_request = context.chat_data.get(self.KEY_STOP_REQUEST)
        if not stop_request or stop_request.get("game_id") != game.id:
            raise UserException(self.ERROR_NO_ACTIVE_REQUEST)
        return stop_request

    def _validate_stop_voter(
        self,
        voter_id: UserId,
        active_ids: Set[UserId],
        manager_id: Optional[UserId],
    ) -> None:
        if voter_id not in active_ids and voter_id != manager_id:
            raise UserException(self.ERROR_NOT_ALLOWED_TO_VOTE)

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

        current_message_id = stop_request.get("message_id")
        message_id = await self._telegram_ops.edit_message_text(
            chat_id,
            current_message_id,
            message_text,
            reply_markup=self.build_stop_request_markup(),
            request_category=RequestCategory.GENERAL,
            log_extra=self._build_telegram_log_extra(
                chat_id=chat_id,
                message_id=current_message_id,
                game_id=getattr(game, "id", None),
                operation="stop_vote_request_message",
                request_category=RequestCategory.GENERAL,
            ),
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

        self._invalidate_adaptive_report_cache(
            player_list, event_type="hand_finished"
        )

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
            summary_line = self.STOP_MANAGER_OVERRIDE_SUMMARY
        else:
            summary_line = self.STOP_MAJORITY_SUMMARY

        if active_ids:
            details = self.STOP_VOTE_SUMMARY_TEMPLATE.format(
                approved=approved_votes,
                required=required_votes,
            )
        else:
            details = self.STOP_NO_VOTES_TEXT

        return "\n".join([summary_line, details])

    async def _finalize_stop_request(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: ChatId,
        stop_request: Dict[str, object],
    ) -> None:
        message_text = self._build_stop_cancellation_message(stop_request)

        current_message_id = stop_request.get("message_id")
        await self._telegram_ops.edit_message_text(
            chat_id,
            current_message_id,
            message_text,
            reply_markup=None,
            request_category=RequestCategory.GENERAL,
            log_extra=self._build_telegram_log_extra(
                chat_id=chat_id,
                message_id=current_message_id,
                game_id=stop_request.get("game_id"),
                operation="stop_vote_finalize_message",
                request_category=RequestCategory.GENERAL,
            ),
        )

        context.chat_data.pop(self.KEY_STOP_REQUEST, None)

    async def _reset_game_state_after_stop(
        self,
        *,
        game: Game,
        chat_id: ChatId,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._reset_core_game_state(
            game,
            context=context,
            chat_id=chat_id,
            send_stop_notification=False,
        )
        await self._telegram_ops.send_message_safe(
            call=lambda text=self.STOPPED_NOTIFICATION: self._view.send_message(
                chat_id, text
            ),
            chat_id=chat_id,
            operation="send_stop_notification",
            log_extra=self._build_telegram_log_extra(
                chat_id=chat_id,
                message_id=None,
                game_id=getattr(game, "id", None),
                operation="send_stop_notification",
                request_category=RequestCategory.GENERAL,
            ),
        )
