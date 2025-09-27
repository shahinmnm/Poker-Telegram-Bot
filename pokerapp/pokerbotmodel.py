#!/usr/bin/env python3

import asyncio
import datetime
import inspect
import json
import math
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, Iterable, List, Optional, Set, Tuple

from cachetools import LRUCache

import redis.asyncio as aioredis
from redis.exceptions import NoScriptError
from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    Bot,
    User,
)
from telegram.constants import ParseMode
from telegram.ext import CallbackContext, ContextTypes
from telegram.helpers import mention_markdown as format_mention_markdown

import logging

from pokerapp.config import Config, DEFAULT_TIMEZONE_NAME, get_game_constants
from pokerapp.utils.datetime_utils import utc_isoformat
from pokerapp.utils.time_utils import format_local, now_utc
from pokerapp.winnerdetermination import WinnerDetermination
from pokerapp.cards import Cards
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    ChatId,
    UserId,
    MessageId,
    UserException,
    Money,
    PlayerState,
    PlayerAction,
    Score,
    Wallet,
    Mention,
    DEFAULT_MONEY,
    SMALL_BLIND,
    MIN_PLAYERS,
    MAX_PLAYERS,
)
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.utils.markdown import escape_markdown_v1
from pokerapp.table_manager import TableManager
from pokerapp.stats import (
    BaseStatsService,
    NullStatsService,
    PlayerHandResult,
    PlayerIdentity,
)
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.utils.cache import AdaptivePlayerReportCache
from pokerapp.utils.player_report_cache import (
    PlayerReportCache as RedisPlayerReportCache,
)
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics
from pokerapp.utils.redis_safeops import RedisSafeOps
from pokerapp.lock_manager import LockManager
from pokerapp.player_identity_manager import PlayerIdentityManager
from pokerapp.player_manager import PlayerManager
from pokerapp.matchmaking_service import MatchmakingService
from pokerapp.stats_reporter import StatsReporter
from pokerapp.game_engine import GameEngine
from pokerapp.utils.telegram_safeops import TelegramSafeOps

_GAME_CONSTANTS = get_game_constants()
_GAME_SECTION = _GAME_CONSTANTS.game
_UI_SECTION = _GAME_CONSTANTS.ui
_ENGINE_SECTION = _GAME_CONSTANTS.engine
_REDIS_KEYS = _GAME_CONSTANTS.redis_keys
_EMOJI_SECTION = _GAME_CONSTANTS.emojis
if isinstance(_REDIS_KEYS, dict):
    _ENGINE_REDIS_KEYS = _REDIS_KEYS.get("engine", {})
    if not isinstance(_ENGINE_REDIS_KEYS, dict):
        _ENGINE_REDIS_KEYS = {}
else:
    _ENGINE_REDIS_KEYS = {}

DICE_MULT = int(_GAME_SECTION.get("dice_mult", 10))
DICE_DELAY_SEC = int(_GAME_SECTION.get("dice_delay_sec", 5))
BONUSES = tuple(_GAME_SECTION.get("bonuses", (5, 20, 40, 80, 160, 320)))
if isinstance(_EMOJI_SECTION, dict):
    _DICE_EMOJIS = _EMOJI_SECTION.get("dice", {})
    if not isinstance(_DICE_EMOJIS, dict):
        _DICE_EMOJIS = {}
else:
    _DICE_EMOJIS = {}
_DICE_SEQUENCE = _DICE_EMOJIS.get("sequence")
if not isinstance(_DICE_SEQUENCE, str) or not _DICE_SEQUENCE:
    _DICE_FACES = _DICE_EMOJIS.get("faces")
    if isinstance(_DICE_FACES, list) and _DICE_FACES:
        _DICE_SEQUENCE = "".join(
            str(face) for face in _DICE_FACES if isinstance(face, str)
        )
if not isinstance(_DICE_SEQUENCE, str) or not _DICE_SEQUENCE:
    _DICE_SEQUENCE = _GAME_SECTION.get("dices", "⚀⚁⚂⚃⚄⚅")
DICES = _DICE_SEQUENCE
_DICE_ROLL_EMOJI = _DICE_EMOJIS.get("roll", "🎲")

AUTO_START_MAX_UPDATES_PER_MINUTE = (
    GameEngine.AUTO_START_MAX_UPDATES_PER_MINUTE
)
AUTO_START_MIN_UPDATE_INTERVAL = GameEngine.AUTO_START_MIN_UPDATE_INTERVAL
KEY_START_COUNTDOWN_LAST_TEXT = GameEngine.KEY_START_COUNTDOWN_LAST_TEXT
KEY_START_COUNTDOWN_LAST_TIMESTAMP = (
    GameEngine.KEY_START_COUNTDOWN_LAST_TIMESTAMP
)
KEY_START_COUNTDOWN_CONTEXT = GameEngine.KEY_START_COUNTDOWN_CONTEXT
KEY_START_COUNTDOWN_ANCHOR = GameEngine.KEY_START_COUNTDOWN_ANCHOR
KEY_START_COUNTDOWN_INITIAL_SECONDS = (
    GameEngine.KEY_START_COUNTDOWN_INITIAL_SECONDS
)

# legacy keys kept for backward compatibility but unused
KEY_OLD_PLAYERS = _ENGINE_SECTION.get("key_old_players", "old_players")
KEY_CHAT_DATA_GAME = _ENGINE_SECTION.get("key_chat_data_game", "game")
KEY_STOP_REQUEST = GameEngine.KEY_STOP_REQUEST

STOP_CONFIRM_CALLBACK = GameEngine.STOP_CONFIRM_CALLBACK
STOP_RESUME_CALLBACK = GameEngine.STOP_RESUME_CALLBACK

STAGE_LOCK_PREFIX = _ENGINE_REDIS_KEYS.get(
    "stage_lock_prefix",
    GameEngine.STAGE_LOCK_PREFIX,
)

_LOCKS_SECTION = _GAME_CONSTANTS.section("locks")
_CATEGORY_TIMEOUTS = {}
if isinstance(_LOCKS_SECTION, dict):
    candidate_timeouts = _LOCKS_SECTION.get("category_timeouts_seconds")
    if isinstance(candidate_timeouts, dict):
        _CATEGORY_TIMEOUTS = candidate_timeouts


def _coerce_positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


_CHAT_GUARD_TIMEOUT_SECONDS = _coerce_positive_float(
    _CATEGORY_TIMEOUTS.get("chat"), 15.0
)

# MAX_PLAYERS = 8 (Defined in entities)
# MIN_PLAYERS = 2 (Defined in entities)
# SMALL_BLIND = 5 (Defined in entities)
# DEFAULT_MONEY = 1000 (Defined in entities)
MAX_TIME_FOR_TURN = GameEngine.MAX_TIME_FOR_TURN
DESCRIPTION_FILE = _UI_SECTION.get("description_file", "assets/description_bot.md")

logger = logging.getLogger(__name__)




@dataclass(slots=True)
class _CountdownCacheEntry:
    message_id: Optional[MessageId]
    countdown: Optional[int]
    text: str
    updated_at: datetime.datetime


class PokerBotModel:
    ACTIVE_GAME_STATES = GameEngine.ACTIVE_GAME_STATES

    @staticmethod
    def _safe_int(value: UserId) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    def _state_token(self, state: Any) -> str:
        return self._game_engine.state_token(state)

    def __init__(
        self,
        view: PokerBotViewer,
        bot: Bot,
        cfg: Config,
        kv: aioredis.Redis,
        table_manager: TableManager,
        private_match_service: PrivateMatchService,
        stats_service: Optional[BaseStatsService] = None,
        *,
        redis_ops: Optional[RedisSafeOps] = None,
        player_report_cache: Optional[RedisPlayerReportCache] = None,
        adaptive_player_report_cache: Optional[AdaptivePlayerReportCache] = None,
        telegram_safe_ops: Optional[TelegramSafeOps] = None,
    ):
        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._cfg: Config = cfg
        self._logger = logger.getChild("model")
        self._constants = cfg.constants
        self._kv = kv
        self._redis_ops = redis_ops or RedisSafeOps(
            kv, logger=logger.getChild("redis_safeops")
        )
        self._shared_player_report_cache = (
            player_report_cache
            if player_report_cache is not None
            else RedisPlayerReportCache(self._redis_ops, logger=logger)
        )
        self._player_report_cache_ttl = max(
            int(getattr(cfg, "PLAYER_REPORT_CACHE_TTL", 300) or 0), 0
        )
        self._table_manager = table_manager
        self._private_match_service = private_match_service
        self._winner_determine: WinnerDetermination = WinnerDetermination()
        self._round_rate = RoundRateModel(view=self._view, kv=self._kv, model=self)
        self._messaging_service = getattr(view, "_messaging_service", None)
        if self._messaging_service is None:
            self._messaging_service = getattr(view, "_messenger", None)
        def _resolve_ttl(attribute: str, fallback: int) -> int:
            raw_value = getattr(cfg, attribute, fallback)
            try:
                parsed = int(raw_value)
            except (TypeError, ValueError):
                return fallback
            return max(parsed, 0)

        self._player_report_cache = adaptive_player_report_cache or AdaptivePlayerReportCache(
            logger_=logger.getChild("player_report_cache"),
            persistent_store=self._redis_ops,
            default_ttl=_resolve_ttl("PLAYER_REPORT_TTL_DEFAULT", 120),
            bonus_ttl=_resolve_ttl("PLAYER_REPORT_TTL_BONUS", 60),
            post_hand_ttl=_resolve_ttl("PLAYER_REPORT_TTL_POST_HAND", 45),
        )
        cfg_timezone = getattr(cfg, "TIMEZONE_NAME", DEFAULT_TIMEZONE_NAME)
        if not isinstance(cfg_timezone, str) or not cfg_timezone.strip():
            cfg_timezone = DEFAULT_TIMEZONE_NAME
        if stats_service is not None:
            self._stats: BaseStatsService = stats_service
            cfg_timezone = getattr(stats_service, "timezone_name", cfg_timezone)
        else:
            self._stats = NullStatsService(timezone_name=cfg_timezone)
        if not isinstance(cfg_timezone, str) or not cfg_timezone.strip():
            cfg_timezone = DEFAULT_TIMEZONE_NAME
        self._timezone_name = cfg_timezone
        self._stats.bind_player_report_cache(self._player_report_cache)
        self._lock_manager = LockManager(
            logger=logger.getChild("lock_manager"),
            category_timeouts=getattr(cfg, "LOCK_TIMEOUTS", None),
            config=cfg,
        )
        self._chat_guard_timeout_seconds = _CHAT_GUARD_TIMEOUT_SECONDS
        self._player_identity_manager = PlayerIdentityManager(
            table_manager=self._table_manager,
            kv=self._kv,
            stats_service=self._stats,
            player_report_cache=self._player_report_cache,
            shared_report_cache=self._shared_player_report_cache,
            shared_report_ttl=self._player_report_cache_ttl,
            view=self._view,
            build_private_menu=self._build_private_menu,
            logger=logger.getChild("player_identity"),
        )
        self._player_manager = PlayerManager(
            view=self._view,
            table_manager=self._table_manager,
            logger=logger.getChild("player_lifecycle"),
        )
        self._stats_reporter = StatsReporter(
            stats_service=self._stats,
            player_report_cache=self._shared_player_report_cache,
            adaptive_player_report_cache=self._player_report_cache,
            safe_int=self._safe_int,
            logger=logger.getChild("stats_reporter"),
        )
        self._private_chat_ids = self._player_identity_manager.private_chat_ids
        self._countdown_cache: LRUCache[int, _CountdownCacheEntry] = LRUCache(
            maxsize=64, getsizeof=lambda entry: 1
        )
        self._countdown_cache_lock = asyncio.Lock()
        metrics_candidate = getattr(self._view, "request_metrics", None)
        if not isinstance(metrics_candidate, RequestMetrics):
            raise ValueError("PokerBotViewer must expose a RequestMetrics instance")
        self._request_metrics = metrics_candidate
        self._matchmaking_service = MatchmakingService(
            view=self._view,
            round_rate=self._round_rate,
            request_metrics=self._request_metrics,
            player_manager=self._player_manager,
            stats_reporter=self._stats_reporter,
            lock_manager=self._lock_manager,
            send_turn_message=self._send_turn_message,
            safe_int=self._safe_int,
            old_players_key=KEY_OLD_PLAYERS,
            logger=logger.getChild("matchmaking"),
            config=cfg,
        )
        self._telegram_ops = telegram_safe_ops or TelegramSafeOps(
            self._view,
            logger=logger.getChild("telegram_safeops"),
            max_retries=getattr(cfg, "TELEGRAM_MAX_RETRIES", 3),
            base_delay=getattr(cfg, "TELEGRAM_RETRY_BASE_DELAY", 0.5),
            max_delay=getattr(cfg, "TELEGRAM_RETRY_MAX_DELAY", 4.0),
            backoff_multiplier=getattr(cfg, "TELEGRAM_RETRY_MULTIPLIER", 2.0),
        )
        self._private_match_service.configure(
            safe_int=self._safe_int,
            build_private_menu=self._build_private_menu,
            view=self._view,
            player_manager=self._player_identity_manager,
            request_metrics=self._request_metrics,
            stats_service=self._stats,
            stats_enabled=self._stats_enabled,
            build_identity_from_player=self._build_identity_from_player,
            clear_player_anchors=self._clear_player_anchors,
            wallet_factory=lambda user_id: WalletManagerModel(user_id, self._kv),
        )
        self._game_engine = GameEngine(
            table_manager=self._table_manager,
            view=self._view,
            winner_determination=self._winner_determine,
            request_metrics=self._request_metrics,
            round_rate=self._round_rate,
            player_manager=self._player_manager,
            matchmaking_service=self._matchmaking_service,
            stats_reporter=self._stats_reporter,
            clear_game_messages=self._clear_game_messages,
            build_identity_from_player=self._build_identity_from_player,
            safe_int=self._safe_int,
            old_players_key=KEY_OLD_PLAYERS,
            telegram_safe_ops=self._telegram_ops,
            lock_manager=self._lock_manager,
            logger=logger.getChild("game_engine"),
            adaptive_player_report_cache=self._player_report_cache,
        )

        self._log_lock_snapshot(stage="startup", level=logging.INFO)

    @property
    def _min_players(self):
        return 1 if self._cfg.DEBUG else MIN_PLAYERS

    def _stats_enabled(self) -> bool:
        return not isinstance(self._stats, NullStatsService)

    def _log_lock_snapshot(self, stage: str, *, level: int = logging.DEBUG) -> None:
        try:
            snapshot = self._lock_manager.detect_deadlock()
        except Exception:
            logger.exception(
                "Failed to capture lock snapshot", extra={"stage": stage}
            )
            return

        if not snapshot.get("tasks") and not snapshot.get("waiting"):
            level = logging.DEBUG if level > logging.DEBUG else level

        logger.log(
            level,
            "Lock snapshot (%s): %s",
            stage,
            json.dumps(snapshot, ensure_ascii=False, default=str),
            extra={"stage": stage, "event_type": "lock_snapshot"},
        )

    async def handle_admin_command(
        self, command: str, args: list[str], admin_chat_id: Optional[int]
    ) -> None:
        """Handle administrative commands issued via the configured admin chat."""

        if admin_chat_id is None:
            return

        messaging_service = getattr(self, "_messaging_service", None)
        if messaging_service is None:
            messaging_service = getattr(self._view, "_messaging_service", None)
        if messaging_service is None:
            messaging_service = getattr(self._view, "_messenger", None)

        async def _send_message(text: str) -> None:
            if messaging_service is not None:
                await messaging_service.send_message(
                    chat_id=admin_chat_id,
                    text=text,
                    request_category=RequestCategory.GENERAL,
                    context={"admin_chat_id": admin_chat_id, "command": command},
                )
                return
            await self._view.send_message(
                admin_chat_id,
                text,
                request_category=RequestCategory.GENERAL,
            )

        if command == "/get_save_error":
            if not args:
                await _send_message("Usage: /get_save_error <chat_id> [detailed]")
                return

            try:
                chat_id_val = int(args[0])
            except (TypeError, ValueError):
                await _send_message(f"Invalid chat_id: {args[0]}")
                return

            detailed_flag = False
            if len(args) > 1:
                flag = args[1]
                if not isinstance(flag, str):
                    flag = str(flag)
                detailed_flag = flag.lower() == "detailed"

            if messaging_service is None:
                await self._view.send_message(
                    admin_chat_id,
                    "Messaging service unavailable; cannot retrieve save errors.",
                    request_category=RequestCategory.GENERAL,
                )
                self._logger.warning(
                    "Messaging service unavailable for admin command",
                    extra={"command": command},
                )
                return

            await messaging_service.send_last_save_error_to_admin(
                admin_chat_id=admin_chat_id,
                chat_id=chat_id_val,
                detailed=detailed_flag,
            )
            return

    @asynccontextmanager
    async def _chat_guard(
        self,
        chat_id: ChatId,
        *,
        event_stage_label: str = "chat_guard",
        game: Optional[Game] = None,
    ):
        """Serialize stateful operations for a chat while allowing nesting."""

        key = f"chat:{self._safe_int(chat_id)}"
        timeout_seconds = self._chat_guard_timeout_seconds
        try:
            async with self._lock_manager.guard(
                key,
                timeout=timeout_seconds,
                level=0,
                failure_log_level=logging.WARNING,
            ):
                yield
                return
        except TimeoutError:
            self._game_engine._log_engine_event_lock_failure(
                lock_key=key,
                event_stage_label=event_stage_label,
                chat_id=chat_id,
                game=game,
                log_level=logging.WARNING,
            )
            logger.warning(
                "Chat guard timed out after %.1fs for chat %s; retrying without timeout",
                timeout_seconds,
                self._safe_int(chat_id),
            )

        async with self._lock_manager.guard(
            key, timeout=math.inf, level=0
        ):
            yield

    def assign_role_labels(self, game: Game) -> None:
        self._player_manager.assign_role_labels(game)

    async def _register_player_identity(
        self,
        user: User,
        *,
        private_chat_id: Optional[int] = None,
        display_name: Optional[str] = None,
    ) -> None:
        await self._player_identity_manager.register_player_identity(
            user,
            private_chat_id=private_chat_id,
            display_name=display_name,
        )

    def _build_private_menu(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                ["🎁 بونوس روزانه", "📊 آمار بازی"],
                ["⚙️ تنظیمات", "🃏 شروع بازی"],
                ["🤝 بازی با ناشناس"],
            ],
            resize_keyboard=True,
        )

    @staticmethod
    def _coerce_optional_int(value: Optional[str]) -> Optional[int]:
        if value in (None, "", b""):
            return None
        if isinstance(value, bytes):
            value = value.decode()
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode_hash(data: Dict[bytes, bytes]) -> Dict[str, str]:
        decoded: Dict[str, str] = {}
        for key, value in data.items():
            if isinstance(key, bytes):
                key = key.decode()
            if isinstance(value, bytes):
                value = value.decode()
            decoded[str(key)] = str(value)
        return decoded

    def _build_identity_from_player(self, player: Player) -> PlayerIdentity:
        return self._player_identity_manager.build_identity_from_player(player)

    async def _send_statistics_report(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        return await self._player_identity_manager.send_statistics_report(update, context)

    async def _send_wallet_balance(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        return await self._player_identity_manager.send_wallet_balance(update, context)

    async def _cancel_private_matchmaking(self, user_id: UserId) -> bool:
        state = await self._private_match_service.get_private_match_state(user_id)
        if state.get("status") != "queued":
            return False
        user_key = self._private_match_service.private_user_key(user_id)
        extra = {"user_id": self._safe_int(user_id)}
        removed = await self._redis_ops.safe_zrem(
            self._private_match_service.queue_key,
            str(self._safe_int(user_id)),
            log_extra=extra,
        )
        await self._redis_ops.safe_delete(user_key, log_extra=extra)
        return bool(removed)

    async def handle_private_matchmaking_request(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat.type != chat.PRIVATE:
            await self._view.send_message(
                chat.id,
                "ℹ️ برای بازی ناشناس، ابتدا در گفت‌وگوی خصوصی ربات این گزینه را انتخاب کنید.",
            )
            return

        await self._register_player_identity(user, private_chat_id=chat.id)
        await self._private_match_service.cleanup_private_queue()

        state = await self._private_match_service.get_private_match_state(user.id)
        status = state.get("status") if state else None

        if status == "queued":
            await self._cancel_private_matchmaking(user.id)
            await self._view.send_message(
                chat.id,
                "❌ شما از صف بازی خصوصی خارج شدید.",
                reply_markup=self._build_private_menu(),
            )
            return

        if status in {"matched", "playing"}:
            opponent_name_raw = state.get("opponent_name") or state.get("opponent")
            opponent_name = (
                escape_markdown_v1(opponent_name_raw)
                if opponent_name_raw
                else "حریف"
            )
            match_id = state.get("match_id") or "نامشخص"
            await self._view.send_message(
                chat.id,
                f"🎮 شما در حال حاضر در بازی با {opponent_name} هستید. (شناسه: {match_id})",
                reply_markup=self._build_private_menu(),
            )
            return

        result = await self._private_match_service.enqueue_private_player(user, chat.id)
        result_status = result.get("status")
        if result_status == "queued":
            await self._view.send_message(
                chat.id,
                "⌛ شما به صف بازی خصوصی اضافه شدید. برای لغو، دوباره همین دکمه را بزنید.",
                reply_markup=self._build_private_menu(),
            )
            return

        if result_status == "busy":
            match_id = result.get("match_id") or "نامشخص"
            await self._view.send_message(
                chat.id,
                f"⏳ بازی قبلی شما هنوز به پایان نرسیده است. (شناسه: {match_id})",
                reply_markup=self._build_private_menu(),
            )
            return

        if result_status == "matched":
            players = result.get("players")
            if isinstance(players, list) and len(players) == 2:
                await self._private_match_service.start_private_headsup_game(
                    players
                )
            return

        await self._view.send_message(
            chat.id,
            "⚠️ در حال حاضر امکان ثبت در صف وجود ندارد. لطفاً دوباره تلاش کنید.",
            reply_markup=self._build_private_menu(),
        )

    async def report_private_match_result(
        self, match_id: str, winner_user_id: UserId
    ) -> None:
        match_key = self._private_match_service.private_match_key(match_id)
        match_extra = {"match_id": match_id}
        match_data_raw = await self._redis_ops.safe_hgetall(
            match_key, log_extra=match_extra
        )
        if not match_data_raw:
            raise UserException("بازی خصوصی مورد نظر یافت نشد.")
        match_data = self._decode_hash(match_data_raw)
        chat_id = match_data.get("chat_id")
        if not chat_id:
            raise UserException("اطلاعات مربوط به بازی خصوصی ناقص است.")
        game = await self._table_manager.get_game(chat_id)
        winner_id = self._safe_int(winner_user_id)
        results: List[PlayerHandResult] = []
        for player in game.players:
            is_winner = self._safe_int(player.user_id) == winner_id
            display_name = getattr(player, "display_name", None) or player.mention_markdown
            results.append(
                PlayerHandResult(
                    user_id=self._safe_int(player.user_id),
                    display_name=display_name,
                    total_bet=0,
                    payout=1 if is_winner else 0,
                    net_profit=1 if is_winner else -1,
                    hand_type=None,
                    was_all_in=False,
                    result="win" if is_winner else "loss",
                )
            )

        pot_total = sum(result.payout for result in results)

        if self._stats_enabled():
            await self._stats.finish_hand(match_id, chat_id, results, pot_total)
            self._player_report_cache.invalidate_on_event(
                (self._safe_int(player.user_id) for player in game.players),
                event_type="hand_finished",
            )

        game.state = GameState.FINISHED
        await self._table_manager.save_game(chat_id, game)

        player_one_id = self._safe_int(match_data.get("player_one"))
        player_two_id = self._safe_int(match_data.get("player_two"))
        player_one_name = match_data.get("player_one_name") or str(player_one_id)
        player_two_name = match_data.get("player_two_name") or str(player_two_id)
        player_one_chat = self._coerce_optional_int(match_data.get("player_one_chat"))
        player_two_chat = self._coerce_optional_int(match_data.get("player_two_chat"))

        winner_name_raw = (
            player_one_name if winner_id == player_one_id else player_two_name
        )
        loser_name_raw = (
            player_two_name if winner_id == player_one_id else player_one_name
        )
        winner_name = escape_markdown_v1(winner_name_raw)
        loser_name = escape_markdown_v1(loser_name_raw)

        message_winner = (
            "🏆 تبریک! شما برنده بازی خصوصی شدید.\n"
            f"🎯 حریف: {loser_name}"
        )
        message_loser = (
            "🤝 بازی خصوصی به پایان رسید.\n"
            f"🏆 برنده: {winner_name}"
        )

        if player_one_chat:
            await self._view.send_message(
                player_one_chat,
                message_winner if winner_id == player_one_id else message_loser,
                reply_markup=self._build_private_menu(),
            )
        if player_two_chat:
            await self._view.send_message(
                player_two_chat,
                message_winner if winner_id == player_two_id else message_loser,
                reply_markup=self._build_private_menu(),
            )

        await self._redis_ops.safe_delete(match_key, log_extra=match_extra)
        await self._redis_ops.safe_delete(
            self._private_match_service.private_user_key(player_one_id),
            log_extra={"user_id": player_one_id, "match_id": match_id},
        )
        await self._redis_ops.safe_delete(
            self._private_match_service.private_user_key(player_two_id),
            log_extra={"user_id": player_two_id, "match_id": match_id},
        )

    async def _get_game(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> Tuple[Game, ChatId]:
        """Fetch the Game instance for the current chat, caching it in ``chat_data``.

        If the game has already been stored in ``context.chat_data`` it will be
        reused. Otherwise it is loaded from ``TableManager`` and cached for
        subsequent calls.
        """
        chat_id = update.effective_chat.id
        game = context.chat_data.get(KEY_CHAT_DATA_GAME)
        if not game:
            game = await self._table_manager.get_game(chat_id)
            context.chat_data[KEY_CHAT_DATA_GAME] = game
        game.chat_id = chat_id
        return game, chat_id

    async def _get_game_by_user(self, user_id: int) -> Tuple[Game, ChatId]:
        """Find the game and chat id for a given user."""
        try:
            game, chat_id = await self._table_manager.find_game_by_user(user_id)
            game.chat_id = chat_id
            return game, chat_id
        except LookupError as exc:
            await self._view.send_message(
                user_id,
                "❌ هیچ بازی فعالی برای شما پیدا نشد. اگر بازی تازه راه‌اندازی شده،"
                " دوباره تلاش کنید.",
            )
            raise UserException("بازی‌ای برای توقف یافت نشد.") from exc

    @staticmethod
    def _current_turn_player(game: Game) -> Optional[Player]:
        if game.current_player_index < 0:
            return None
        # Use seat-based lookup
        return game.get_player_by_seat(game.current_player_index)

    def _get_first_player_index(self, game: Game) -> int:
        """Return index of the first active player after the dealer."""
        return self._round_rate._find_next_active_player_index(game, game.dealer_index)

    async def _send_join_prompt(self, game: Game, chat_id: ChatId) -> None:
        await self._player_manager.send_join_prompt(game, chat_id)

    async def send_new_ready_prompt(self, game: Game, chat_id: ChatId) -> None:
        """Public helper to request a refreshed ready prompt."""

        await self._player_manager.send_join_prompt(game, chat_id)

    async def _countdown_cache_should_skip(
        self,
        chat_id: ChatId,
        countdown: Optional[int],
        text: str,
        message_id: Optional[MessageId],
    ) -> bool:
        key = self._safe_int(chat_id)
        async with self._countdown_cache_lock:
            entry = self._countdown_cache.get(key)
        if not entry:
            return False
        if entry.text != text or entry.countdown != countdown:
            return False
        if message_id is not None and entry.message_id != message_id:
            return False
        logger.debug(
            "Countdown cache hit; skipping edit",
            extra={"chat_id": chat_id, "message_id": message_id},
        )
        return True

    async def _update_countdown_cache(
        self,
        chat_id: ChatId,
        countdown: Optional[int],
        text: str,
        message_id: Optional[MessageId],
    ) -> None:
        key = self._safe_int(chat_id)
        entry = _CountdownCacheEntry(
            message_id=message_id,
            countdown=countdown,
            text=text,
            updated_at=now_utc(),
        )
        async with self._countdown_cache_lock:
            self._countdown_cache[key] = entry
            logger.debug(
                "Countdown cache size %s",
                self._countdown_cache.currsize,
                extra={"chat_id": chat_id, "cache_max": self._countdown_cache.maxsize},
            )

    def _countdown_context_key(
        self, chat_id: ChatId, game_id: Optional[object]
    ) -> Tuple[int, str]:
        return self._safe_int(chat_id), str(game_id if game_id is not None else 0)

    def _get_countdown_context(
        self, context: CallbackContext, chat_id: ChatId, game: Game
    ) -> Dict[str, Any]:
        store = context.chat_data.setdefault(KEY_START_COUNTDOWN_CONTEXT, {})
        key = self._countdown_context_key(chat_id, getattr(game, "id", None))
        state = store.get(key)
        if state is None:
            state = {}
            store[key] = state
        return state

    def _prune_ready_seats(self, game: Game) -> List[Player]:
        """Remove players from ``game`` whose ready state is stale and return the active list."""

        if game.state != GameState.INITIAL:
            return list(game.players)

        ready_message_id = getattr(game, "ready_message_main_id", None)
        ready_users: Set[int] = set(getattr(game, "ready_users", set()))

        if not ready_message_id or not ready_users:
            if ready_users:
                game.ready_users.clear()
            for player in list(game.players):
                game.remove_player_by_user(player.user_id)
            return []

        valid_players: List[Player] = []
        valid_ids: Set[int] = set()

        for player in list(game.players):
            if getattr(player, "ready_message_id", None) != ready_message_id:
                game.remove_player_by_user(player.user_id)
                continue
            if player.user_id not in ready_users:
                game.remove_player_by_user(player.user_id)
                continue
            valid_players.append(player)
            valid_ids.add(player.user_id)

        if valid_ids != ready_users:
            game.ready_users.intersection_update(valid_ids)

        return valid_players

    def _clear_countdown_context(
        self, context: CallbackContext, chat_id: ChatId, game_id: Optional[object]
    ) -> None:
        store = context.chat_data.get(KEY_START_COUNTDOWN_CONTEXT)
        if not isinstance(store, dict):
            return
        key = self._countdown_context_key(chat_id, game_id)
        store.pop(key, None)
        if not store:
            context.chat_data.pop(KEY_START_COUNTDOWN_CONTEXT, None)

    async def _handle_countdown_expiry(
        self, context: CallbackContext, chat_id: ChatId, game_id: int | str
    ) -> None:
        async with self._chat_guard(
            chat_id, event_stage_label="countdown_expiry"
        ):
            game = await self._table_manager.get_game(chat_id)
            if str(getattr(game, "id", None)) != str(game_id):
                return
            if game.state != GameState.INITIAL:
                self._clear_countdown_context(context, chat_id, game_id)
                return
            logger.info(
                "[Countdown] Expired for chat %s game %s", chat_id, game_id
            )
            await self._start_game(context, game, chat_id, require_guard=False)
            await self._table_manager.save_game(chat_id, game)

    def _build_ready_message(
        self,
        game: Game,
        countdown: Optional[int],
        *,
        anchor_time: Optional[datetime.datetime] = None,
        total_seconds: Optional[int | float] = None,
        ready_players: Optional[List[Player]] = None,
    ) -> Tuple[str, InlineKeyboardMarkup]:
        resolved_ready_players = ready_players or [
            player
            for player in game.players
            if player and player.user_id in getattr(game, "ready_users", set())
        ]
        ready_user_ids = {player.user_id for player in resolved_ready_players}

        ready_items = [
            f"{idx+1}. (صندلی {idx+1}) {player.mention_markdown} 🟢"
            for idx, player in enumerate(game.seats)
            if player and player.user_id in ready_user_ids
        ]
        ready_list = "\n".join(ready_items) if ready_items else "هنوز بازیکنی آماده نیست."

        lines: List[str] = ["👥 *لیست بازیکنان آماده*", "", ready_list, ""]
        lines.append(f"📊 {len(ready_user_ids)}/{MAX_PLAYERS} بازیکن آماده")
        lines.append("")

        if countdown is None:
            lines.append("🚀 برای شروع بازی /start را بزنید یا منتظر بمانید.")
        elif countdown <= 0:
            lines.append("🚀 بازی در حال شروع است...")
        else:
            lines.append(f"⏳ بازی در {countdown} ثانیه شروع می‌شود.")
            anchor = anchor_time or now_utc()
            if anchor.tzinfo is None or anchor.tzinfo.utcoffset(anchor) is None:
                anchor = anchor.replace(tzinfo=datetime.timezone.utc)
            seconds_total = (
                int(total_seconds)
                if isinstance(total_seconds, (int, float)) and total_seconds > 0
                else max(int(countdown), 0)
            )
            target_time = anchor + datetime.timedelta(seconds=seconds_total)
            localized = format_local(
                target_time, self._timezone_name, fmt="%H:%M:%S"
            )
            lines.append(f"🕒 زمان تقریبی شروع: {localized}")
            lines.append("🚀 برای شروع سریع‌تر بازی /start را بزنید یا صبر کنید.")

        text = "\n".join(lines)

        keyboard_buttons: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game")]
        ]

        if countdown is None:
            if game.seated_count() >= self._min_players:
                keyboard_buttons[0].append(
                    InlineKeyboardButton(text="شروع بازی", callback_data="start_game")
                )
        else:
            keyboard_buttons[0].append(
                InlineKeyboardButton(text="شروع بازی", callback_data="start_game")
            )

        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        return text, keyboard

    @staticmethod
    def _ready_prompt_is_current(game: Game) -> bool:
        """Return ``True`` if the cached ready prompt belongs to ``game``."""

        message_id = getattr(game, "ready_message_main_id", None)
        if not message_id:
            return False

        current_game_id = getattr(game, "id", None)
        stored_game_id = getattr(game, "ready_message_game_id", None)
        current_stage = getattr(game, "state", None)
        stored_stage = getattr(game, "ready_message_stage", None)

        if not current_game_id or not stored_game_id:
            return False
        if stored_game_id != current_game_id:
            return False
        if stored_stage != current_stage:
            return False

        waiting_without_preflop = (
            current_stage == GameState.INITIAL
            and not getattr(game, "cards_table", None)
            and stored_stage not in (GameState.INITIAL,)
        )
        if waiting_without_preflop:
            return False

        return True

    async def _auto_start_tick(self, context: CallbackContext) -> None:
        job = context.job
        chat_id = job.chat_id
        start_game_after_guard = False
        game_for_start: Optional[Game] = None
        early_exit = False

        async with self._chat_guard(
            chat_id, event_stage_label="auto_start_tick"
        ):
            game = await self._table_manager.get_game(chat_id)
            context.chat_data[KEY_CHAT_DATA_GAME] = game
            current_state_token = self._state_token(game.state)
            job_data = getattr(job, "data", {})
            scheduled_state = None
            if isinstance(job_data, dict):
                scheduled_state = job_data.get("game_state")
            if scheduled_state is not None and scheduled_state != current_state_token:
                if isinstance(job_data, dict):
                    job_data["game_state"] = current_state_token
                logger.debug(
                    "Skipping auto-start tick due to game state change",
                    extra={
                        "chat_id": chat_id,
                        "scheduled_state": scheduled_state,
                        "current_state": current_state_token,
                    },
                )
                return

            countdown_ctx = self._get_countdown_context(context, chat_id, game)
            countdown_ctx.setdefault("active", False)
            game_identifier = getattr(game, "id", None)
            remaining = countdown_ctx.get("seconds")
            if remaining is None:
                job.schedule_removal()
                context.chat_data.pop("start_countdown_job", None)
                await self._view._cancel_prestart_countdown(chat_id, game_identifier)
                self._clear_countdown_context(context, chat_id, game_identifier)
                return

            if remaining <= 0 or game.state != GameState.INITIAL:
                job.schedule_removal()
                context.chat_data.pop("start_countdown_job", None)
                await self._view._cancel_prestart_countdown(chat_id, game_identifier)
                self._clear_countdown_context(context, chat_id, game_identifier)
                if remaining <= 0 and game.state == GameState.INITIAL:
                    start_game_after_guard = True
                    game_for_start = game
                early_exit = True
            else:
                countdown_value = max(int(remaining), 0)
                now = now_utc()
                anchor_time = countdown_ctx.get(KEY_START_COUNTDOWN_ANCHOR)
                if not isinstance(anchor_time, datetime.datetime):
                    anchor_time = now
                    countdown_ctx[KEY_START_COUNTDOWN_ANCHOR] = anchor_time
                total_seconds_value = countdown_ctx.get(
                    KEY_START_COUNTDOWN_INITIAL_SECONDS
                )
                if (
                    not isinstance(total_seconds_value, (int, float))
                    or total_seconds_value <= 0
                ):
                    total_seconds_value = countdown_value
                    countdown_ctx[KEY_START_COUNTDOWN_INITIAL_SECONDS] = (
                        total_seconds_value
                    )
                ready_players = self._prune_ready_seats(game)
                text, keyboard = self._build_ready_message(
                    game,
                    countdown_value,
                    anchor_time=anchor_time,
                    total_seconds=total_seconds_value,
                    ready_players=ready_players,
                )
                previous_text = countdown_ctx.get(KEY_START_COUNTDOWN_LAST_TEXT)
                countdown_ctx[KEY_START_COUNTDOWN_LAST_TEXT] = text
                countdown_ctx[KEY_START_COUNTDOWN_LAST_TIMESTAMP] = now

                message_id = game.ready_message_main_id
                current_text = getattr(game, "ready_message_main_text", "")
                if message_id is not None and not self._ready_prompt_is_current(game):
                    message_id = None
                    current_text = ""
                    game.ready_message_main_id = None
                    game.ready_message_main_text = ""
                    game.ready_message_game_id = None
                    game.ready_message_stage = None
                if message_id is None:
                    new_message_id = await self._view.send_message_return_id(
                        chat_id,
                        text,
                        reply_markup=keyboard,
                        request_category=RequestCategory.COUNTDOWN,
                    )
                    if new_message_id:
                        game.ready_message_main_id = new_message_id
                        game.ready_message_game_id = getattr(game, "id", None)
                        game.ready_message_stage = game.state
                        await self._table_manager.save_game(chat_id, game)
                        message_id = new_message_id
                    else:
                        await self._view._cancel_prestart_countdown(
                            chat_id, game_identifier
                        )
                        countdown_ctx["seconds"] = countdown_value
                        early_exit = True
                elif text and text != current_text and text != previous_text:
                    await self._telegram_ops.edit_message_text(
                        chat_id,
                        message_id,
                        text,
                        reply_markup=keyboard,
                        request_category=RequestCategory.COUNTDOWN,
                        current_game_id=getattr(game, "id", None),
                    )

                if not early_exit:
                    anchor_message_id = game.ready_message_main_id
                    last_seconds = countdown_ctx.get("last_seconds")
                    should_restart = False
                    if isinstance(last_seconds, (int, float)):
                        should_restart = (
                            bool(countdown_ctx.get("active"))
                            and int(countdown_value) > int(last_seconds)
                        )

                    if anchor_message_id and (
                        not countdown_ctx.get("active") or should_restart
                    ):
                        countdown_ctx["active"] = True
                        countdown_ctx["last_seconds"] = countdown_value

                        def _countdown_payload(
                            seconds: int,
                        ) -> Tuple[str, InlineKeyboardMarkup]:
                            anchor = countdown_ctx.get(KEY_START_COUNTDOWN_ANCHOR)
                            if not isinstance(anchor, datetime.datetime):
                                anchor = now_utc()
                                countdown_ctx[KEY_START_COUNTDOWN_ANCHOR] = anchor
                            payload_total_seconds = countdown_ctx.get(
                                KEY_START_COUNTDOWN_INITIAL_SECONDS
                            )
                            if (
                                not isinstance(payload_total_seconds, (int, float))
                                or payload_total_seconds <= 0
                            ):
                                payload_total_seconds = seconds
                                countdown_ctx[
                                    KEY_START_COUNTDOWN_INITIAL_SECONDS
                                ] = payload_total_seconds
                            current_ready_players = self._prune_ready_seats(game)
                            preview_text, preview_markup = self._build_ready_message(
                                game,
                                seconds,
                                anchor_time=anchor,
                                total_seconds=payload_total_seconds,
                                ready_players=current_ready_players,
                            )
                            countdown_ctx[
                                KEY_START_COUNTDOWN_LAST_TEXT
                            ] = preview_text
                            countdown_ctx[
                                KEY_START_COUNTDOWN_LAST_TIMESTAMP
                            ] = now_utc()
                            game.ready_message_main_text = preview_text
                            return preview_text, preview_markup

                        await self._view.start_prestart_countdown(
                            chat_id=chat_id,
                            game_id=str(game_identifier) if game_identifier is not None else None,
                            anchor_message_id=anchor_message_id,
                            seconds=countdown_value,
                            payload_fn=_countdown_payload,
                        )
                    elif anchor_message_id:
                        countdown_ctx["last_seconds"] = countdown_value
                    else:
                        countdown_ctx.pop("active", None)
                        countdown_ctx.pop("last_seconds", None)

                    game.ready_message_main_text = text or current_text
                    countdown_ctx["seconds"] = max(countdown_value - 1, 0)

        if start_game_after_guard and game_for_start is not None:
            await self._start_game(
                context, game_for_start, chat_id, require_guard=False
            )
            await self._table_manager.save_game(chat_id, game_for_start)
            context.chat_data[KEY_CHAT_DATA_GAME] = game_for_start

        if early_exit:
            return

    async def _schedule_auto_start(
        self, context: CallbackContext, game: Game, chat_id: ChatId
    ) -> None:
        if context.chat_data.get("start_countdown_job"):
            return

        if context.job_queue is None:
            logger.warning("JobQueue not available; auto start disabled")
            return

        countdown_ctx = self._get_countdown_context(context, chat_id, game)
        countdown_ctx["seconds"] = 60
        countdown_ctx[KEY_START_COUNTDOWN_INITIAL_SECONDS] = 60
        countdown_ctx[KEY_START_COUNTDOWN_ANCHOR] = now_utc()
        countdown_ctx[KEY_START_COUNTDOWN_LAST_TEXT] = game.ready_message_main_text
        countdown_ctx.pop(KEY_START_COUNTDOWN_LAST_TIMESTAMP, None)
        countdown_ctx.pop("last_seconds", None)
        countdown_ctx["active"] = False
        self._prune_ready_seats(game)
        job = context.job_queue.run_repeating(
            self._auto_start_tick,
            interval=1,
            chat_id=chat_id,
            data={
                "game_state": self._state_token(game.state),
                "scheduled_at": utc_isoformat(),
            },
        )
        context.chat_data["start_countdown_job"] = job

    async def _cancel_auto_start(
        self,
        context: CallbackContext,
        chat_id: Optional[ChatId] = None,
        game: Optional[Game] = None,
    ) -> None:
        job = context.chat_data.pop("start_countdown_job", None)
        if job:
            job.schedule_removal()
            if chat_id is None:
                chat_id = getattr(job, "chat_id", None)
        game_identifier = getattr(game, "id", None) if game is not None else None
        if chat_id is not None:
            await self._view._cancel_prestart_countdown(chat_id, game_identifier)
            if game_identifier is not None:
                self._clear_countdown_context(context, chat_id, game_identifier)
            else:
                context.chat_data.pop(KEY_START_COUNTDOWN_CONTEXT, None)
        else:
            context.chat_data.pop(KEY_START_COUNTDOWN_CONTEXT, None)

    async def hide_cards(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """در نسخه جدید پیامی در چت خصوصی ارسال نمی‌کند."""
        chat_id = update.effective_chat.id
        if update.message:
            try:
                await update.message.delete()
            except Exception as e:
                logger.warning(
                    "Failed to delete hide message %s in chat %s: %s",
                    update.message.message_id,
                    chat_id,
                    e,
                )

    def _describe_player_role(self, game: Game, player: Player) -> str:
        seat_index = player.seat_index if player.seat_index is not None else -1
        roles: List[str] = []
        role_labels = PlayerManager.ROLE_TRANSLATIONS
        if seat_index == game.dealer_index:
            roles.append(role_labels.get("dealer", "Dealer"))
        if seat_index == game.small_blind_index:
            roles.append(role_labels.get("small_blind", "Small blind"))
        if seat_index == game.big_blind_index:
            roles.append(role_labels.get("big_blind", "Big blind"))
        if not roles:
            roles.append(role_labels.get("player", "Player"))
        return "، ".join(dict.fromkeys(roles))

    async def _safe_edit_message_text(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        parse_mode: str = ParseMode.MARKDOWN,
        log_context: Optional[str] = None,
        request_category: RequestCategory = RequestCategory.GENERAL,
        current_game_id: Optional[str] = None,
    ) -> Optional[MessageId]:
        return await self._telegram_ops.edit_message_text(
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            log_context=log_context,
            request_category=request_category,
            current_game_id=current_game_id,
        )

    async def show_table(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """کارت‌های روی میز را به درخواست بازیکن با فرمت جدید نمایش می‌دهد."""
        game, chat_id = await self._get_game(update, context)

        # پیام درخواست بازیکن حذف نمی‌شود
        logger.debug(
            "Skipping deletion of message %s in chat %s",
            update.message.message_id,
            chat_id,
        )

        if game.state in self._game_engine.ACTIVE_GAME_STATES and game.cards_table:
            # از متد اصلاح‌شده برای نمایش میز استفاده می‌کنیم
            # با count=0 و یک عنوان عمومی و زیبا
            await self.add_cards_to_table(0, game, chat_id, "🃏 کارت‌های روی میز")
            await self._table_manager.save_game(chat_id, game)
        else:
            msg_id = await self._view.send_message_return_id(
                chat_id, "هنوز بازی شروع نشده یا کارتی روی میز نیست."
            )
            if msg_id:
                logger.debug(
                    "Skipping deletion of message %s in chat %s",
                    msg_id,
                    chat_id,
                )

    async def join_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازیکن با دکمهٔ نشستن سر میز به بازی افزوده می‌شود."""
        game, chat_id = await self._get_game(update, context)
        user = update.effective_user
        if update.callback_query:
            await update.callback_query.answer()

        await self._send_join_prompt(game, chat_id)

        await self._register_player_identity(user)

        ready_players = self._prune_ready_seats(game)

        if game.state != GameState.INITIAL:
            await self._view.send_message(chat_id, "⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return

        if len(ready_players) >= MAX_PLAYERS:
            await self._view.send_message(chat_id, "🚪 اتاق پر است!")
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if await wallet.value() < SMALL_BLIND * 2:
            await self._view.send_message(
                chat_id,
                f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).",
            )
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=format_mention_markdown(
                    user.id, user.full_name, version=1
                ),
                wallet=wallet,
                ready_message_id=game.ready_message_main_id,
                seat_index=None,
            )
            player.display_name = user.full_name or user.first_name or user.username
            player.username = user.username
            player.full_name = user.full_name
            player.private_chat_id = self._player_identity_manager.private_chat_ids.get(
                self._safe_int(user.id)
            )
            game.ready_users.add(user.id)
            seat_assigned = game.add_player(player)
            if seat_assigned == -1:
                await self._view.send_message(chat_id, "🚪 اتاق پر است!")
                return

        ready_players = self._prune_ready_seats(game)

        if len(ready_players) >= self._min_players:
            await self._schedule_auto_start(context, game, chat_id)
        else:
            await self._cancel_auto_start(context, chat_id, game)

        countdown_ctx = self._get_countdown_context(context, chat_id, game)
        countdown_value = countdown_ctx.get("seconds")
        now = now_utc()
        anchor = countdown_ctx.get(KEY_START_COUNTDOWN_ANCHOR)
        if isinstance(countdown_value, (int, float)) and countdown_value >= 0:
            if not isinstance(anchor, datetime.datetime):
                anchor = now
                countdown_ctx[KEY_START_COUNTDOWN_ANCHOR] = anchor
        else:
            anchor = None
        total_seconds_value = countdown_ctx.get(KEY_START_COUNTDOWN_INITIAL_SECONDS)
        if not isinstance(total_seconds_value, (int, float)) or total_seconds_value <= 0:
            total_seconds_value = countdown_value
            if isinstance(total_seconds_value, (int, float)) and total_seconds_value > 0:
                countdown_ctx[KEY_START_COUNTDOWN_INITIAL_SECONDS] = (
                    total_seconds_value
                )
        text, keyboard = self._build_ready_message(
            game,
            countdown_value,
            anchor_time=anchor or now,
            total_seconds=total_seconds_value,
            ready_players=ready_players,
        )
        countdown_ctx[KEY_START_COUNTDOWN_LAST_TEXT] = text
        countdown_ctx[KEY_START_COUNTDOWN_LAST_TIMESTAMP] = now
        current_text = getattr(game, "ready_message_main_text", "")

        message_id = game.ready_message_main_id
        if message_id and not self._ready_prompt_is_current(game):
            if message_id and message_id in game.message_ids_to_delete:
                game.message_ids_to_delete.remove(message_id)
            message_id = None
            current_text = ""
            game.ready_message_main_id = None
            game.ready_message_main_text = ""
            game.ready_message_game_id = None
            game.ready_message_stage = None

        if message_id:
            if text != current_text:
                new_id = await self._telegram_ops.edit_message_text(
                    chat_id,
                    message_id,
                    text,
                    reply_markup=keyboard,
                    request_category=RequestCategory.COUNTDOWN,
                    current_game_id=getattr(game, "id", None),
                )
                if new_id is None:
                    if message_id and message_id in game.message_ids_to_delete:
                        game.message_ids_to_delete.remove(message_id)
                    game.ready_message_main_id = None
                    game.ready_message_game_id = None
                    game.ready_message_stage = None
                    msg = await self._view.send_message_return_id(
                        chat_id,
                        text,
                        reply_markup=keyboard,
                        request_category=RequestCategory.COUNTDOWN,
                    )
                    if msg:
                        game.ready_message_main_id = msg
                        game.ready_message_main_text = text
                        game.ready_message_game_id = getattr(game, "id", None)
                        game.ready_message_stage = game.state
                elif new_id:
                    game.ready_message_main_id = new_id
                    game.ready_message_main_text = text
                    game.ready_message_game_id = getattr(game, "id", None)
                    game.ready_message_stage = game.state
            else:
                game.ready_message_main_text = current_text
        else:
            msg = await self._view.send_message_return_id(
                chat_id,
                text,
                reply_markup=keyboard,
                request_category=RequestCategory.COUNTDOWN,
            )
            if msg:
                game.ready_message_main_id = msg
                game.ready_message_main_text = text
                game.ready_message_game_id = getattr(game, "id", None)
                game.ready_message_stage = game.state

        await self._table_manager.save_game(chat_id, game)

    async def ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        game, chat_id = await self._get_game(update, context)

        current_game_id = getattr(game, "id", None)
        stored_game_id = getattr(game, "ready_message_game_id", None)

        if not stored_game_id or stored_game_id != current_game_id:
            for player in getattr(game, "players", []):
                setattr(player, "ready_message_id", None)

            if getattr(game, "ready_users", None):
                game.ready_users.clear()
            remover = getattr(game, "remove_player_by_user", None)
            if callable(remover):
                for player in list(getattr(game, "players", [])):
                    user_id = getattr(player, "user_id", None)
                    if user_id is not None:
                        remover(user_id)

            game.ready_message_main_id = None
            game.ready_message_game_id = None
            game.ready_message_stage = None
            game.ready_message_main_text = ""

            if self._table_manager is not None:
                await self._table_manager.save_game(chat_id, game)

            self._logger.info(
                "Sent new ready prompt due to stale message",
                extra={"chat_id": chat_id, "game_id": current_game_id},
            )

            await self.send_new_ready_prompt(game, chat_id)
            return

        await self.join_game(update, context)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازی را به صورت دستی شروع می‌کند."""
        chat = update.effective_chat
        user = update.effective_user
        if chat.type == chat.PRIVATE:
            await self._register_player_identity(
                user,
                private_chat_id=chat.id,
            )
            welcome_text = (
                f"{_DICE_ROLL_EMOJI} خوش آمدید به بازی پوکر ما!\n"
                "لطفاً یکی از گزینه‌ها را از منوی زیر انتخاب کنید تا ادامه دهیم."
            )
            await self._view.send_message(
                chat.id,
                welcome_text,
                reply_markup=self._build_private_menu(),
            )
            return

        await self._register_player_identity(user)

        game, chat_id = await self._get_game(update, context)
        await self._cancel_auto_start(context, chat_id, game)
        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            await self._view.send_message(
                chat_id, "🎮 یک بازی در حال حاضر در جریان است."
            )
            return

        if game.state == GameState.FINISHED:
            await self._request_metrics.end_cycle(
                self._safe_int(chat_id), cycle_token=game.id
            )
            game.reset()
            # بازیکنان قبلی را برای دور جدید نگه دار
            old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
            # Re-add players logic would go here if needed.
            # For now, just resetting allows new players to join.

        ready_players = self._prune_ready_seats(game)

        if len(ready_players) >= self._min_players:
            await self._start_game(context, game, chat_id)
        else:
            await self._view.send_message(
                chat_id,
                f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).",
            )
        await self._table_manager.save_game(chat_id, game)

    async def stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """درخواست توقف بازی را ثبت می‌کند و رأی‌گیری را آغاز می‌کند."""
        user_id = update.effective_user.id

        try:
            game, chat_id = await self._get_game(update, context)
        except Exception:
            game, chat_id = await self._get_game_by_user(user_id)
            context.chat_data[KEY_CHAT_DATA_GAME] = game

        await self._game_engine.stop_game(
            context=context,
            game=game,
            chat_id=chat_id,
            requester_id=user_id,
        )

    async def confirm_stop_vote(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle a confirmation vote for stopping the current hand."""

        game, chat_id = await self._get_game(update, context)
        voter_id = update.callback_query.from_user.id
        await self._game_engine.confirm_stop_vote(
            context=context,
            game=game,
            chat_id=chat_id,
            voter_id=voter_id,
        )

    async def resume_stop_vote(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Cancel the stop request and keep the current game running."""

        game, chat_id = await self._get_game(update, context)
        await self._game_engine.resume_stop_vote(
            context=context,
            game=game,
            chat_id=chat_id,
        )

    async def _start_game(
        self,
        context: CallbackContext,
        game: Game,
        chat_id: ChatId,
        *,
        require_guard: bool = True,
    ) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""

        async def _run_start() -> None:
            await self._cancel_auto_start(context, chat_id, game)
            await self._game_engine.start_game(context, game, chat_id)

        if require_guard:
            async with self._chat_guard(
                chat_id, event_stage_label="start_game", game=game
            ):
                await _run_start()
        else:
            await _run_start()

    def _is_betting_round_over(self, game: Game) -> bool:
        """
        بررسی می‌کند که آیا دور شرط‌بندی فعلی به پایان رسیده است یا خیر.
        یک دور زمانی تمام می‌شود که:
        1. تمام بازیکنانی که فولد نکرده‌اند، حداقل یک بار حرکت کرده باشند.
        2. تمام بازیکنانی که فولد نکرده‌اند، مقدار یکسانی پول در این دور گذاشته باشند.
        """
        active_players = game.players_by(states=(PlayerState.ACTIVE,))

        # اگر هیچ بازیکن فعالی وجود ندارد (مثلاً همه all-in یا فولد کرده‌اند)، دور تمام است.
        if not active_players:
            return True

        # شرط اول: آیا همه بازیکنان فعال حرکت کرده‌اند؟
        # فلگ `has_acted` باید در ابتدای هر street و بعد از هر raise ریست شود.
        if not all(p.has_acted for p in active_players):
            return False

        # شرط دوم: آیا همه بازیکنان فعال مقدار یکسانی شرط بسته‌اند؟
        # مقدار شرط اولین بازیکن فعال را به عنوان مرجع در نظر می‌گیریم.
        reference_rate = active_players[0].round_rate
        if not all(p.round_rate == reference_rate for p in active_players):
            return False

        # اگر هر دو شرط برقرار باشد، دور تمام شده است.
        return True

    async def _process_playing(
        self, chat_id: ChatId, game: Game, context: CallbackContext
    ) -> Optional[Player]:
        """
        مغز متفکر و کنترل‌کننده اصلی جریان بازی.
        این متد پس از هر حرکت بازیکن فراخوانی می‌شود تا تصمیم بگیرد:
        1. آیا دست تمام شده؟ (یک نفر باقی مانده)
        2. آیا دور شرط‌بندی تمام شده؟
        3. در غیر این صورت، نوبت را به بازیکن فعال بعدی بده.
        این متد جایگزین چرخه بازگشتی قبلی بین _process_playing و _move_to_next_player_and_process شده است.
        """
        if game.turn_message_id:
            logger.debug(
                "Keeping turn message %s in chat %s",
                game.turn_message_id,
                chat_id,
            )

        # شرط ۱: آیا فقط یک بازیکن (یا کمتر) در بازی باقی مانده؟
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            should_continue = await self._game_engine.progress_stage(
                context=context,
                chat_id=chat_id,
                game=game,
            )
            if should_continue:
                return await self._process_playing(chat_id, game, context)
            return None

        # شرط ۲: آیا دور شرط‌بندی فعلی به پایان رسیده است؟
        if self._is_betting_round_over(game):
            should_continue = await self._game_engine.progress_stage(
                context=context,
                chat_id=chat_id,
                game=game,
            )
            if should_continue:
                return await self._process_playing(chat_id, game, context)
            return None

        # شرط ۳: بازی ادامه دارد، نوبت را به بازیکن بعدی منتقل کن
        next_player_index = self._round_rate._find_next_active_player_index(
            game, game.current_player_index
        )

        if next_player_index != -1:
            game.current_player_index = next_player_index
            return game.players[next_player_index]

        # اگر هیچ بازیکن فعالی برای حرکت بعدی وجود ندارد (مثلاً همه All-in هستند)
        should_continue = await self._game_engine.progress_stage(
            context=context,
            chat_id=chat_id,
            game=game,
        )
        if should_continue:
            return await self._process_playing(chat_id, game, context)
        return None

    async def _send_turn_message(
        self,
        game: Game,
        player: Player,
        chat_id: ChatId,
    ):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف در آینده ذخیره می‌کند."""
        lock_key = f"{STAGE_LOCK_PREFIX}{self._safe_int(chat_id)}"
        money: Optional[Money] = None
        recent_actions: List[str] = []
        previous_message_id: Optional[MessageId] = None

        async with self._chat_guard(
            chat_id, event_stage_label="send_turn_message", game=game
        ):
            try:
                async with self._lock_manager.guard(lock_key, timeout=10):
                    game.chat_id = chat_id
                    await self._view.update_player_anchors_and_keyboards(game)

                    wallet = getattr(player, "wallet", None)
                    money = None
                    if wallet is not None:
                        try:
                            money = await wallet.value()
                        except Exception:
                            logger.exception(
                                "Failed to fetch wallet value", extra={"chat_id": chat_id, "player_id": getattr(player, "user_id", None)}
                            )
                    if money is None:
                        logger.debug(
                            "Defaulting missing wallet value to zero",
                            extra={
                                "chat_id": chat_id,
                                "player_id": getattr(player, "user_id", None),
                                "wallet_present": wallet is not None,
                            },
                        )
                        money = 0
                    recent_actions = list(game.last_actions)
                    previous_message_id = game.turn_message_id
            except TimeoutError:
                self._game_engine._log_engine_event_lock_failure(
                    lock_key=lock_key,
                    event_stage_label="send_turn_message",
                    chat_id=chat_id,
                    game=game,
                )
                raise

            turn_update = await self._view.update_turn_message(
                chat_id=chat_id,
                game=game,
                player=player,
                money=money,
                message_id=previous_message_id,
                recent_actions=recent_actions,
            )

            now_value = now_utc()
            try:
                async with self._lock_manager.guard(lock_key, timeout=10):
                    if (
                        turn_update.message_id
                        and game.turn_message_id == previous_message_id
                    ):
                        game.turn_message_id = turn_update.message_id
                    elif (
                        turn_update.message_id
                        and game.turn_message_id != previous_message_id
                    ):
                        logger.debug(
                            "Skipping turn message id update due to concurrent change",
                            extra={
                                "chat_id": chat_id,
                                "previous_turn_message_id": previous_message_id,
                                "current_turn_message_id": game.turn_message_id,
                                "new_turn_message_id": turn_update.message_id,
                            },
                        )

                    game.last_turn_time = now_value

                    logger.debug(
                        "Turn message refreshed",
                        extra={
                            "chat_id": chat_id,
                            "turn_message_id": game.turn_message_id,
                        },
                    )
            except TimeoutError:
                self._game_engine._log_engine_event_lock_failure(
                    lock_key=lock_key,
                    event_stage_label="send_turn_message",
                    chat_id=chat_id,
                    game=game,
                )
                raise

    # --- Player Action Handlers ---
    # این بخش تمام حرکات ممکن بازیکنان در نوبتشان را مدیریت می‌کند.

    async def player_action_fold(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """بازیکن فولد می‌کند، از دور شرط‌بندی کنار می‌رود و نوبت به نفر بعدی منتقل می‌شود."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        current_player.state = PlayerState.FOLD
        action_str = f"{current_player.mention_markdown}: فولد"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 5:
            game.last_actions.pop(0)

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    async def player_action_call_check(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """بازیکن کال (پرداخت) یا چک (عبور) را انجام می‌دهد."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        call_amount = game.max_round_rate - current_player.round_rate
        current_player.has_acted = True

        try:
            if call_amount > 0:
                await current_player.wallet.authorize(game.id, call_amount)
                current_player.round_rate += call_amount
                current_player.total_bet += call_amount
                game.pot += call_amount
            # منطق Check بدون نیاز به عمل خاص
        except UserException as e:
            await self._view.send_message(
                chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}"
            )
            return  # اگر پول نداشت، از ادامه متد جلوگیری کن

        action_type = "کال" if call_amount > 0 else "چک"
        amount = call_amount if call_amount > 0 else 0
        action_str = f"{current_player.mention_markdown}: {action_type}"
        if amount > 0:
            action_str += f" {amount}$"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 5:
            game.last_actions.pop(0)

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    async def player_action_raise_bet(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, raise_amount: int
    ) -> None:
        """بازیکن شرط را افزایش می‌دهد (Raise) یا برای اولین بار شرط می‌بندد (Bet)."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        call_amount = game.max_round_rate - current_player.round_rate
        total_amount_to_bet = call_amount + raise_amount

        try:
            await current_player.wallet.authorize(game.id, total_amount_to_bet)
            current_player.round_rate += total_amount_to_bet
            current_player.total_bet += total_amount_to_bet
            game.pot += total_amount_to_bet

            game.max_round_rate = current_player.round_rate
            action_text = "بِت" if call_amount == 0 else "رِیز"

            # --- بخش کلیدی منطق پوکر ---
            game.trading_end_user_id = current_player.user_id
            current_player.has_acted = True
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False

        except UserException as e:
            await self._view.send_message(
                chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}"
            )
            return

        action_str = f"{current_player.mention_markdown}: {action_text} {total_amount_to_bet}$"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 5:
            game.last_actions.pop(0)

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    async def player_action_all_in(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """بازیکن تمام موجودی خود را شرط می‌بندد (All-in)."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        all_in_amount = await current_player.wallet.value()

        if all_in_amount <= 0:
            await self._view.send_message(
                chat_id,
                f"👀 {current_player.mention_markdown} موجودی برای آل-این ندارد و چک می‌کند.",
            )
            await self.player_action_call_check(
                update, context
            )  # این حرکت معادل چک است
            return

        await current_player.wallet.authorize(game.id, all_in_amount)
        current_player.round_rate += all_in_amount
        current_player.total_bet += all_in_amount
        game.pot += all_in_amount
        current_player.state = PlayerState.ALL_IN
        current_player.has_acted = True

        action_str = f"{current_player.mention_markdown}: آل-این {all_in_amount}$"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 5:
            game.last_actions.pop(0)

        if current_player.round_rate > game.max_round_rate:
            game.max_round_rate = current_player.round_rate
            game.trading_end_user_id = current_player.user_id
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    # ---- Table management commands ---------------------------------

    async def create_game(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat_id = update.effective_chat.id
        await self._table_manager.create_game(chat_id)
        game = await self._table_manager.get_game(chat_id)
        await self._send_join_prompt(game, chat_id)
        await self._view.send_message(chat_id, "بازی جدید ایجاد شد.")

    async def add_cards_to_table(
        self,
        count: int,
        game: Game,
        chat_id: ChatId,
        street_name: str,
        send_message: bool = True,
    ) -> None:
        async with self._chat_guard(
            chat_id, event_stage_label="add_cards_to_table", game=game
        ):
            await self._game_engine.add_cards_to_table(
                count=count,
                game=game,
                chat_id=chat_id,
                street_name=street_name,
                send_message=send_message,
            )

    async def bonus(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user

        if chat.type != chat.PRIVATE:
            await self._view.send_message(
                chat.id,
                "ℹ️ برای دریافت بونوس روزانه، لطفاً در چت خصوصی با ربات گفتگو کنید.",
            )
            return

        await self._register_player_identity(user, private_chat_id=chat.id)

        wallet = WalletManagerModel(user.id, self._kv)
        amount = random.choice(BONUSES)
        try:
            new_balance = await wallet.add_daily(amount)
        except UserException as exc:
            await self._view.send_message(
                chat.id,
                f"⚠️ {exc}",
                reply_markup=self._build_private_menu(),
            )
            return

        await self._view.send_message(
            chat.id,
            (
                f"🎁 تبریک! {amount}$ بونوس تازه به موجودی شما افزوده شد.\n"
                f"💼 موجودی فعلی: {new_balance}$"
            ),
            reply_markup=self._build_private_menu(),
        )

        if self._stats_enabled():
            user_id_int = self._safe_int(user.id)
            await self._stats.record_daily_bonus(user_id_int, amount)
            self._player_report_cache.invalidate_on_event(
                [user_id_int], event_type="bonus_claimed"
            )

    async def _clear_game_messages(
        self, game: Game, chat_id: ChatId, *, collect_only: bool = False
    ) -> Optional[Set[MessageId]]:
        """Deletes all temporary messages related to the current hand."""
        async with self._chat_guard(
            chat_id, event_stage_label="clear_game_messages", game=game
        ):
            logger.debug("Clearing game messages", extra={"chat_id": chat_id})

            ids_to_delete: Set[MessageId] = set(game.message_ids_to_delete)

            if game.board_message_id:
                ids_to_delete.add(game.board_message_id)
                game.board_message_id = None

            if game.turn_message_id:
                ids_to_delete.add(game.turn_message_id)
                game.turn_message_id = None

            game.chat_id = chat_id

            for player in game.seated_players():
                anchor_message_id: Optional[MessageId] = None
                if player.anchor_message and player.anchor_message[0] == chat_id:
                    anchor_message_id = player.anchor_message[1]
                if anchor_message_id:
                    ids_to_delete.discard(anchor_message_id)

            game.message_ids_to_delete.clear()
            game.message_ids.clear()

        if collect_only:
            return ids_to_delete

        for message_id in ids_to_delete:
            try:
                await self._view.delete_message(chat_id, message_id)
            except Exception as e:
                logger.debug(
                    "Failed to delete message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(e).__name__,
                    },
                )

        return None

    async def _clear_player_anchors(self, game: Game) -> None:
        await self._player_manager.clear_player_anchors(game)

    async def _end_hand(
        self, game: Game, chat_id: ChatId, context: CallbackContext
    ) -> None:
        """
        یک دست از بازی را تمام کرده، پیام‌ها را پاکسازی کرده و برای دست بعدی آماده می‌شود.
        """
        await self._clear_game_messages(game, chat_id)
        await self._clear_player_anchors(game)

        # ۲. ذخیره بازیکنان برای دست بعدی
        # این باعث می‌شود در بازی بعدی، لازم نباشد همه دوباره دکمهٔ نشستن سر میز را بزنند
        old_players: List[UserId] = []
        for p in game.players:
            if await p.wallet.value() > 0:
                old_players.append(p.user_id)
        context.chat_data[KEY_OLD_PLAYERS] = old_players

        # ۳. ریست کردن کامل آبجکت بازی برای شروع یک دست جدید و تمیز
        # یک آبجکت جدید Game می‌سازیم تا هیچ داده‌ای از دست قبل باقی نماند
        new_game = Game()
        context.chat_data[KEY_CHAT_DATA_GAME] = new_game
        await self._table_manager.save_game(chat_id, new_game)
        await self._send_join_prompt(new_game, chat_id)

        # ۴. اعلام پایان دست و راهنمایی برای شروع دست بعدی
        await self._view.send_message(
            chat_id=chat_id,
            text="🎉 دست تمام شد! برای شروع دست بعدی، دکمهٔ «نشستن سر میز» را بزنید یا منتظر بمانید تا کسی /start کند.",
        )

    def _format_cards(self, cards: Cards) -> str:
        """
        کارت‌ها را با فرمت ثابت و زیبای Markdown برمی‌گرداند.
        برای هماهنگی با نسخه قدیمی، بین کارت‌ها دو اسپیس قرار می‌دهیم.
        """
        if not cards:
            return "??  ??"
        return "  ".join(str(card) for card in cards)


class RoundRateModel:
    def __init__(
        self,
        view: PokerBotViewer = None,
        kv: aioredis.Redis = None,
        model: "PokerBotModel" = None,
    ):
        self._view = view
        self._kv = kv
        self._model = model  # optional reference to model

    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        num_players = game.seated_count()
        for i in range(1, num_players + 1):
            next_index = (start_index + i) % num_players
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index
        return -1

    def _get_first_player_index(self, game: Game) -> int:
        return self._find_next_active_player_index(game, game.dealer_index)

    # داخل کلاس RoundRateModel
    async def set_blinds(self, game: Game, chat_id: ChatId) -> Optional[Player]:
        """
        Determine small/big blinds (using seat indices) and debit the players.
        Works for heads-up (2-player) and multiplayer by walking occupied seats.
        """
        num_players = game.seated_count()
        if num_players < 2:
            return

        # find next occupied seats for small and big blinds
        # heads-up special case: dealer is small blind
        if num_players == 2:
            small_blind_index = game.dealer_index
            big_blind_index = game.next_occupied_seat(small_blind_index)
            first_action_index = small_blind_index
        else:
            small_blind_index = game.next_occupied_seat(game.dealer_index)
            big_blind_index = game.next_occupied_seat(small_blind_index)
            first_action_index = game.next_occupied_seat(big_blind_index)

        # record in game
        game.small_blind_index = small_blind_index
        game.big_blind_index = big_blind_index

        small_blind_player = game.get_player_by_seat(small_blind_index)
        big_blind_player = game.get_player_by_seat(big_blind_index)

        if small_blind_player is None or big_blind_player is None:
            return None

        # apply blinds
        await self._set_player_blind(
            game, small_blind_player, SMALL_BLIND, "کوچک", chat_id
        )
        await self._set_player_blind(
            game, big_blind_player, SMALL_BLIND * 2, "بزرگ", chat_id
        )

        game.max_round_rate = SMALL_BLIND * 2
        game.current_player_index = first_action_index
        game.trading_end_user_id = big_blind_player.user_id

        player_turn = game.get_player_by_seat(game.current_player_index)
        return player_turn

    async def _set_player_blind(
        self,
        game: Game,
        player: Player,
        amount: Money,
        blind_type: str,
        chat_id: ChatId,
    ):
        try:
            await player.wallet.authorize(game_id=str(chat_id), amount=amount)
            player.round_rate += amount
            player.total_bet += amount  # ← این خط اضافه شود
            game.pot += amount

            action_str = (
                f"💸 {player.mention_markdown} بلایند {blind_type} به مبلغ {amount}$ را پرداخت کرد."
            )
            game.last_actions.append(action_str)
            if len(game.last_actions) > 5:
                game.last_actions.pop(0)
        except UserException as e:
            available_money = await player.wallet.value()
            await player.wallet.authorize(game_id=str(chat_id), amount=available_money)
            player.round_rate += available_money
            player.total_bet += available_money  # ← این خط هم اضافه شود
            game.pot += available_money
            player.state = PlayerState.ALL_IN
            await self._view.send_message(
                chat_id,
                f"⚠️ {player.mention_markdown} موجودی کافی برای بلایند نداشت و All-in شد ({available_money}$).",
            )

    async def finish_rate(
        self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]
    ) -> None:
        """Split the pot among players based on their hand scores.

        ``player_scores`` maps a score to a list of ``(Player, Cards)`` tuples
        where higher scores represent better hands. Players receive chips
        proportional to their wager and capped by the remaining pot.
        """
        total_players = sum(len(v) for v in player_scores.values())
        remaining_pot = game.pot

        for score in sorted(player_scores.keys(), reverse=True):
            group = player_scores[score]
            caps: List[Money] = []
            for p, _ in group:
                authorized = await p.wallet.authorized_money(game.id)
                caps.append(authorized * total_players)
            group_total = sum(caps)
            if group_total == 0:
                continue
            scale = min(1, remaining_pot / group_total)
            for (player, _), cap in zip(group, caps):
                payout = cap * scale
                await player.wallet.inc(int(round(payout)))
                remaining_pot -= payout
            if remaining_pot <= 0:
                break

        for group in player_scores.values():
            for player, _ in group:
                await player.wallet.approve(game.id)

        game.pot = int(remaining_pot)

    def collect_bets_for_pot(self, game: Game):
        # This function resets the round-specific bets for the next street.
        # The money is already in the pot.
        for player in game.seated_players():
            player.round_rate = 0
        game.max_round_rate = 0


class WalletManagerModel(Wallet):
    """
    این کلاس مسئولیت مدیریت موجودی (Wallet) هر بازیکن را با استفاده از Redis بر عهده دارد.
    این کلاس به صورت اتمی (atomic) کار می‌کند تا از مشکلات همزمانی (race condition) جلوگیری کند.
    """

    def __init__(self, user_id: UserId, kv: aioredis.Redis):
        self._user_id = user_id
        self._kv: aioredis.Redis = kv
        self._val_key = f"u_m:{user_id}"
        self._daily_bonus_key = f"u_db:{user_id}"
        self._authorized_money_key = f"u_am:{user_id}"  # برای پول رزرو شده در بازی

        # اسکریپت Lua برای کاهش اتمی موجودی (جلوگیری از race condition)
        # این اسکریپت ابتدا مقدار فعلی را می‌گیرد، اگر کافی بود کم می‌کند و مقدار جدید را برمیگرداند
        # در غیر این صورت -1 را برمیگرداند.
        self._LUA_DECR_IF_GE = self._kv.register_script(
            """
            local current = tonumber(redis.call('GET', KEYS[1]))
            if current == nil then
                redis.call('SET', KEYS[1], ARGV[2])
                current = tonumber(ARGV[2])
            end
            local amount = tonumber(ARGV[1])
            if current >= amount then
                return redis.call('DECRBY', KEYS[1], amount)
            else
                return -1
            end
        """
        )

    async def value(self) -> Money:
        """موجودی فعلی بازیکن را برمی‌گرداند. اگر بازیکن وجود نداشته باشد، با مقدار پیش‌فرض ایجاد می‌شود."""
        val = await self._kv.get(self._val_key)
        if val is None:
            await self._kv.set(self._val_key, DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(val)

    async def inc(self, amount: Money = 0) -> Money:
        """موجودی بازیکن را به مقدار مشخص شده افزایش می‌دهد."""
        result = await self._kv.incrby(self._val_key, amount)
        return int(result)

    async def dec(self, amount: Money) -> Money:
        """
        موجودی بازیکن را به مقدار مشخص شده کاهش می‌دهد، تنها اگر موجودی کافی باشد.
        این عملیات به صورت اتمی با استفاده از اسکریپت Lua انجام می‌شود.
        """
        if amount < 0:
            raise ValueError("Amount to decrease cannot be negative.")
        if amount == 0:
            return await self.value()

        try:
            result = await self._LUA_DECR_IF_GE(
                keys=[self._val_key], args=[amount, DEFAULT_MONEY]
            )
        except (NoScriptError, ModuleNotFoundError):
            current_raw = await self._kv.get(self._val_key)
            if current_raw is None:
                await self._kv.set(self._val_key, DEFAULT_MONEY)
                current = DEFAULT_MONEY
            else:
                current = int(current_raw)
            if current >= amount:
                await self._kv.decrby(self._val_key, amount)
                result = current - amount
            else:
                result = -1
        if result == -1:
            raise UserException("موجودی شما کافی نیست.")
        return int(result)

    async def has_daily_bonus(self) -> bool:
        """چک می‌کند آیا بازیکن پاداش روزانه خود را دریافت کرده است یا خیر."""
        result = await self._kv.exists(self._daily_bonus_key)
        return bool(result)

    async def add_daily(self, amount: Money) -> Money:
        """پاداش روزانه را به بازیکن می‌دهد و زمان آن را تا روز بعد ثبت می‌ند."""
        if await self.has_daily_bonus():
            raise UserException("شما قبلاً پاداش روزانه خود را دریافت کرده‌اید.")

        now = now_utc()
        tomorrow = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + datetime.timedelta(days=1)
        ttl = int((tomorrow - now).total_seconds())

        await self._kv.setex(self._daily_bonus_key, ttl, "1")
        return await self.inc(amount)

    # --- متدهای مربوط به تراکنش‌های بازی (برای تطابق با Wallet ABC) ---
    async def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        """Increase reserved money for a specific game."""
        await self._kv.hincrby(self._authorized_money_key, game_id, amount)

    async def authorized_money(self, game_id: str) -> Money:
        """Return the amount of money currently reserved for ``game_id``."""
        val = await self._kv.hget(self._authorized_money_key, game_id)
        return int(val) if val else 0

    async def authorize_all(self, game_id: str) -> Money:
        """Reserve the entire wallet for ``game_id`` and return that amount."""
        current = await self.value()
        if current > 0:
            await self.dec(current)
            await self._kv.hincrby(self._authorized_money_key, game_id, current)
        return current

    async def authorize(self, game_id: str, amount: Money) -> None:
        """مبلغی از پول بازیکن را برای یک بازی خاص رزرو (dec) می‌کند."""
        await self.dec(amount)
        await self._kv.hincrby(self._authorized_money_key, game_id, amount)

    async def approve(self, game_id: str) -> None:
        """تراکنش موفق یک بازی را تایید می‌کند (پول خرج شده و نیاز به بازگشت نیست)."""
        await self._kv.hdel(self._authorized_money_key, game_id)

    async def cancel(self, game_id: str) -> None:
        """تراکنش ناموفق را لغو و پول رزرو شده را به بازیکن برمی‌گرداند."""
        amount_to_return_bytes = await self._kv.hget(self._authorized_money_key, game_id)
        if amount_to_return_bytes:
            amount_to_return = int(amount_to_return_bytes)
            if amount_to_return > 0:
                await self.inc(amount_to_return)
                await self._kv.hdel(self._authorized_money_key, game_id)
