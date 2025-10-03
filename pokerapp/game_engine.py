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
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Mapping,
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
    Money,
    Player,
    PlayerState,
    UserException,
    UserId,
    Wallet,
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
from pokerapp.services.countdown_queue import CountdownMessageQueue
from pokerapp.services.countdown_worker import CountdownWorker
from pokerapp.player_manager import PlayerManager
from pokerapp.stats_reporter import StatsReporter
from pokerapp.stats import PlayerIdentity
from pokerapp.winnerdetermination import (
    HAND_LANGUAGE_ORDER,
    HAND_NAMES_TRANSLATIONS,
    HandsOfPoker,
    WinnerDetermination,
)
from pokerapp.translations import translate


def clear_all_message_ids(game: Game) -> None:
    """Reset cached message identifiers for ``game`` and its players."""

    game.ready_message_main_id = None
    game.ready_message_game_id = None
    game.ready_message_stage = None
    game.ready_message_main_text = ""
    game.anchor_message_id = None
    game.board_message_id = None
    game.seat_announcement_message_id = None

    message_ids = getattr(game, "message_ids_to_delete", None)
    if message_ids is not None and hasattr(message_ids, "clear"):
        message_ids.clear()

    for player in getattr(game, "players", []):
        player.ready_message_id = None


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

_LOCKS_SECTION = _CONSTANTS.section("locks")
_CATEGORY_TIMEOUTS = {}
if isinstance(_LOCKS_SECTION, dict):
    candidate_timeouts = _LOCKS_SECTION.get("category_timeouts_seconds")
    if isinstance(candidate_timeouts, dict):
        _CATEGORY_TIMEOUTS = candidate_timeouts


_CRITICAL_SECTION_LONG_HOLD_THRESHOLD_SECONDS = 2.0


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


class _TableLockWallet(Wallet):
    """Minimal wallet used for table-lock-guarded join operations."""

    def __init__(self, balance: Money) -> None:
        self._balance: Money = balance
        self._authorized: Dict[str, Money] = {}

    @staticmethod
    def _prefix(id: int, suffix: str = ""):
        return f"wallet:{id}{suffix}"

    async def add_daily(self, amount: Money) -> Money:
        self._balance += amount
        return self._balance

    async def has_daily_bonus(self) -> bool:
        return False

    async def inc(self, amount: Money = 0) -> Money:
        self._balance += amount
        return self._balance

    async def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        self._authorized[game_id] = self._authorized.get(game_id, 0) + amount

    async def authorized_money(self, game_id: str) -> Money:
        return self._authorized.get(game_id, 0)

    async def authorize(self, game_id: str, amount: Money) -> None:
        self._authorized[game_id] = amount

    async def authorize_all(self, game_id: str) -> Money:
        return self._authorized.get(game_id, 0)

    async def value(self) -> Money:
        return self._balance

    async def approve(self, game_id: str) -> None:
        self._authorized.pop(game_id, None)

    async def cancel(self, game_id: str) -> None:
        self._authorized.pop(game_id, None)


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


@dataclass(frozen=True)
class _ResetNotifications:
    chat_id: ChatId
    game_id: Optional[int]


@dataclass(frozen=True)
class _GameStatsSnapshot:
    id: Optional[int]
    _players: Tuple[Player, ...]

    def seated_players(self) -> List[Player]:
        return list(self._players)

    @property
    def players(self) -> List[Player]:
        return list(self._players)
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
    STAGE_LOCK_TIMEOUT_SECONDS = _non_negative_float(
        _CATEGORY_TIMEOUTS.get("engine_stage"),
        25.0,
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
    KEY_START_COUNTDOWN_ANCHOR = _ENGINE_CONSTANTS.get(
        "key_start_countdown_anchor",
        "start_countdown_anchor",
    )
    KEY_START_COUNTDOWN_INITIAL_SECONDS = _ENGINE_CONSTANTS.get(
        "key_start_countdown_initial_seconds",
        "start_countdown_initial_seconds",
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

    @staticmethod
    def _loop_time() -> float:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - fallback for compatibility
            loop = asyncio.get_event_loop()
        return loop.time()

    @classmethod
    def compute_turn_deadline(cls) -> float:
        return cls._loop_time() + cls._MAX_TIME_FOR_TURN_SECONDS

    def refresh_turn_deadline(self, game: Game) -> None:
        if game is None:
            return

        current_index = getattr(game, "current_player_index", -1)
        if not isinstance(current_index, int) or current_index < 0:
            if hasattr(game, "turn_deadline"):
                game.turn_deadline = None
            return

        game.turn_deadline = self.compute_turn_deadline()
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
        player_factory: Optional[Callable[[int, str], Player]] = None,
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
        self._safe_ops = telegram_safe_ops
        self._lock_manager = lock_manager
        self._logger = logger
        self.constants = constants or _CONSTANTS
        self._adaptive_player_report_cache = adaptive_player_report_cache
        self._stage_lock_timeout = self.STAGE_LOCK_TIMEOUT_SECONDS
        self._max_players = _positive_int(
            _GAME_CONSTANTS.get("max_players"), 8
        )
        self._default_money = _positive_int(
            _GAME_CONSTANTS.get("default_money"), 1000
        )
        self._player_factory = player_factory
        self._initialize_stop_translations()
        self._countdown_queue = CountdownMessageQueue(max_size=100)
        self._countdown_worker = CountdownWorker(
            queue=self._countdown_queue,
            safe_ops=self._safe_ops,
            edit_interval=1.0,
        )
        self._countdown_contexts: Dict[int, ContextTypes.DEFAULT_TYPE] = {}

        locks_config = getattr(self.constants, "locks", None)
        action_lock_config: Optional[Mapping[str, Any]] = None
        if isinstance(locks_config, Mapping):
            candidate = locks_config.get("action")
            if isinstance(candidate, Mapping):
                action_lock_config = candidate
        if action_lock_config is None and hasattr(self.constants, "section"):
            try:
                locks_section = self.constants.section("locks")  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - defensive fallback
                locks_section = None
            if isinstance(locks_section, Mapping):
                candidate = locks_section.get("action")
                if isinstance(candidate, Mapping):
                    action_lock_config = candidate

        self._valid_player_actions: Set[str] = {"fold", "check", "call", "raise"}
        self._action_lock_ttl: int = 10
        self._action_lock_feedback_text = "⚠️ Action in progress, please wait..."
        if isinstance(action_lock_config, Mapping):
            ttl_candidate = action_lock_config.get("ttl")
            if isinstance(ttl_candidate, (int, float)):
                ttl_value = int(ttl_candidate)
                if ttl_value > 0:
                    self._action_lock_ttl = ttl_value
            valid_types_candidate = action_lock_config.get("valid_types")
            if isinstance(valid_types_candidate, (list, tuple, set)):
                normalized = {
                    str(value).strip().lower()
                    for value in valid_types_candidate
                    if str(value).strip()
                }
                if normalized:
                    self._valid_player_actions = normalized
            feedback_candidate = action_lock_config.get("feedback_text")
            if isinstance(feedback_candidate, str) and feedback_candidate.strip():
                self._action_lock_feedback_text = feedback_candidate.strip()

    async def join_game(
        self,
        *,
        chat_id: int,
        user_id: int,
        user_name: str,
    ) -> bool:
        lock_token = await self._lock_manager.acquire_table_lock(
            chat_id=chat_id,
            operation="join",
            timeout_seconds=5,
        )

        if not lock_token:
            self._logger.warning(
                "Join rejected - table lock held",
                extra={
                    "event_type": "join_table_locked",
                    "chat_id": chat_id,
                    "user_id": user_id,
                },
            )
            await self._view.send_message(
                chat_id,
                translate(
                    "error.table_busy",
                    "⚠️ Table is busy. Please try again in a moment.",
                ),
                request_category=RequestCategory.GENERAL,
            )
            return False

        try:
            loaded = await self._table_manager.load_game(chat_id)
            game = loaded[0] if isinstance(loaded, tuple) else loaded
            if game is None:
                game = await self._table_manager.create_game(chat_id)

            if game.state != GameState.INITIAL:
                await self._view.send_message(
                    chat_id,
                    translate(
                        "error.game_in_progress",
                        "⚠️ Cannot join - game in progress.",
                    ),
                    request_category=RequestCategory.GENERAL,
                )
                return False

            if game.seat_index_for_user(user_id) != -1:
                await self._view.send_message(
                    chat_id,
                    translate(
                        "error.already_joined",
                        "⚠️ You've already joined this game.",
                    ),
                    request_category=RequestCategory.GENERAL,
                )
                return False

            if game.seated_count() >= self._max_players:
                await self._view.send_message(
                    chat_id,
                    translate(
                        "error.table_full",
                        "⚠️ Table is full ({} players maximum).",
                    ).format(self._max_players),
                    request_category=RequestCategory.GENERAL,
                )
                return False

            available_seats = self._get_available_seats(game)
            if not available_seats:
                self._logger.error(
                    "No available seats despite table not full",
                    extra={
                        "event_type": "seat_assignment_error",
                        "chat_id": chat_id,
                        "player_count": game.seated_count(),
                        "max_players": self._max_players,
                    },
                )
                return False

            seat_index = available_seats[0]
            player = await self._create_joining_player(user_id, user_name)
            game.add_player(player, seat_index)
            if hasattr(game, "ready_users"):
                game.ready_users.add(user_id)

            await self._table_manager.save_game(chat_id, game)

            await self._view.send_message(
                chat_id,
                translate(
                    "game.player_joined",
                    "✅ {name} joined the game (Seat {seat}).",
                ).format(name=user_name, seat=seat_index + 1),
                request_category=RequestCategory.GENERAL,
            )

            self._logger.info(
                "Player joined game",
                extra={
                    "event_type": "player_joined",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "seat": seat_index,
                },
            )
            return True
        finally:
            await self._lock_manager.release_table_lock(
                chat_id=chat_id,
                token=lock_token,
                operation="join",
            )

    async def leave_game(
        self,
        *,
        chat_id: int,
        user_id: int,
    ) -> bool:
        lock_token = await self._lock_manager.acquire_table_lock(
            chat_id=chat_id,
            operation="leave",
            timeout_seconds=5,
        )

        if not lock_token:
            self._logger.warning(
                "Leave rejected - table lock held",
                extra={
                    "event_type": "leave_table_locked",
                    "chat_id": chat_id,
                    "user_id": user_id,
                },
            )
            await self._view.send_message(
                chat_id,
                translate(
                    "error.table_busy",
                    "⚠️ Table is busy. Please try again in a moment.",
                ),
                request_category=RequestCategory.GENERAL,
            )
            return False

        try:
            loaded = await self._table_manager.load_game(chat_id)
            game = loaded[0] if isinstance(loaded, tuple) else loaded
            if game is None:
                await self._view.send_message(
                    chat_id,
                    translate(
                        "error.not_in_game",
                        "⚠️ You're not in this game.",
                    ),
                    request_category=RequestCategory.GENERAL,
                )
                return False

            seat_index = game.seat_index_for_user(user_id)
            if seat_index == -1:
                await self._view.send_message(
                    chat_id,
                    translate(
                        "error.not_in_game",
                        "⚠️ You're not in this game.",
                    ),
                    request_category=RequestCategory.GENERAL,
                )
                return False

            if game.state != GameState.INITIAL:
                await self._view.send_message(
                    chat_id,
                    translate(
                        "error.cannot_leave_active",
                        "⚠️ Cannot leave during active hand. Use /stop to end the game.",
                    ),
                    request_category=RequestCategory.GENERAL,
                )
                return False

            player = game.seats[seat_index]
            name = getattr(player, "display_name", None) or getattr(
                player, "full_name", None
            ) or str(user_id)

            game.remove_player_by_user(user_id)
            if hasattr(game, "ready_users"):
                game.ready_users.discard(user_id)

            await self._table_manager.save_game(chat_id, game)

            await self._view.send_message(
                chat_id,
                translate(
                    "game.player_left",
                    "👋 {name} left the game.",
                ).format(name=name),
                request_category=RequestCategory.GENERAL,
            )

            self._logger.info(
                "Player left game",
                extra={
                    "event_type": "player_left",
                    "chat_id": chat_id,
                    "user_id": user_id,
                },
            )
            return True
        finally:
            await self._lock_manager.release_table_lock(
                chat_id=chat_id,
                token=lock_token,
                operation="leave",
            )

    async def _create_joining_player(
        self, user_id: int, user_name: str
    ) -> Player:
        if self._player_factory is not None:
            result = self._player_factory(user_id, user_name)
            if asyncio.iscoroutine(result):
                return await result  # type: ignore[return-value]
            return result

        mention = format_mention_markdown(user_id, user_name or str(user_id), version=1)
        player = Player(
            user_id=user_id,
            mention_markdown=mention,
            wallet=_TableLockWallet(self._default_money),
            ready_message_id=None,
            seat_index=None,
        )
        player.display_name = user_name or str(user_id)
        player.username = None
        player.full_name = user_name
        return player

    def _get_available_seats(self, game: Game) -> List[int]:
        seats = getattr(game, "seats", [])
        if not isinstance(seats, list):
            return []

        max_seats = min(len(seats), self._max_players)
        available: List[int] = [
            index for index in range(max_seats) if seats[index] is None
        ]
        if max_seats < self._max_players:
            available.extend(range(max_seats, self._max_players))
        return sorted(dict.fromkeys(available))

    def _log_lock_snapshot(
        self,
        *,
        stage: str,
        level: int = logging.DEBUG,
        minimum_level: int = logging.DEBUG,
    ) -> None:
        try:
            snapshot = self._lock_manager.detect_deadlock()
        except Exception:
            self._logger.exception(
                "Failed to capture lock snapshot", extra={"stage": stage}
            )
            return

        effective_level = max(level, minimum_level)
        if (
            not snapshot.get("tasks")
            and not snapshot.get("waiting")
            and minimum_level <= logging.DEBUG
            and effective_level > logging.DEBUG
        ):
            effective_level = logging.DEBUG

        self._logger.log(
            effective_level,
            "Lock snapshot (%s): %s",
            stage,
            json.dumps(snapshot, ensure_ascii=False, default=str),
            extra={"stage": stage, "event_type": "lock_snapshot"},
        )

    def _log_engine_event_lock_failure(
        self,
        *,
        lock_key: str,
        event_stage_label: str,
        chat_id: Optional[ChatId] = None,
        game: Optional[Game] = None,
        log_level: int = logging.ERROR,
    ) -> None:
        resolved_chat_id: Optional[int]
        if chat_id is not None:
            try:
                resolved_chat_id = self._safe_int(chat_id)
            except Exception:  # pragma: no cover - defensive fallback
                resolved_chat_id = chat_id  # type: ignore[assignment]
        elif game is not None and getattr(game, "chat_id", None) is not None:
            chat_candidate = getattr(game, "chat_id")
            try:
                resolved_chat_id = self._safe_int(chat_candidate)
            except Exception:  # pragma: no cover - defensive fallback
                resolved_chat_id = chat_candidate  # type: ignore[assignment]
        else:
            resolved_chat_id = None

        game_id = getattr(game, "id", None) if game is not None else None
        lock_level = self._lock_manager._resolve_level(lock_key, override=None)
        payload = self._lock_manager._build_context_payload(
            lock_key,
            lock_level,
            additional={"chat_id": resolved_chat_id, "game_id": game_id},
        )
        payload.setdefault("lock_key", lock_key)
        payload.setdefault("lock_level", lock_level)
        payload.setdefault("chat_id", resolved_chat_id)
        payload.setdefault("game_id", game_id)

        stage_parts = ["game_event_lock_failure", event_stage_label, lock_key]
        if resolved_chat_id is not None:
            stage_parts.append(f"chat={resolved_chat_id}")
        if game_id is not None:
            stage_parts.append(f"game={game_id}")
        stage_label = ":".join(str(part) for part in stage_parts if part)

        effective_level = max(log_level, logging.WARNING)
        self._lock_manager._log_lock_snapshot_on_timeout(
            stage_label,
            level=effective_level,
            minimum_level=effective_level,
            extra=payload,
        )

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

    @asynccontextmanager
    async def _player_action_locks(
        self,
        game: Game,
        player: Player,
        *,
        include_wallet: bool = True,
        include_report: bool = False,
    ) -> AsyncIterator[Dict[str, bool]]:
        """Acquire locks needed for player action (optimized batch)."""

        chat_id = game.chat_id
        locks_needed = [f"stage:{self._safe_int(chat_id)}"]

        if include_wallet:
            locks_needed.append(f"wallet:{player.user_id}")

        if include_report:
            locks_needed.append(f"player_report:{player.user_id}")

        async with self._lock_manager.acquire_batch(
            locks_needed,
            timeout=self._stage_lock_timeout,
            context={
                "chat_id": chat_id,
                "game_id": game.id,
                "user_id": player.user_id,
            },
        ) as results:
            yield results

    def _find_player_by_user_id(self, game: Game, user_id: int) -> Optional[Player]:
        """Locate a player in ``game`` by their Telegram ``user_id``."""

        seat_lookup = getattr(game, "seat_index_for_user", None)
        if callable(seat_lookup):
            try:
                seat_index = seat_lookup(user_id)
            except Exception:  # pragma: no cover - defensive fallback
                seat_index = -1
            if isinstance(seat_index, int) and seat_index >= 0:
                candidate = game.get_player_by_seat(seat_index)
                if candidate is not None:
                    return candidate

        for player in getattr(game, "players", []):
            if getattr(player, "user_id", None) == user_id:
                return player
        return None

    async def process_action(
        self,
        chat_id: int,
        user_id: int,
        action: str,
        amount: int = 0,
    ) -> bool:
        """Process a player's action with action-level locking protection."""

        action_token = (action or "").strip().lower()
        if action_token not in self._valid_player_actions:
            self._logger.warning(
                "Invalid action type",  # pragma: no cover - sanity logging
                extra={
                    "event_type": "engine_invalid_action",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "action": action,
                },
            )
            return False

        lock_token: Optional[str] = None
        retry_due_to_version_conflict = False
        result = False

        try:
            try:
                lock_token = await self._lock_manager.acquire_action_lock(
                    chat_id,
                    user_id,
                    action_token,
                    ttl=self._action_lock_ttl,
                )
            except Exception:
                self._logger.exception(
                    "Failed acquiring action lock",
                    extra={
                        "event_type": "engine_action_lock_error",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "action": action_token,
                    },
                )
                return False

            if lock_token is None:
                self._logger.info(
                    "Action rejected - lock already held",
                    extra={
                        "event_type": "engine_action_lock_contention",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "action": action_token,
                    },
                )
                if (
                    hasattr(self, "_safe_ops")
                    and hasattr(self._safe_ops, "send_message_safe")
                    and hasattr(self, "_view")
                    and hasattr(self._view, "send_message")
                ):
                    try:
                        await self._safe_ops.send_message_safe(
                            call=lambda text=self._action_lock_feedback_text: self._view.send_message(  # type: ignore[misc]
                                user_id,
                                text,
                                request_category=RequestCategory.GENERAL,
                            ),
                            chat_id=user_id,
                            operation="action_lock_feedback",
                            log_extra={
                                "event_type": "engine_action_lock_feedback",
                                "chat_id": chat_id,
                                "user_id": user_id,
                                "action": action_token,
                            },
                        )
                    except Exception:
                        self._logger.debug(
                            "Unable to send action lock feedback",
                            extra={
                                "event_type": "engine_action_lock_feedback_failed",
                                "chat_id": chat_id,
                                "user_id": user_id,
                                "action": action_token,
                            },
                            exc_info=True,
                        )
                return False

            async def _process_under_table_lock() -> bool:
                nonlocal retry_due_to_version_conflict

                try:
                    game, version = await self._table_manager.load_game_with_version(chat_id)
                except Exception:
                    self._logger.exception(
                        "Failed loading game for action",
                        extra={
                            "event_type": "engine_action_load_failed",
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "action": action_token,
                        },
                    )
                    return False

                if isinstance(game, tuple):
                    current_game = game[0]
                else:
                    current_game = game

                if current_game is None:
                    self._logger.warning(
                        "No active game for action",
                        extra={
                            "event_type": "engine_action_no_game",
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "action": action_token,
                        },
                    )
                    return False

                player = self._find_player_by_user_id(current_game, user_id)
                if player is None:
                    self._logger.warning(
                        "Player not found in game",
                        extra={
                            "event_type": "engine_action_no_player",
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "action": action_token,
                            "game_id": getattr(current_game, "id", None),
                        },
                    )
                    return False

                deadline = getattr(current_game, "turn_deadline", None)
                if isinstance(deadline, (int, float)):
                    try:
                        current_time = asyncio.get_running_loop().time()
                    except RuntimeError:  # pragma: no cover - fallback for compatibility
                        current_time = asyncio.get_event_loop().time()
                    if current_time > float(deadline):
                        self._logger.info(
                            "Action rejected due to expired turn deadline",
                            extra={
                                "event_type": "engine_action_timeout",
                                "chat_id": chat_id,
                                "user_id": user_id,
                                "action": action_token,
                            },
                        )
                        return False

                try:
                    success = await self._execute_player_action(
                        game=current_game,
                        player=player,
                        action=action_token,
                        amount=amount,
                    )
                except Exception:
                    self._logger.exception(
                        "Unexpected error executing action",
                        extra={
                            "event_type": "engine_action_execute_error",
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "action": action_token,
                        },
                    )
                    return False

                if not success:
                    return False

                self.refresh_turn_deadline(current_game)

                try:
                    save_success = await self._table_manager.save_game_with_version_check(
                        chat_id,
                        current_game,
                        version,
                    )
                except Exception:
                    self._logger.exception(
                        "Failed saving game after action",
                        extra={
                            "event_type": "engine_action_save_failed",
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "action": action_token,
                        },
                    )
                    return False

                if not save_success:
                    retry_due_to_version_conflict = True
                    return False

                return True

            lock_manager = self._lock_manager
            if lock_manager is None:
                result = await _process_under_table_lock()
            else:
                async with lock_manager.table_write_lock(chat_id):
                    result = await _process_under_table_lock()
        finally:
            if lock_token is not None:
                try:
                    await self._lock_manager.release_action_lock(
                        chat_id,
                        user_id,
                        action_token,
                        lock_token,
                    )
                except Exception:
                    self._logger.warning(
                        "Failed releasing action lock",
                        extra={
                            "event_type": "engine_action_release_failed",
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "action": action_token,
                        },
                        exc_info=True,
                    )

        if retry_due_to_version_conflict:
            return await self.process_action(chat_id, user_id, action, amount)

        return result

    async def handle_call(self, chat_id: int, user_id: int) -> bool:
        """Handle a player's call with table-level locking and version retries."""

        async def _process_once() -> Optional[bool]:
            try:
                game_data, version = await self._table_manager.load_game_with_version(
                    chat_id
                )
            except Exception:
                self._logger.exception(
                    "Failed loading game for call",
                    extra={
                        "event_type": "engine_call_load_failed",
                        "chat_id": chat_id,
                        "user_id": user_id,
                    },
                )
                return False

            current_game: Optional[Game]
            if isinstance(game_data, tuple):
                current_game = game_data[0]
            else:
                current_game = game_data

            if current_game is None:
                self._logger.warning(
                    "No active game for call",
                    extra={
                        "event_type": "engine_call_no_game",
                        "chat_id": chat_id,
                        "user_id": user_id,
                    },
                )
                return False

            player = self._find_player_by_user_id(current_game, user_id)
            if player is None:
                self._logger.warning(
                    "Player not found in game during call",
                    extra={
                        "event_type": "engine_call_no_player",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "game_id": getattr(current_game, "id", None),
                    },
                )
                return False

            try:
                success = await self._execute_player_action(
                    game=current_game,
                    player=player,
                    action="call",
                    amount=0,
                )
            except Exception:
                self._logger.exception(
                    "Unexpected error executing call",
                    extra={
                        "event_type": "engine_call_execute_error",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "game_id": getattr(current_game, "id", None),
                    },
                )
                return False

            if not success:
                return False

            self.refresh_turn_deadline(current_game)

            try:
                save_success = await self._table_manager.save_game_with_version_check(
                    chat_id,
                    current_game,
                    version,
                )
            except Exception:
                self._logger.exception(
                    "Failed saving game after call",
                    extra={
                        "event_type": "engine_call_save_failed",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "game_id": getattr(current_game, "id", None),
                    },
                )
                return False

            if not save_success:
                return None

            return True

        lock_manager = self._lock_manager

        while True:
            if lock_manager is None:
                result = await _process_once()
            else:
                async with lock_manager.table_write_lock(chat_id):
                    result = await _process_once()

            if result is None:
                continue

            return bool(result)

    async def handle_fold(self, chat_id: int, user_id: int) -> bool:
        """Handle a player's fold with table-level locking and version retries."""

        async def _process_once() -> Optional[bool]:
            try:
                game_data, version = await self._table_manager.load_game_with_version(
                    chat_id
                )
            except Exception:
                self._logger.exception(
                    "Failed loading game for fold",
                    extra={
                        "event_type": "engine_fold_load_failed",
                        "chat_id": chat_id,
                        "user_id": user_id,
                    },
                )
                return False

            current_game: Optional[Game]
            if isinstance(game_data, tuple):
                current_game = game_data[0]
            else:
                current_game = game_data

            if current_game is None:
                self._logger.warning(
                    "No active game for fold",
                    extra={
                        "event_type": "engine_fold_no_game",
                        "chat_id": chat_id,
                        "user_id": user_id,
                    },
                )
                return False

            player = self._find_player_by_user_id(current_game, user_id)
            if player is None:
                self._logger.warning(
                    "Player not found in game during fold",
                    extra={
                        "event_type": "engine_fold_no_player",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "game_id": getattr(current_game, "id", None),
                    },
                )
                return False

            try:
                success = await self._execute_player_action(
                    game=current_game,
                    player=player,
                    action="fold",
                    amount=0,
                )
            except Exception:
                self._logger.exception(
                    "Unexpected error executing fold",
                    extra={
                        "event_type": "engine_fold_execute_error",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "game_id": getattr(current_game, "id", None),
                    },
                )
                return False

            if not success:
                return False

            self.refresh_turn_deadline(current_game)

            try:
                save_success = await self._table_manager.save_game_with_version_check(
                    chat_id,
                    current_game,
                    version,
                )
            except Exception:
                self._logger.exception(
                    "Failed saving game after fold",
                    extra={
                        "event_type": "engine_fold_save_failed",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "game_id": getattr(current_game, "id", None),
                    },
                )
                return False

            if not save_success:
                return None

            return True

        lock_manager = self._lock_manager

        while True:
            if lock_manager is None:
                result = await _process_once()
            else:
                async with lock_manager.table_write_lock(chat_id):
                    result = await _process_once()

            if result is None:
                continue

            return bool(result)

    async def process_bet(self, chat_id: int, user_id: int, amount: int) -> bool:
        """Process a betting request with table-level locking and retries."""

        if amount is None or amount <= 0:
            self._logger.warning(
                "Bet rejected due to invalid amount",
                extra={
                    "event_type": "engine_bet_invalid_amount",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "amount": amount,
                },
            )
            return False

        async def _process_once() -> Optional[bool]:
            try:
                game_data, version = await self._table_manager.load_game_with_version(
                    chat_id
                )
            except Exception:
                self._logger.exception(
                    "Failed loading game for bet",
                    extra={
                        "event_type": "engine_bet_load_failed",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "amount": amount,
                    },
                )
                return False

            current_game: Optional[Game]
            if isinstance(game_data, tuple):
                current_game = game_data[0]
            else:
                current_game = game_data

            if current_game is None:
                self._logger.warning(
                    "No active game for bet",
                    extra={
                        "event_type": "engine_bet_no_game",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "amount": amount,
                    },
                )
                return False

            log_context = {
                "event_type": "engine_process_bet",
                "chat_id": chat_id,
                "game_id": getattr(current_game, "id", None),
                "user_id": user_id,
            }

            player = self._find_player_by_user_id(current_game, user_id)
            if player is None:
                self._logger.warning(
                    "Player not found in game during bet",
                    extra={**log_context, "amount": amount},
                )
                return False

            mention = getattr(
                player, "mention_markdown", str(getattr(player, "user_id", user_id))
            )
            current_round_rate = int(getattr(player, "round_rate", 0))
            max_round_rate = int(getattr(current_game, "max_round_rate", 0))
            call_amount = max(0, max_round_rate - current_round_rate)
            raise_amount = int(amount)
            total_commit = call_amount + raise_amount

            if total_commit <= 0:
                self._logger.warning(
                    "Bet rejected because no chips would be committed",
                    extra={**log_context, "amount": amount},
                )
                return False

            wallet = getattr(player, "wallet", None)
            if wallet is not None and hasattr(wallet, "authorize"):
                try:
                    await wallet.authorize(current_game.id, total_commit)
                except Exception:
                    self._logger.warning(
                        "Wallet authorization failed during bet",
                        extra={**log_context, "amount": total_commit},
                        exc_info=True,
                    )
                    return False

            player.round_rate = current_round_rate + total_commit
            player.total_bet = int(getattr(player, "total_bet", 0)) + total_commit
            current_game.pot = int(getattr(current_game, "pot", 0)) + total_commit
            current_game.max_round_rate = max(
                int(getattr(current_game, "max_round_rate", 0)),
                player.round_rate,
            )

            if hasattr(player, "has_acted"):
                player.has_acted = True
            if hasattr(current_game, "trading_end_user_id"):
                current_game.trading_end_user_id = getattr(player, "user_id", None)

            for other in getattr(current_game, "players", []):
                if other is player:
                    continue
                if (
                    getattr(other, "state", PlayerState.ACTIVE) == PlayerState.ACTIVE
                    and hasattr(other, "has_acted")
                ):
                    other.has_acted = False

            actions = getattr(current_game, "last_actions", None)
            if isinstance(actions, list):
                action_label = "رِیز" if call_amount > 0 else "بِت"
                actions.append(f"{mention}: {action_label} {total_commit}$")
                if len(actions) > 5:
                    del actions[:-5]

            self.refresh_turn_deadline(current_game)

            try:
                save_success = (
                    await self._table_manager.save_game_with_version_check(
                        chat_id, current_game, version
                    )
                )
            except Exception:
                self._logger.exception(
                    "Failed saving game after bet",
                    extra={**log_context, "amount": total_commit},
                )
                return False

            if not save_success:
                return None

            return True

        lock_manager = self._lock_manager

        while True:
            if lock_manager is None:
                result = await _process_once()
            else:
                async with lock_manager.table_write_lock(chat_id):
                    result = await _process_once()

            if result is None:
                continue

            return bool(result)

    async def _execute_player_action(
        self,
        *,
        game: Game,
        player: Player,
        action: str,
        amount: int,
    ) -> bool:
        """Execute a validated player action on the in-memory game state."""

        action_token = action.strip().lower()
        mention = getattr(player, "mention_markdown", str(getattr(player, "user_id", "")))
        chat_id = getattr(game, "chat_id", None)
        log_context = {
            "event_type": "engine_execute_player_action",
            "chat_id": chat_id,
            "game_id": getattr(game, "id", None),
            "user_id": getattr(player, "user_id", None),
            "action": action_token,
        }

        def record_action(entry: str) -> None:
            actions = getattr(game, "last_actions", None)
            if isinstance(actions, list):
                actions.append(entry)
                if len(actions) > 5:
                    del actions[:-5]

        if action_token == "fold":
            if hasattr(player, "state"):
                player.state = PlayerState.FOLD
            if hasattr(player, "has_acted"):
                player.has_acted = True
            record_action(f"{mention}: فولد")
            return True

        if action_token == "check":
            if hasattr(player, "has_acted"):
                player.has_acted = True
            record_action(f"{mention}: چک")
            return True

        if action_token == "call":
            call_amount = max(
                0,
                int(getattr(game, "max_round_rate", 0))
                - int(getattr(player, "round_rate", 0)),
            )
            if call_amount > 0:
                wallet = getattr(player, "wallet", None)
                if wallet is not None and hasattr(wallet, "authorize"):
                    try:
                        await wallet.authorize(game.id, call_amount)
                    except Exception:
                        self._logger.warning(
                            "Wallet authorization failed during call",
                            extra={**log_context, "amount": call_amount},
                            exc_info=True,
                        )
                        return False
                player.round_rate = int(getattr(player, "round_rate", 0)) + call_amount
                player.total_bet = int(getattr(player, "total_bet", 0)) + call_amount
                game.pot = int(getattr(game, "pot", 0)) + call_amount
            if hasattr(player, "has_acted"):
                player.has_acted = True
            if call_amount > 0:
                record_action(f"{mention}: کال {call_amount}$")
            else:
                record_action(f"{mention}: چک")
            return True

        if action_token == "raise":
            if amount is None or amount <= 0:
                self._logger.warning(
                    "Raise rejected due to invalid amount",
                    extra={**log_context, "amount": amount},
                )
                return False
            call_amount = max(
                0,
                int(getattr(game, "max_round_rate", 0))
                - int(getattr(player, "round_rate", 0)),
            )
            total_amount = call_amount + int(amount)
            wallet = getattr(player, "wallet", None)
            if wallet is not None and hasattr(wallet, "authorize"):
                try:
                    await wallet.authorize(game.id, total_amount)
                except Exception:
                    self._logger.warning(
                        "Wallet authorization failed during raise",
                        extra={**log_context, "amount": total_amount},
                        exc_info=True,
                    )
                    return False
            player.round_rate = int(getattr(player, "round_rate", 0)) + total_amount
            player.total_bet = int(getattr(player, "total_bet", 0)) + total_amount
            game.pot = int(getattr(game, "pot", 0)) + total_amount
            game.max_round_rate = max(
                int(getattr(game, "max_round_rate", 0)),
                player.round_rate,
            )
            if hasattr(player, "has_acted"):
                player.has_acted = True
            if hasattr(game, "trading_end_user_id"):
                game.trading_end_user_id = getattr(player, "user_id", None)
            for other in getattr(game, "players", []):
                if other is player:
                    continue
                if getattr(other, "state", PlayerState.ACTIVE) == PlayerState.ACTIVE and hasattr(other, "has_acted"):
                    other.has_acted = False
            record_action(f"{mention}: رِیز {total_amount}$")
            return True

        self._logger.warning(
            "Unsupported action encountered during execution",
            extra=log_context,
        )
        return False

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
        prefix = self.STAGE_LOCK_PREFIX
        if not isinstance(prefix, str) or not prefix.startswith("stage:"):
            prefix = "stage:"
        return f"{prefix}{self._safe_int(chat_id)}"

    def _build_lock_context(
        self,
        *,
        chat_id: ChatId,
        game: Optional[Game],
        base: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = dict(base or {})
        context.setdefault("chat_id", self._safe_int(chat_id))
        if game is not None:
            game_id = getattr(game, "id", None)
            if game_id is not None:
                context.setdefault("game_id", game_id)
        return context

    def _log_long_hold_snapshot(
        self,
        *,
        lock_category: str,
        stage_label: str,
        chat_id: Optional[ChatId],
        game: Optional[Game],
        lock_key: str,
        elapsed: float,
        timeout: Optional[float],
    ) -> None:
        snapshot_stage = f"lock_snapshot_long_hold_{lock_category}" if lock_category else "lock_snapshot_long_hold_unknown"
        try:
            snapshot = self._lock_manager.detect_deadlock()
        except Exception:
            error_extra = self._log_extra(
                stage=f"{snapshot_stage}:capture_failed",
                chat_id=chat_id,
                game=game,
                event_type="lock_snapshot_capture_failed",
                lock_key=lock_key,
                lock_category=lock_category,
                elapsed=elapsed,
                timeout=timeout,
                operation_stage=stage_label,
            )
            self._logger.exception(
                "Failed to capture lock snapshot for long hold", extra=error_extra
            )
            return

        snapshot_extra = self._log_extra(
            stage=snapshot_stage,
            chat_id=chat_id,
            game=game,
            event_type="lock_snapshot_long_hold",
            lock_key=lock_key,
            lock_category=lock_category,
            elapsed=elapsed,
            timeout=timeout,
            operation_stage=stage_label,
        )
        self._logger.warning(
            "Lock snapshot (%s): %s",
            snapshot_stage,
            json.dumps(snapshot, ensure_ascii=False, default=str),
            extra=snapshot_extra,
        )

    @asynccontextmanager
    async def _trace_lock_guard(
        self,
        *,
        lock_key: str,
        chat_id: Optional[ChatId],
        game: Optional[Game],
        stage_label: str,
        event_stage_label: Optional[str] = None,
        timeout: Optional[float] = None,
        context: Optional[Mapping[str, Any]] = None,
        retry_without_timeout: bool = False,
        retry_stage_label: Optional[str] = None,
        retry_log_level: int = logging.WARNING,
        **guard_kwargs: Any,
    ) -> AsyncIterator[None]:
        if chat_id is not None:
            lock_context = self._build_lock_context(
                chat_id=chat_id, game=game, base=context
            )
        else:
            lock_context = dict(context or {})
            if game is not None and getattr(game, "id", None) is not None:
                lock_context.setdefault("game_id", getattr(game, "id"))
        lock_category = (
            self._lock_manager._resolve_lock_category(lock_key) or "unknown"
        )
        event_stage = event_stage_label or stage_label
        guard_context_manager = self._lock_manager.trace_guard

        def _resolve_chat_and_game_ids() -> Tuple[Optional[int], Optional[int]]:
            resolved_chat: Optional[int] = None
            if chat_id is not None:
                try:
                    resolved_chat = self._safe_int(chat_id)
                except Exception:  # pragma: no cover - defensive
                    resolved_chat = None
            elif "chat_id" in lock_context:
                try:
                    resolved_chat = self._safe_int(lock_context["chat_id"])
                except Exception:  # pragma: no cover - defensive
                    resolved_chat = None

            resolved_game: Optional[int] = None
            if game is not None and getattr(game, "id", None) is not None:
                resolved_game = getattr(game, "id")
            elif "game_id" in lock_context:
                candidate = lock_context["game_id"]
                if isinstance(candidate, int):
                    resolved_game = candidate
                else:
                    try:
                        resolved_game = int(candidate)  # pragma: no cover - defensive
                    except Exception:  # pragma: no cover - defensive
                        resolved_game = None

            return resolved_chat, resolved_game

        def _log_retry_snapshot() -> Tuple[Optional[int], Optional[int]]:
            safe_chat_id, game_id = _resolve_chat_and_game_ids()
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage,
                chat_id=chat_id,
                game=game,
                log_level=retry_log_level,
            )
            lock_level = self._lock_manager._resolve_level(lock_key, override=None)
            snapshot_context: Dict[str, Any] = dict(lock_context)
            snapshot_context.setdefault("lock_key", lock_key)
            snapshot_context.setdefault("lock_level", lock_level)
            snapshot_context.setdefault("chat_id", safe_chat_id)
            snapshot_context.setdefault("game_id", game_id)

            failure_stage_parts = ["engine_lock_failure", lock_key]
            if safe_chat_id is not None:
                failure_stage_parts.append(f"chat={safe_chat_id}")
            if game_id is not None:
                failure_stage_parts.append(f"game={game_id}")
            failure_stage = ":".join(str(part) for part in failure_stage_parts if part)
            self._lock_manager._log_lock_snapshot_on_timeout(
                failure_stage,
                level=retry_log_level,
                minimum_level=retry_log_level,
                extra=snapshot_context,
            )

            snapshot_stage_parts = ["chat_guard_timeout"]
            if safe_chat_id is not None:
                snapshot_stage_parts.append(f"chat={safe_chat_id}")
            if game_id is not None:
                snapshot_stage_parts.append(f"game={game_id}")
            snapshot_stage = ":".join(str(part) for part in snapshot_stage_parts if part)
            self._log_lock_snapshot(
                stage=snapshot_stage,
                level=retry_log_level,
                minimum_level=retry_log_level,
            )

            stage_name_parts = [retry_stage_label or f"chat_guard_timeout:{event_stage}"]
            if safe_chat_id is not None:
                stage_name_parts.append(f"chat={safe_chat_id}")
            if game_id is not None:
                stage_name_parts.append(f"game={game_id}")
            stage_name = ":".join(str(part) for part in stage_name_parts if part)
            log_extra = self._log_extra(
                stage=stage_name,
                chat_id=chat_id,
                game=game,
                event_type="chat_guard_timeout",
                timeout=timeout,
            )
            timeout_value = float("inf") if timeout is None else float(timeout)
            self._logger.warning(
                "Chat guard timed out after %.1fs for chat %s; retrying without timeout",
                timeout_value,
                safe_chat_id,
                extra=log_extra,
            )
            return safe_chat_id, game_id

        attempt_timeout = timeout
        first_attempt = True
        while True:
            current_timeout = attempt_timeout
            loop = asyncio.get_running_loop()
            start_time = loop.time()
            if current_timeout is None:
                timeout_repr: Any = "none"
            else:
                try:
                    timeout_repr = f"{float(current_timeout):.3f}s"
                except Exception:
                    timeout_repr = current_timeout
            start_extra = self._log_extra(
                stage=stage_label,
                chat_id=chat_id,
                game=game,
                event_type="critical_section_start",
                lock_key=lock_key,
                lock_category=lock_category,
                timeout=current_timeout,
            )
            self._logger.debug(
                "[LOCK_TRACE] START critical_section lock=%s stage=%s timeout=%s",
                lock_key,
                stage_label,
                timeout_repr,
                extra=start_extra,
            )
            entered = False
            try:
                async with guard_context_manager(
                    lock_key,
                    timeout=current_timeout,
                    context=lock_context,
                    **guard_kwargs,
                ):
                    entered = True
                    yield
                    break
            except TimeoutError:
                if (
                    retry_without_timeout
                    and first_attempt
                    and timeout is not None
                ):
                    _log_retry_snapshot()
                    attempt_timeout = None
                    first_attempt = False
                    continue
                raise
            finally:
                if not entered:
                    continue
                elapsed = loop.time() - start_time
                end_extra = self._log_extra(
                    stage=stage_label,
                    chat_id=chat_id,
                    game=game,
                    event_type="critical_section_end",
                    lock_key=lock_key,
                    lock_category=lock_category,
                    elapsed=elapsed,
                    timeout=current_timeout,
                )
                self._logger.debug(
                    "[LOCK_TRACE] END critical_section lock=%s stage=%s elapsed=%.3fs",
                    lock_key,
                    stage_label,
                    elapsed,
                    extra=end_extra,
                )
                if elapsed > _CRITICAL_SECTION_LONG_HOLD_THRESHOLD_SECONDS:
                    long_extra = self._log_extra(
                        stage=stage_label,
                        chat_id=chat_id,
                        game=game,
                        event_type="critical_section_long",
                        lock_key=lock_key,
                        lock_category=lock_category,
                        elapsed=elapsed,
                        timeout=current_timeout,
                    )
                    self._logger.warning(
                        "[LOCK_TRACE] LONG HOLD critical_section lock=%s stage=%s elapsed=%.3fs",
                        lock_key,
                        stage_label,
                        elapsed,
                        extra=long_extra,
                    )
                    self._log_long_hold_snapshot(
                        lock_category=lock_category,
                        stage_label=stage_label,
                        chat_id=chat_id,
                        game=game,
                        lock_key=lock_key,
                        elapsed=elapsed,
                        timeout=current_timeout,
                    )
        return

    def _invalidate_adaptive_report_cache(
        self,
        players: Iterable[Player],
        *,
        event_type: str,
        chat_id: Optional[ChatId] = None,
    ) -> None:
        if self._adaptive_player_report_cache is None:
            return
        player_ids = normalize_player_ids(players)
        if not player_ids:
            return
        normalized_chat = self._safe_int(chat_id) if chat_id is not None else None
        self._adaptive_player_report_cache.invalidate_on_event(
            player_ids, event_type, chat_id=normalized_chat
        )

    async def start_game(
        self, context: ContextTypes.DEFAULT_TYPE, game: Game, chat_id: ChatId
    ) -> None:
        """Begin a poker hand, delegating to the matchmaking service."""

        self._log_lock_snapshot(stage="before_start_game", level=logging.INFO)

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:start_game"
        event_stage_label = "start_game"

        async def _run_locked() -> None:
            await self._matchmaking_service.start_game(
                context=context,
                game=game,
                chat_id=chat_id,
                build_identity_from_player=self._build_identity_from_player,
            )

        try:
            # Migrated to _trace_lock_guard for audited stage lock acquisition
            async with self._trace_lock_guard(
                lock_key=self._stage_lock_key(chat_id),
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
            ):
                await _run_locked()
        except (asyncio.TimeoutError, TimeoutError):
            self._logger.error(
                "Timeout occurred while acquiring the lock for game start.",
                extra=self._log_extra(
                    stage="stage_lock_timeout:start_game",
                    chat_id=chat_id,
                    game=game,
                    event_type="start_game_lock_timeout",
                    lock_key=lock_key,
                ),
            )
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise
        except Exception:
            self._logger.exception(
                "Error while starting the game.",
                extra=self._log_extra(
                    stage="stage_lock_error:start_game",
                    chat_id=chat_id,
                    game=game,
                    event_type="start_game_lock_error",
                    lock_key=lock_key,
                ),
            )
            raise
        finally:
            self._logger.info(
                "Game start completed or failed, lock released for chat %s.",
                chat_id,
                extra=self._log_extra(
                    stage="stage_lock:release_notice",
                    chat_id=chat_id,
                    game=game,
                    event_type="start_game_lock_release",
                    lock_key=lock_key,
                ),
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
        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:add_cards_to_table"
        event_stage_label = "add_cards_to_table"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=False,
            ):
                await self._matchmaking_service.add_cards_to_table(
                    count=count,
                    game=game,
                    chat_id=chat_id,
                    street_name=street_name,
                    send_message=send_message,
                )
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

    async def emergency_reset(self, chat_id: int) -> None:
        """Forcefully unwind timers and locks after a critical failure."""

        normalized_chat = self._safe_int(chat_id)
        self._logger.warning(
            "EMERGENCY RESET TRIGGERED",
            extra={
                "chat_id": normalized_chat,
                "reason": "circuit breaker or watchdog escalation",
            },
        )

        try:
            await self._cancel_all_timers_internal(chat_id)
            await self._force_release_locks(chat_id)

            game = await self._table_manager.get_game(chat_id)
            if game:
                game.stage = GameState.WAITING
                game.current_bet = 0
                game.pot = 0
                game.active_players = []
                await self._table_manager.save_game(chat_id, game)

            reset_message = (
                "⚠️ **سیستم به حالت اضطراری بازنشانی شد**\n\n"
                "به دلیل شناسایی مشکل فنی، بازی متوقف و بازنشانی شد.\n"
                "می‌توانید بازی جدید را با دستور /start آغاز کنید.\n\n"
                "پوزش بابت ناراحتی ایجاد شده 🙏"
            )
            await self._telegram_ops.send_message_safe(
                call=lambda: self._view.send_message(
                    chat_id,
                    reset_message,
                    parse_mode="Markdown",
                ),
                chat_id=chat_id,
                operation="emergency_reset_notification",
            )

            self._logger.info(
                "Emergency reset completed",
                extra={"chat_id": normalized_chat},
            )
        except Exception as exc:
            self._logger.error(
                "Emergency reset failed",
                extra={
                    "chat_id": normalized_chat,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    async def _cancel_all_timers_internal(self, chat_id: int) -> None:
        """Cancel countdown timers for ``chat_id`` if they are active."""

        try:
            await self.cancel_prestart_countdown(chat_id)
            normalized_chat = self._safe_int(chat_id)
            self._logger.debug(
                "Cancelled all timers for chat", extra={"chat_id": normalized_chat}
            )
        except Exception as exc:
            self._logger.error(
                "Failed to cancel timers during emergency reset",
                extra={
                    "chat_id": self._safe_int(chat_id),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    async def _force_release_locks(self, chat_id: int) -> None:
        """Force release locks associated with ``chat_id`` (dangerous)."""

        try:
            normalized_chat = self._safe_int(chat_id)
            lock_manager = self._lock_manager
            lock_keys = {
                self._stage_lock_key(chat_id),
                f"chat:{normalized_chat}",
                f"engine_stage:{normalized_chat}",
                f"table:{normalized_chat}",
            }
            prefixes = (
                f"chat:{normalized_chat}",
                f"stage:{normalized_chat}",
                f"engine_stage:{normalized_chat}",
                f"table:{normalized_chat}",
            )

            for key, lock in list(lock_manager._locks.items()):  # pragma: no cover - safety
                if key in lock_keys or any(key.startswith(prefix) for prefix in prefixes):
                    reentrant_depth = getattr(lock, "_count", 0)
                    is_locked = bool(reentrant_depth)
                    inner_lock = getattr(lock, "_lock", None)
                    if not is_locked and inner_lock is not None:
                        is_locked = getattr(inner_lock, "locked", lambda: False)()
                    if not is_locked:
                        continue
                    try:
                        lock.release()
                        reset_extra = {
                            "chat_id": normalized_chat,
                            "lock_key": key,
                        }
                        lock_manager._timeout_count.pop(key, None)
                        lock_manager._circuit_reset_time.pop(key, None)
                        lock_manager._bypassed_locks.discard(key)
                        self._logger.warning(
                            "Force-released lock during emergency reset",
                            extra=reset_extra,
                        )
                    except Exception as exc:
                        self._logger.error(
                            "Failed to force-release lock",
                            extra={
                                "chat_id": normalized_chat,
                                "lock_key": key,
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )

            self._logger.debug(
                "All relevant locks released for chat",
                extra={"chat_id": normalized_chat},
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.error(
                "Failed to force-release locks during emergency reset",
                extra={
                    "chat_id": self._safe_int(chat_id),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    async def _start_prestart_countdown(
        self,
        chat_id: int,
        duration_seconds: int = 10,
        *,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None,
    ) -> None:
        """Start or restart the pre-game countdown using the queue system."""

        await self._countdown_queue.cancel_countdown_for_chat(chat_id)

        if context is not None:
            self._countdown_contexts[chat_id] = context

        game = await self._table_manager.get_game(chat_id)
        if game is None:
            self._logger.warning(
                "Cannot start countdown: no active game",
                extra={"chat_id": self._safe_int(chat_id)},
            )
            return

        message_id = getattr(game, "ready_message_main_id", None)
        if not message_id:
            self._logger.warning(
                "Cannot start countdown: no anchor message",
                extra={"chat_id": self._safe_int(chat_id)},
            )
            return

        def format_countdown(remaining: float) -> str:
            seconds = max(0, int(remaining))
            if seconds == 0:
                return "🎮 Game starting now!"
            return f"⏳ Game starts in {seconds} second{'s' if seconds != 1 else ''}..."

        async def on_countdown_complete() -> None:
            try:
                await self._handle_countdown_completion(chat_id)
            except Exception:
                self._logger.exception(
                    "Error in countdown completion handler",
                    extra={"chat_id": self._safe_int(chat_id)},
                )

        try:
            await self._countdown_queue.enqueue(
                chat_id=chat_id,
                message_id=message_id,
                text="⏳ Starting...",
                duration_seconds=float(duration_seconds),
                formatter=format_countdown,
                on_complete=on_countdown_complete,
            )
        except asyncio.QueueFull:
            self._logger.error(
                "Countdown queue is full",
                extra={"chat_id": self._safe_int(chat_id)},
            )
            return

        self._logger.info(
            "Prestart countdown enqueued",
            extra={
                "chat_id": self._safe_int(chat_id),
                "message_id": message_id,
                "duration": duration_seconds,
            },
        )

    async def _handle_countdown_completion(self, chat_id: int) -> None:
        """Called when pre-start countdown finishes."""

        context = self._countdown_contexts.pop(chat_id, None)

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:countdown_completion"
        event_stage_label = "countdown_completion"

        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=None,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
            ):
                game = await self._table_manager.get_game(chat_id)
                if game is None:
                    self._logger.debug(
                        "Countdown completed but game not found",
                        extra={"chat_id": self._safe_int(chat_id)},
                    )
                    return

                if game.state != GameState.WAITING:
                    self._logger.debug(
                        "Countdown completed but game is no longer waiting",
                        extra={
                            "chat_id": self._safe_int(chat_id),
                            "state": getattr(game.state, "name", str(game.state)),
                        },
                    )
                    return

                seated_players = [
                    player for player in getattr(game, "players", []) if player is not None
                ]
                seated_count = len(seated_players)
                if seated_count < 2:
                    self._logger.warning(
                        "Countdown completed but not enough players",
                        extra={
                            "chat_id": self._safe_int(chat_id),
                            "seated_count": seated_count,
                        },
                    )
                    try:
                        await self._player_manager.send_join_prompt(game, chat_id)
                    except Exception:
                        self._logger.exception(
                            "Failed to refresh join prompt after countdown",
                            extra={"chat_id": self._safe_int(chat_id)},
                        )
                    finally:
                        try:
                            await self._table_manager.save_game(chat_id, game)
                        except Exception:
                            self._logger.exception(
                                "Failed to persist game after countdown cancellation",
                                extra={"chat_id": self._safe_int(chat_id)},
                            )
                    return

                if context is None:
                    self._logger.warning(
                        "Countdown completed without context to start game",
                        extra={
                            "chat_id": self._safe_int(chat_id),
                            "seated_count": seated_count,
                        },
                    )
                    return

                await self.start_game(context, game, chat_id)
        except Exception:
            self._logger.exception(
                "Error while handling countdown completion",
                extra={"chat_id": self._safe_int(chat_id)},
            )

    async def cancel_prestart_countdown(self, chat_id: int) -> None:
        """Cancel any active countdown for this chat."""

        self._countdown_contexts.pop(chat_id, None)
        cancelled = await self._countdown_queue.cancel_countdown_for_chat(chat_id)

        try:
            game = await self._table_manager.get_game(chat_id)
        except Exception:
            game = None

        message_id = getattr(game, "ready_message_main_id", None) if game else None
        if message_id is not None:
            self._countdown_queue.cancel_countdown(chat_id, message_id)
            self._logger.debug(
                "Cancelled prestart countdown",
                extra={
                    "chat_id": self._safe_int(chat_id),
                    "message_id": message_id,
                    "cancelled_entries": cancelled,
                },
            )
        elif cancelled:
            self._logger.debug(
                "Cancelled prestart countdown entries without anchor",
                extra={
                    "chat_id": self._safe_int(chat_id),
                    "cancelled_entries": cancelled,
                },
            )

    async def start(self) -> None:
        """Start background workers managed by the game engine.

        This should be called once during application initialization,
        after the :class:`GameEngine` is constructed.
        """

        await self._countdown_worker.start()
        self._logger.info("GameEngine background workers started")

    async def shutdown(self) -> None:
        """Stop background workers managed by the game engine.

        This should be called during application shutdown to ensure
        graceful cleanup of countdown tasks.
        """

        await self._countdown_worker.stop()
        self._logger.info("GameEngine background workers stopped")

    async def progress_stage(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: ChatId,
        game: Game,
    ) -> bool:
        async def _progress_locked() -> bool:
            return await self._matchmaking_service.progress_stage(
                context=context,
                chat_id=chat_id,
                game=game,
                finalize_game=self.finalize_game,
            )

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:progress_stage"
        event_stage_label = "progress_stage"
        # Migrated to _trace_lock_guard for audited stage lock acquisition
        result = False
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
            ):
                result = await _progress_locked()
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

        self.refresh_turn_deadline(game)
        return result

    async def finalize_game(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        async def _finalize_locked() -> Tuple[
            Set[MessageId],
            List[Dict[str, Any]],
            Optional[Dict[str, Any]],
            Optional[_ResetNotifications],
        ]:
            message_cleanup_local: Set[MessageId] = set()
            announcements_local: List[Dict[str, Any]] = []
            stats_payload_local: Optional[Dict[str, Any]] = None
            reset_notifications_local: Optional[_ResetNotifications] = None

            game.chat_id = chat_id
            players_snapshot = list(game.players)

            collected_ids = await self._clear_game_messages(
                game, chat_id, collect_only=True
            )
            if collected_ids:
                message_cleanup_local = set(collected_ids)

            pot_total = game.pot
            game_id = getattr(game, "id", None)

            payouts, hand_labels, announcements_local = await self._determine_winners(
                game=game,
                chat_id=chat_id,
            )

            await self._execute_payouts(game=game, payouts=payouts)

            stats_payload_local = self._prepare_hand_statistics(
                chat_id=chat_id,
                payouts=payouts,
                hand_labels=hand_labels,
                pot_total=pot_total,
                players_snapshot=players_snapshot,
                game_id=game_id,
            )

            reset_notifications_local = await self._reset_game_state(
                game=game,
                context=context,
                chat_id=chat_id,
                game_id=game_id,
                defer_notifications=True,
            )

            return (
                message_cleanup_local,
                announcements_local,
                stats_payload_local,
                reset_notifications_local,
            )

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:finalize_game"
        event_stage_label = "finalize_game"
        retry_stage_label = "chat_guard_timeout:finalize_game"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=True,
                retry_stage_label=retry_stage_label,
            ):
                (
                    message_cleanup_ids,
                    announcements,
                    stats_payload,
                    reset_notifications,
                ) = await _finalize_locked()
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

        if message_cleanup_ids:
            await self._delete_chat_messages(chat_id, message_cleanup_ids)

        if announcements:
            await self._notify_results(
                chat_id=chat_id,
                announcements=announcements,
            )

        if stats_payload is not None:
            await self._record_hand_results(statistics=stats_payload)

        if reset_notifications is not None:
            await self._announce_new_hand_ready(
                chat_id=reset_notifications.chat_id,
                game_id=reset_notifications.game_id,
            )

        await self._player_manager.send_join_prompt(game, chat_id)

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

    async def _delete_chat_messages(
        self, chat_id: ChatId, message_ids: Iterable[MessageId]
    ) -> None:
        for message_id in message_ids:
            try:
                await self._view.delete_message(chat_id, message_id)
            except Exception as error:
                self._logger.debug(
                    "Failed to delete message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(error).__name__,
                    },
                )

    async def _announce_new_hand_ready(
        self, *, chat_id: ChatId, game_id: Optional[int]
    ) -> None:
        await self._telegram_ops.send_message_safe(
            call=lambda: self._view.send_new_hand_ready_message(chat_id),
            chat_id=chat_id,
            operation="send_new_hand_ready_message",
            log_extra=self._build_telegram_log_extra(
                chat_id=chat_id,
                message_id=None,
                game_id=game_id,
                operation="send_new_hand_ready_message",
                request_category=RequestCategory.START_GAME,
            ),
        )

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

    def _prepare_hand_statistics(
        self,
        *,
        chat_id: ChatId,
        payouts: Mapping[int, int],
        hand_labels: Mapping[int, Optional[str]],
        pot_total: int,
        players_snapshot: Iterable[Player],
        game_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """Prepare statistics payload for deferred persistence.

        Runs entirely within the stage lock but avoids any blocking I/O so
        that the resulting snapshot can be handed off once the lock is
        released.
        """

        players_list = [player for player in players_snapshot if player is not None]
        if not players_list:
            return None

        snapshot = _GameStatsSnapshot(game_id, tuple(players_list))
        stats_kwargs: Dict[str, Any] = {
            "game": snapshot,
            "chat_id": chat_id,
            "payouts": dict(payouts),
            "hand_labels": dict(hand_labels),
            "pot_total": pot_total,
        }
        return {
            "players": players_list,
            "stats_kwargs": stats_kwargs,
        }

    async def _record_hand_results(
        self,
        *,
        statistics: Optional[Dict[str, Any]],
    ) -> None:
        if not statistics:
            return

        players_list = list(statistics.get("players", []))
        stats_kwargs = statistics.get("stats_kwargs") if statistics else None
        chat_scope: Optional[int] = None
        if isinstance(stats_kwargs, dict):
            raw_chat = stats_kwargs.get("chat_id")
            if raw_chat is not None:
                try:
                    chat_scope = self._safe_int(raw_chat)
                except Exception:
                    chat_scope = None
        if players_list:
            self._invalidate_adaptive_report_cache(
                players_list,
                event_type="hand_finished",
                chat_id=chat_scope,
            )

        if not isinstance(stats_kwargs, dict):
            return

        game_snapshot = stats_kwargs.get("game")
        try:
            await self._stats_reporter.hand_finished_deferred(**stats_kwargs)
        except Exception:
            resolved_chat: Optional[int] = None
            if "chat_id" in stats_kwargs:
                try:
                    resolved_chat = self._safe_int(stats_kwargs["chat_id"])
                except Exception:  # pragma: no cover - defensive logging
                    try:
                        resolved_chat = int(stats_kwargs["chat_id"])  # type: ignore[arg-type]
                    except Exception:  # pragma: no cover - fallback to None
                        resolved_chat = None
            log_extra = {
                "chat_id": resolved_chat,
                "game_id": getattr(game_snapshot, "id", None),
            }
            self._logger.error(
                "Failed to record deferred hand statistics",
                extra=log_extra,
                exc_info=True,
            )
        else:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "Queued hand statistics for chat_id=%s hand_id=%s",
                    chat_scope,
                    getattr(game_snapshot, "id", None),
                )

    async def _reset_game_state(
        self,
        *,
        game: Game,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: ChatId,
        game_id: Optional[int],
        defer_notifications: bool = False,
    ) -> Optional[_ResetNotifications]:
        await self._reset_core_game_state(
            game,
            context=context,
            chat_id=chat_id,
            send_stop_notification=False,
        )

        if defer_notifications:
            return _ResetNotifications(chat_id=chat_id, game_id=game_id)

        await self._announce_new_hand_ready(chat_id=chat_id, game_id=game_id)
        await self._player_manager.send_join_prompt(game, chat_id)
        return None

    async def _reset_game_state_after_round(
        self,
        *,
        chat_id: ChatId,
        game: Game,
    ) -> None:
        """
        Reset post-round bookkeeping while holding the stage lock.

        The method currently clears cached Telegram message identifiers and
        emits a structured log entry. Additional state-reset steps can be added
        over time without exposing partially reset state to concurrent
        callbacks.
        """

        async def _run_locked() -> None:
            clear_all_message_ids(game)
            game.reset_bets()
            game.rotate_dealer()
            await self._player_manager.reseat_players(game)
            if hasattr(game, "increment_callback_version"):
                game.increment_callback_version()
            self._logger.info(
                "Game state reset after round",
                extra=self._log_extra(
                    stage="reset_game_state_after_round", game=game, chat_id=chat_id
                ),
            )

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:reset_game_state_after_round"
        event_stage_label = "reset_game_state_after_round"

        try:
            # Migrated to _trace_lock_guard for audited stage lock acquisition with retries
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=True,
                retry_stage_label="chat_guard_timeout:reset_game_state_after_round",
            ):
                await _run_locked()
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

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
                    chat_id, winners, game=game
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
        clear_all_message_ids(game)
        await self._player_manager.clear_player_anchors(game)
        game.reset()
        if hasattr(game, "increment_callback_version"):
            game.increment_callback_version()
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
                    request_category=RequestCategory.ENGINE_CRITICAL,
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

        async def _stop_locked() -> None:
            if game.state == GameState.INITIAL:
                await self._player_manager.cleanup_ready_prompt(game, chat_id)
                clear_all_message_ids(game)
                await self._table_manager.save_game(chat_id, game)
                raise UserException(self.ERROR_NO_ACTIVE_GAME)

            if not any(
                player.user_id == requester_id for player in game.seated_players()
            ):
                raise UserException(self.ERROR_NOT_IN_GAME)

            await self.request_stop(
                context=context,
                game=game,
                chat_id=chat_id,
                requester_id=requester_id,
            )
            await self._table_manager.save_game(chat_id, game)

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:stop_game"
        event_stage_label = "stop_game"
        retry_stage_label = "chat_guard_timeout:stop_game"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=True,
                retry_stage_label=retry_stage_label,
            ):
                await _stop_locked()
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

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
            current_game_id=getattr(game, "id", None),
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

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:confirm_stop_vote"
        event_stage_label = "confirm_stop_vote"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=False,
            ):
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
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

    async def resume_stop_vote(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        """Cancel a pending stop request and resume play."""

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:resume_stop_vote"
        event_stage_label = "resume_stop_vote"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=False,
            ):
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
                    current_game_id=getattr(game, "id", None),
                )
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

    async def cancel_hand(
        self,
        game: Game,
        chat_id: ChatId,
        context: ContextTypes.DEFAULT_TYPE,
        stop_request: Dict[str, object],
    ) -> None:
        """Cancel the current hand, refund players, and reset the game."""

        async def _cancel_locked() -> None:
            original_game_id = game.id
            players_snapshot = list(game.seated_players())

            await self._refund_players(
                players_snapshot, original_game_id, chat_id=chat_id
            )

            await self._finalize_stop_request(
                context=context,
                chat_id=chat_id,
                stop_request=stop_request,
                game=game,
            )

            await self._reset_game_state_after_stop(
                game=game, chat_id=chat_id, context=context
            )

        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:cancel_hand"
        event_stage_label = "cancel_hand"
        retry_stage_label = "chat_guard_timeout:cancel_hand"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=True,
                retry_stage_label=retry_stage_label,
            ):
                await _cancel_locked()
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

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
            current_game_id=getattr(game, "id", None),
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
        self,
        players: Iterable[Player],
        original_game_id: str,
        *,
        chat_id: Optional[ChatId] = None,
    ) -> None:
        player_list = list(players)
        for player in player_list:
            if player.wallet:
                await player.wallet.cancel(original_game_id)

        self._invalidate_adaptive_report_cache(
            player_list,
            event_type="hand_finished",
            chat_id=chat_id,
        )

        await self._stats_reporter.invalidate_players(
            player_list,
            chat_id=self._safe_int(chat_id) if chat_id is not None else None,
            event_type="hand_finished",
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
        game: Optional[Game] = None,
    ) -> None:
        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:finalize_stop_request"
        event_stage_label = "_finalize_stop_request"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=False,
            ):
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
                    current_game_id=stop_request.get("game_id"),
                )

                context.chat_data.pop(self.KEY_STOP_REQUEST, None)
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise

    async def _reset_game_state_after_stop(
        self,
        *,
        game: Game,
        chat_id: ChatId,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        lock_key = self._stage_lock_key(chat_id)
        stage_label = "stage_lock:reset_game_state_after_stop"
        event_stage_label = "reset_game_state_after_stop"
        try:
            async with self._trace_lock_guard(
                lock_key=lock_key,
                chat_id=chat_id,
                game=game,
                stage_label=stage_label,
                event_stage_label=event_stage_label,
                timeout=self._stage_lock_timeout,
                retry_without_timeout=False,
            ):
                await self._player_manager.cleanup_ready_prompt(
                    game, chat_id, persist=False
                )
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
        except TimeoutError:
            self._log_engine_event_lock_failure(
                lock_key=lock_key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
            )
            raise
