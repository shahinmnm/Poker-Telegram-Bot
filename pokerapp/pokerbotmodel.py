#!/usr/bin/env python3

import asyncio
import datetime
import inspect
import random
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import CallbackContext, ContextTypes
from telegram.helpers import mention_markdown as format_mention_markdown

import logging

from pokerapp.config import Config
from pokerapp.winnerdetermination import (
    WinnerDetermination,
    HandsOfPoker,
    HAND_NAMES_TRANSLATIONS,
)
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
from pokerapp.utils.cache import PlayerReportCache
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics
from pokerapp.utils.locks import ReentrantAsyncLock

DICE_MULT = 10
DICE_DELAY_SEC = 5
BONUSES = (5, 20, 40, 80, 160, 320)
DICES = "⚀⚁⚂⚃⚄⚅"

AUTO_START_MAX_UPDATES_PER_MINUTE = 20
AUTO_START_MIN_UPDATE_INTERVAL = datetime.timedelta(
    seconds=60 / AUTO_START_MAX_UPDATES_PER_MINUTE
)
KEY_START_COUNTDOWN_LAST_TEXT = "start_countdown_last_text"
KEY_START_COUNTDOWN_LAST_TIMESTAMP = "start_countdown_last_timestamp"
KEY_START_COUNTDOWN_CONTEXT = "start_countdown_context"

# legacy keys kept for backward compatibility but unused
KEY_OLD_PLAYERS = "old_players"
KEY_CHAT_DATA_GAME = "game"
KEY_STOP_REQUEST = "stop_request"

STOP_CONFIRM_CALLBACK = "stop:confirm"
STOP_RESUME_CALLBACK = "stop:resume"

# MAX_PLAYERS = 8 (Defined in entities)
# MIN_PLAYERS = 2 (Defined in entities)
# SMALL_BLIND = 5 (Defined in entities)
# DEFAULT_MONEY = 1000 (Defined in entities)
MAX_TIME_FOR_TURN = datetime.timedelta(minutes=2)
DESCRIPTION_FILE = "assets/description_bot.md"

PRIVATE_MATCH_QUEUE_KEY = "pokerbot:private_matchmaking:queue"
PRIVATE_MATCH_USER_KEY_PREFIX = "pokerbot:private_matchmaking:user:"
PRIVATE_MATCH_RECORD_KEY_PREFIX = "pokerbot:private_matchmaking:match:"
PRIVATE_MATCH_QUEUE_TTL = 180  # seconds
PRIVATE_MATCH_STATE_TTL = 3600  # seconds

logger = logging.getLogger(__name__)


ROLE_TRANSLATIONS = {
    "dealer": "دیلر",
    "small_blind": "بلایند کوچک",
    "big_blind": "بلایند بزرگ",
    "player": "بازیکن",
}


def assign_role_labels(game: Game) -> None:
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
            roles.append(ROLE_TRANSLATIONS["dealer"])
        if is_small_blind:
            roles.append(ROLE_TRANSLATIONS["small_blind"])
        if is_big_blind:
            roles.append(ROLE_TRANSLATIONS["big_blind"])
        if not roles:
            roles.append(ROLE_TRANSLATIONS["player"])

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

        logger.debug(
            "Assigned role_label: player=%s seat=%s role=%s",
            display_name,
            seat_number,
            role_label,
        )


@dataclass(slots=True)
class PrivateMatchPlayerInfo:
    user_id: int
    chat_id: Optional[int]
    display_name: str
    username: Optional[str] = None


@dataclass(slots=True)
class _CountdownCacheEntry:
    message_id: Optional[MessageId]
    countdown: Optional[int]
    text: str
    updated_at: datetime.datetime


class PokerBotModel:
    ACTIVE_GAME_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }

    @staticmethod
    def _safe_int(value: UserId) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _state_token(state: Any) -> str:
        name = getattr(state, "name", None)
        if isinstance(name, str):
            return name
        value = getattr(state, "value", None)
        if isinstance(value, str):
            return value
        return str(state)

    def _hand_type_to_label(self, hand_type: Optional[HandsOfPoker]) -> Optional[str]:
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

    def __init__(
        self,
        view: PokerBotViewer,
        bot: Bot,
        cfg: Config,
        kv: aioredis.Redis,
        table_manager: TableManager,
        stats_service: Optional[BaseStatsService] = None,
    ):
        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._cfg: Config = cfg
        self._kv = kv
        self._table_manager = table_manager
        self._winner_determine: WinnerDetermination = WinnerDetermination()
        self._round_rate = RoundRateModel(view=self._view, kv=self._kv, model=self)
        self._stats: BaseStatsService = stats_service or NullStatsService()
        self._player_report_cache = PlayerReportCache(
            logger_=logger.getChild("player_report_cache")
        )
        self._private_chat_ids: Dict[int, int] = {}
        self._chat_locks: Dict[int, ReentrantAsyncLock] = {}
        self._countdown_cache: LRUCache[int, _CountdownCacheEntry] = LRUCache(
            maxsize=64, getsizeof=lambda entry: 1
        )
        self._countdown_cache_lock = asyncio.Lock()
        self._stage_batch_locks: Dict[int, asyncio.Lock] = {}
        self._stage_batch_guard = asyncio.Lock()
        metrics_candidate = getattr(self._view, "request_metrics", None)
        if not isinstance(metrics_candidate, RequestMetrics):
            metrics_candidate = RequestMetrics(
                logger_=logger.getChild("request_metrics")
            )
            setattr(self._view, "request_metrics", metrics_candidate)
        self._request_metrics = metrics_candidate

    @property
    def _min_players(self):
        return 1 if self._cfg.DEBUG else MIN_PLAYERS

    def _stats_enabled(self) -> bool:
        return not isinstance(self._stats, NullStatsService)

    def _get_chat_lock(self, chat_id: ChatId) -> ReentrantAsyncLock:
        normalized = self._safe_int(chat_id)
        lock = self._chat_locks.get(normalized)
        if lock is None:
            lock = ReentrantAsyncLock()
            self._chat_locks[normalized] = lock
        return lock

    async def _get_stage_lock(self, chat_id: ChatId) -> asyncio.Lock:
        normalized = self._safe_int(chat_id)
        async with self._stage_batch_guard:
            lock = self._stage_batch_locks.get(normalized)
            if lock is None:
                lock = asyncio.Lock()
                self._stage_batch_locks[normalized] = lock
            return lock

    @asynccontextmanager
    async def _chat_guard(self, chat_id: ChatId):
        """Serialize stateful operations for a chat while allowing nesting."""

        lock = self._get_chat_lock(chat_id)
        async with lock:
            yield

    async def _register_player_identity(
        self,
        user: User,
        *,
        private_chat_id: Optional[int] = None,
        display_name: Optional[str] = None,
    ) -> None:
        player_id = self._safe_int(user.id)
        if private_chat_id:
            self._private_chat_ids[player_id] = private_chat_id

            table_manager = getattr(self, "_table_manager", None)
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
                                    logger.exception(
                                        "Failed to persist game after updating private chat id",
                                        extra={"chat_id": chat_id, "user_id": player_id},
                                    )
                except Exception:
                    logger.exception(
                        "Failed to update player private chat id in active game",
                        extra={"user_id": player_id},
                    )
        if not self._stats_enabled():
            return
        identity = PlayerIdentity(
            user_id=self._safe_int(user.id),
            display_name=display_name or user.full_name or user.first_name or str(user.id),
            username=user.username,
            full_name=user.full_name,
            private_chat_id=private_chat_id,
        )
        await self._stats.register_player_profile(identity)

    def _build_private_menu(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                ["🎁 بونوس روزانه", "📊 آمار بازی"],
                ["⚙️ تنظیمات", "🃏 شروع بازی"],
                ["🤝 بازی با ناشناس"],
            ],
            resize_keyboard=True,
        )

    def _private_user_key(self, user_id: UserId) -> str:
        return f"{PRIVATE_MATCH_USER_KEY_PREFIX}{self._safe_int(user_id)}"

    @staticmethod
    def _private_match_key(match_id: str) -> str:
        return f"{PRIVATE_MATCH_RECORD_KEY_PREFIX}{match_id}"

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

    def _build_player_info_from_state(
        self, user_id: str, state: Dict[str, str]
    ) -> PrivateMatchPlayerInfo:
        display_name = state.get("display_name") or str(user_id)
        username = state.get("username") or None
        chat_id = self._coerce_optional_int(state.get("chat_id"))
        return PrivateMatchPlayerInfo(
            user_id=self._safe_int(user_id),
            chat_id=chat_id,
            display_name=display_name,
            username=username,
        )

    def _build_identity_from_player(self, player: Player) -> PlayerIdentity:
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

    async def _send_statistics_report(
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

        await self._register_player_identity(user, private_chat_id=chat.id)

        if not self._stats_enabled():
            await self._view.send_message(
                chat.id,
                "⚙️ سیستم آمار در حال حاضر غیرفعال است. لطفاً بعداً دوباره تلاش کنید.",
                reply_markup=self._build_private_menu(),
            )
            return

        user_id_int = self._safe_int(user.id)

        async def _load_report() -> Optional[Any]:
            return await self._stats.build_player_report(user_id_int)

        report = await self._player_report_cache.get(user_id_int, _load_report)
        if report is None or (
            report.stats.total_games <= 0 and not report.recent_games
        ):
            await self._view.send_message(
                chat.id,
                "ℹ️ هنوز داده‌ای برای نمایش وجود ندارد. پس از شرکت در چند دست بازی دوباره تلاش کنید.",
                reply_markup=self._build_private_menu(),
            )
            return

        formatted = self._stats.format_report(report)
        await self._view.send_message(
            chat.id,
            formatted,
            reply_markup=self._build_private_menu(),
        )

    async def _send_wallet_balance(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat = update.effective_chat
        user = update.effective_user

        if chat.type == chat.PRIVATE:
            await self._register_player_identity(user, private_chat_id=chat.id)
        else:
            await self._register_player_identity(user)

        wallet = WalletManagerModel(user.id, self._kv)
        balance = await wallet.value()

        reply_markup = self._build_private_menu() if chat.type == chat.PRIVATE else None
        await self._view.send_message(
            chat.id,
            f"💰 موجودی فعلی شما: {balance}$",
            reply_markup=reply_markup,
        )

    async def _get_private_match_state(self, user_id: UserId) -> Dict[str, str]:
        key = self._private_user_key(user_id)
        data = await self._kv.hgetall(key)
        if not data:
            return {}
        return self._decode_hash(data)

    async def _cleanup_private_queue(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff_ts = int(now.timestamp()) - PRIVATE_MATCH_QUEUE_TTL
        expired = await self._kv.zrangebyscore(
            PRIVATE_MATCH_QUEUE_KEY, "-inf", cutoff_ts
        )
        if not expired:
            return
        for raw_user_id in expired:
            if isinstance(raw_user_id, bytes):
                user_id_str = raw_user_id.decode()
            else:
                user_id_str = str(raw_user_id)
            await self._kv.zrem(PRIVATE_MATCH_QUEUE_KEY, raw_user_id)
            state = await self._get_private_match_state(user_id_str)
            key = self._private_user_key(user_id_str)
            await self._kv.delete(key)
            chat_id = self._coerce_optional_int(state.get("chat_id")) if state else None
            if chat_id:
                await self._view.send_message(
                    chat_id,
                    "⏳ زمان انتظار شما به پایان رسید و از صف بازی خصوصی خارج شدید.",
                    reply_markup=self._build_private_menu(),
                )

    async def _try_pop_match(self) -> Optional[List[PrivateMatchPlayerInfo]]:
        popped = await self._kv.zpopmin(PRIVATE_MATCH_QUEUE_KEY, 2)
        if not popped:
            return None
        if len(popped) < 2:
            member, score = popped[0]
            await self._kv.zadd(PRIVATE_MATCH_QUEUE_KEY, {member: score})
            return None
        states: List[Tuple[str, Dict[str, str], float]] = []
        for member, score in popped:
            user_id_str = member.decode() if isinstance(member, bytes) else str(member)
            state = await self._get_private_match_state(user_id_str)
            states.append((user_id_str, state, score))
        valid = [item for item in states if item[1].get("status") == "queued"]
        if len(valid) < 2:
            for user_id_str, state, score in states:
                timestamp = state.get("timestamp") if state else None
                score_value = int(timestamp) if timestamp else score
                await self._kv.zadd(PRIVATE_MATCH_QUEUE_KEY, {user_id_str: score_value})
            return None
        players = [
            self._build_player_info_from_state(user_id_str, state)
            for user_id_str, state, _ in valid[:2]
        ]
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for idx, (user_id_str, state, _) in enumerate(valid[:2]):
            opponent = players[1 - idx]
            await self._kv.hset(
                self._private_user_key(user_id_str),
                mapping={
                    "status": "matched",
                    "opponent": str(opponent.user_id),
                    "matched_at": str(now_ts),
                    "chat_id": state.get("chat_id", ""),
                    "display_name": state.get("display_name", ""),
                    "username": state.get("username", ""),
                },
            )
            await self._kv.expire(
                self._private_user_key(user_id_str), PRIVATE_MATCH_STATE_TTL
            )
        return players

    async def _enqueue_private_player(
        self, user: User, chat_id: int
    ) -> Dict[str, object]:
        existing_state = await self._get_private_match_state(user.id)
        status = existing_state.get("status") if existing_state else None
        if status == "queued":
            return {"status": "queued"}
        if status in {"matched", "playing"}:
            return {
                "status": "busy",
                "match_id": existing_state.get("match_id") if existing_state else None,
                "opponent": existing_state.get("opponent") if existing_state else None,
            }

        timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        display_name = (
            user.full_name
            or user.first_name
            or user.username
            or str(user.id)
        )
        username = user.username or ""
        state_key = self._private_user_key(user.id)
        await self._kv.hset(
            state_key,
            mapping={
                "status": "queued",
                "timestamp": str(timestamp),
                "chat_id": str(chat_id),
                "display_name": display_name,
                "username": username,
            },
        )
        await self._kv.expire(state_key, PRIVATE_MATCH_STATE_TTL)
        await self._kv.zadd(
            PRIVATE_MATCH_QUEUE_KEY,
            {str(self._safe_int(user.id)): timestamp},
        )

        players = await self._try_pop_match()
        if players:
            return {"status": "matched", "players": players}
        return {"status": "queued"}

    async def _cancel_private_matchmaking(self, user_id: UserId) -> bool:
        state = await self._get_private_match_state(user_id)
        if state.get("status") != "queued":
            return False
        user_key = self._private_user_key(user_id)
        removed = await self._kv.zrem(
            PRIVATE_MATCH_QUEUE_KEY, str(self._safe_int(user_id))
        )
        await self._kv.delete(user_key)
        return bool(removed)

    async def _start_private_headsup_game(
        self, players: List[PrivateMatchPlayerInfo]
    ) -> str:
        if len(players) != 2:
            raise ValueError("Private heads-up games require exactly two players")
        match_id = f"pm_{uuid.uuid4().hex}"
        chat_id: ChatId = f"private:{match_id}"
        game = await self._table_manager.create_game(chat_id)
        await self._request_metrics.end_cycle(
            self._safe_int(chat_id), cycle_token=game.id
        )
        await self._clear_player_anchors(game)
        game.reset()
        for index, info in enumerate(players):
            safe_user_id = self._safe_int(info.user_id)
            wallet = WalletManagerModel(safe_user_id, self._kv)
            mention_name = info.display_name or str(safe_user_id)
            mention = format_mention_markdown(safe_user_id, mention_name, version=1)
            player = Player(
                user_id=safe_user_id,
                mention_markdown=mention,
                wallet=wallet,
                ready_message_id="private_match",
                seat_index=index,
            )
            player.display_name = info.display_name or mention_name
            player.username = info.username
            player.full_name = info.display_name
            player.private_chat_id = info.chat_id
            game.add_player(player, seat_index=index)
            game.ready_users.add(safe_user_id)
            if info.chat_id:
                self._private_chat_ids[safe_user_id] = info.chat_id
        await self._table_manager.save_game(chat_id, game)

        started_at = datetime.datetime.now(datetime.timezone.utc)
        match_key = self._private_match_key(match_id)
        await self._kv.hset(
            match_key,
            mapping={
                "status": "active",
                "chat_id": chat_id,
                "player_one": str(self._safe_int(players[0].user_id)),
                "player_two": str(self._safe_int(players[1].user_id)),
                "player_one_name": players[0].display_name,
                "player_two_name": players[1].display_name,
                "player_one_chat": str(players[0].chat_id or ""),
                "player_two_chat": str(players[1].chat_id or ""),
                "started_at": str(started_at.timestamp()),
            },
        )
        await self._kv.expire(match_key, PRIVATE_MATCH_STATE_TTL)

        if self._stats_enabled():
            identities = [self._build_identity_from_player(p) for p in game.players]
            await self._stats.start_hand(
                match_id, chat_id, identities, start_time=started_at
            )

        for idx, info in enumerate(players):
            opponent = players[1 - idx]
            state_key = self._private_user_key(info.user_id)
            await self._kv.hset(
                state_key,
                mapping={
                    "status": "playing",
                    "match_id": match_id,
                    "opponent": str(self._safe_int(opponent.user_id)),
                    "opponent_name": opponent.display_name,
                    "chat_id": str(info.chat_id or ""),
                    "display_name": info.display_name,
                    "username": info.username or "",
                },
            )
            await self._kv.expire(state_key, PRIVATE_MATCH_STATE_TTL)
            if info.chat_id:
                opponent_name_raw = (
                    opponent.display_name
                    or str(self._safe_int(opponent.user_id))
                )
                opponent_name = escape_markdown_v1(opponent_name_raw)
                message = (
                    "🤝 حریف شما پیدا شد!\n"
                    f"🎮 بازی خصوصی با {opponent_name} تا لحظاتی دیگر آغاز می‌شود.\n"
                    f"🆔 شناسه بازی: {match_id}"
                )
                await self._view.send_message(
                    info.chat_id,
                    message,
                    reply_markup=self._build_private_menu(),
                )

        return match_id

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
        await self._cleanup_private_queue()

        state = await self._get_private_match_state(user.id)
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

        result = await self._enqueue_private_player(user, chat.id)
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
                await self._start_private_headsup_game(players)  # type: ignore[arg-type]
            return

        await self._view.send_message(
            chat.id,
            "⚠️ در حال حاضر امکان ثبت در صف وجود ندارد. لطفاً دوباره تلاش کنید.",
            reply_markup=self._build_private_menu(),
        )

    async def report_private_match_result(
        self, match_id: str, winner_user_id: UserId
    ) -> None:
        match_key = self._private_match_key(match_id)
        match_data_raw = await self._kv.hgetall(match_key)
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
            self._player_report_cache.invalidate_many(
                self._safe_int(player.user_id) for player in game.players
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

        await self._kv.delete(match_key)
        await self._kv.delete(self._private_user_key(player_one_id))
        await self._kv.delete(self._private_user_key(player_two_id))

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
        """Send initial join prompt with inline button if not already sent."""
        if game.state == GameState.INITIAL and not game.ready_message_main_id:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game")]]
            )
            msg_id = await self._view.send_message_return_id(
                chat_id, "برای نشستن سر میز دکمه را بزن", reply_markup=markup
            )
            if msg_id:
                game.ready_message_main_id = msg_id
                game.ready_message_main_text = "برای نشستن سر میز دکمه را بزن"
                await self._table_manager.save_game(chat_id, game)

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
            updated_at=datetime.datetime.now(datetime.timezone.utc),
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
        async with self._chat_guard(chat_id):
            game = await self._table_manager.get_game(chat_id)
            if str(getattr(game, "id", None)) != str(game_id):
                return
            if game.state != GameState.INITIAL:
                self._clear_countdown_context(context, chat_id, game_id)
                return
            logger.info(
                "[Countdown] Expired for chat %s game %s", chat_id, game_id
            )
            await self._start_game(context, game, chat_id)
            await self._table_manager.save_game(chat_id, game)

    def _build_ready_message(
        self, game: Game, countdown: Optional[int]
    ) -> Tuple[str, InlineKeyboardMarkup]:
        ready_items = [
            f"{idx+1}. (صندلی {idx+1}) {p.mention_markdown} 🟢"
            for idx, p in enumerate(game.seats)
            if p
        ]
        ready_list = "\n".join(ready_items) if ready_items else "هنوز بازیکنی آماده نیست."

        lines: List[str] = ["👥 *لیست بازیکنان آماده*", "", ready_list, ""]
        lines.append(f"📊 {game.seated_count()}/{MAX_PLAYERS} بازیکن آماده")
        lines.append("")

        if countdown is None:
            lines.append("🚀 برای شروع بازی /start را بزنید یا منتظر بمانید.")
        elif countdown <= 0:
            lines.append("🚀 بازی در حال شروع است...")
        else:
            lines.append(f"⏳ بازی تا {countdown} ثانیه دیگر شروع می‌شود.")
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
            start_label = "شروع بازی (اکنون)" if countdown <= 0 else f"شروع بازی ({countdown})"
            keyboard_buttons[0].append(
                InlineKeyboardButton(text=start_label, callback_data="start_game")
            )

        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        return text, keyboard

    async def _auto_start_tick(self, context: CallbackContext) -> None:
        job = context.job
        chat_id = job.chat_id
        async with self._chat_guard(chat_id):
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
                    await self._start_game(context, game, chat_id)
                    await self._table_manager.save_game(chat_id, game)
                return

            countdown_value = max(int(remaining), 0)
            now = datetime.datetime.now(datetime.timezone.utc)
            text, keyboard = self._build_ready_message(game, countdown_value)
            countdown_ctx[KEY_START_COUNTDOWN_LAST_TEXT] = text
            countdown_ctx[KEY_START_COUNTDOWN_LAST_TIMESTAMP] = now
            game.ready_message_main_text = text

            message_id = game.ready_message_main_id
            if message_id is None:
                new_message_id = await self._view.send_message_return_id(
                    chat_id,
                    text,
                    reply_markup=keyboard,
                    request_category=RequestCategory.COUNTDOWN,
                )
                if new_message_id:
                    game.ready_message_main_id = new_message_id
                    await self._table_manager.save_game(chat_id, game)
                    message_id = new_message_id
                else:
                    await self._view._cancel_prestart_countdown(chat_id, game_identifier)
                    countdown_ctx["seconds"] = countdown_value
                    countdown_ctx["active"] = False
                    return

            previous_seconds = countdown_ctx.get("last_seconds")
            countdown_active = bool(countdown_ctx.get("active"))

            def payload_fn(seconds_left: int) -> Tuple[str, InlineKeyboardMarkup]:
                payload_text, payload_keyboard = self._build_ready_message(
                    game, max(seconds_left, 0)
                )
                game.ready_message_main_text = payload_text
                countdown_ctx[KEY_START_COUNTDOWN_LAST_TEXT] = payload_text
                countdown_ctx[KEY_START_COUNTDOWN_LAST_TIMESTAMP] = (
                    datetime.datetime.now(datetime.timezone.utc)
                )
                return payload_text, payload_keyboard

            should_start_countdown = False
            if message_id is not None:
                if not countdown_active:
                    should_start_countdown = True
                elif previous_seconds is None:
                    should_start_countdown = True
                elif countdown_value > int(previous_seconds):
                    should_start_countdown = True

            if should_start_countdown:
                async def _on_countdown_complete() -> None:
                    await self._handle_countdown_expiry(
                        context, chat_id, game_identifier
                    )

                await self._view.start_prestart_countdown(
                    chat_id=chat_id,
                    game_id=game_identifier,
                    anchor_message_id=message_id,
                    seconds=countdown_value,
                    payload_fn=payload_fn,
                    on_complete=_on_countdown_complete,
                )
                countdown_active = True

            countdown_ctx["active"] = countdown_active
            countdown_ctx["last_seconds"] = countdown_value
            countdown_ctx["seconds"] = max(countdown_value - 1, 0)

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
        countdown_ctx[KEY_START_COUNTDOWN_LAST_TEXT] = game.ready_message_main_text
        countdown_ctx.pop(KEY_START_COUNTDOWN_LAST_TIMESTAMP, None)
        countdown_ctx.pop("last_seconds", None)
        countdown_ctx["active"] = False
        job = context.job_queue.run_repeating(
            self._auto_start_tick,
            interval=1,
            chat_id=chat_id,
            data={
                "game_state": self._state_token(game.state),
                "scheduled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
        if seat_index == game.dealer_index:
            roles.append("دیلر")
        if seat_index == game.small_blind_index:
            roles.append("بلایند کوچک")
        if seat_index == game.big_blind_index:
            roles.append("بلایند بزرگ")
        if not roles:
            roles.append("بازیکن")
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
    ) -> Optional[MessageId]:
        """
        Safely edit a message's text, retrying on rate limits and
        sending a new message if the original cannot be edited.

        The method handles ``BadRequest`` and ``RetryAfter`` errors by
        retrying or falling back to sending a fresh message. The ID of
        the edited or newly sent message is returned.
        """

        if not message_id:
            return await self._view.send_message_return_id(
                chat_id, text, reply_markup=reply_markup
            )

        try:
            result = await self._view.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=parse_mode,
            )
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            result = await self._view.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=parse_mode,
            )
        except BadRequest as exc:
            error_message = getattr(exc, "message", None) or str(exc)
            preview = text
            max_preview_length = 120
            if len(preview) > max_preview_length:
                preview = preview[: max_preview_length - 3] + "..."
            logger.warning(
                "BadRequest when editing message; will send a replacement",
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "context": log_context or "general",
                    "error_message": error_message,
                    "text_preview": preview,
                },
            )
            result = None
        except TelegramError as exc:
            logger.error(
                "TelegramError when editing message; will send a replacement",
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "context": log_context or "general",
                    "error_type": type(exc).__name__,
                },
            )
            result = None
        else:
            if result:
                return result

        new_id = await self._view.send_message_return_id(
            chat_id,
            text,
            reply_markup=reply_markup,
            request_category=request_category,
        )
        if new_id and message_id and new_id != message_id:
            try:
                await self._view.delete_message(chat_id, message_id)
            except Exception as e:
                logger.debug(
                    "Failed to delete message after replacement",
                    extra={
                        "chat_id": chat_id,
                        "old_message_id": message_id,
                        "error_type": type(e).__name__,
                    },
                )
            logger.info(
                "Sent replacement message after edit failure",
                extra={
                    "chat_id": chat_id,
                    "old_message_id": message_id,
                    "new_message_id": new_id,
                },
            )
        return new_id

    async def show_table(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """کارت‌های روی میز را به درخواست بازیکن با فرمت جدید نمایش می‌دهد."""
        game, chat_id = await self._get_game(update, context)

        # پیام درخواست بازیکن حذف نمی‌شود
        logger.debug(
            "Skipping deletion of message %s in chat %s",
            update.message.message_id,
            chat_id,
        )

        if game.state in self.ACTIVE_GAME_STATES and game.cards_table:
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

        if game.state != GameState.INITIAL:
            await self._view.send_message(chat_id, "⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return

        if game.seated_count() >= MAX_PLAYERS:
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
            player.private_chat_id = self._private_chat_ids.get(self._safe_int(user.id))
            game.ready_users.add(user.id)
            seat_assigned = game.add_player(player)
            if seat_assigned == -1:
                await self._view.send_message(chat_id, "🚪 اتاق پر است!")
                return

        if game.seated_count() >= self._min_players:
            await self._schedule_auto_start(context, game, chat_id)
        else:
            await self._cancel_auto_start(context, chat_id, game)

        countdown_ctx = self._get_countdown_context(context, chat_id, game)
        countdown_value = countdown_ctx.get("seconds")
        text, keyboard = self._build_ready_message(game, countdown_value)
        countdown_ctx[KEY_START_COUNTDOWN_LAST_TEXT] = text
        countdown_ctx[KEY_START_COUNTDOWN_LAST_TIMESTAMP] = (
            datetime.datetime.now(datetime.timezone.utc)
        )
        current_text = getattr(game, "ready_message_main_text", "")

        if game.ready_message_main_id:
            if text != current_text:
                new_id = await self._safe_edit_message_text(
                    chat_id,
                    game.ready_message_main_id,
                    text,
                    reply_markup=keyboard,
                    request_category=RequestCategory.COUNTDOWN,
                )
                if new_id is None:
                    old_id = game.ready_message_main_id
                    if old_id and old_id in game.message_ids_to_delete:
                        game.message_ids_to_delete.remove(old_id)
                    game.ready_message_main_id = None
                    msg = await self._view.send_message_return_id(
                        chat_id,
                        text,
                        reply_markup=keyboard,
                        request_category=RequestCategory.COUNTDOWN,
                    )
                    if msg:
                        game.ready_message_main_id = msg
                        game.ready_message_main_text = text
                elif new_id:
                    game.ready_message_main_id = new_id
                    game.ready_message_main_text = text
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

        await self._table_manager.save_game(chat_id, game)

    async def ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                "🎲 خوش آمدید به بازی پوکر ما!\n"
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

        if game.seated_count() >= self._min_players:
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

        if game.state == GameState.INITIAL:
            raise UserException("بازی فعالی برای توقف وجود ندارد.")

        if not any(player.user_id == user_id for player in game.seated_players()):
            raise UserException("فقط بازیکنان حاضر می‌توانند درخواست توقف بدهند.")

        await self._request_stop(context, game, chat_id, user_id)
        await self._table_manager.save_game(chat_id, game)

    async def _request_stop(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        game: Game,
        chat_id: ChatId,
        requester_id: UserId,
    ) -> None:
        """Create or update a stop request vote and announce it to the chat."""

        active_players = [
            p
            for p in game.seated_players()
            if p.state in (PlayerState.ACTIVE, PlayerState.ALL_IN)
        ]
        if not active_players:
            raise UserException("هیچ بازیکن فعالی برای رأی‌گیری وجود ندارد.")

        stop_request = context.chat_data.get(KEY_STOP_REQUEST)
        if not stop_request or stop_request.get("game_id") != game.id:
            stop_request = {
                "game_id": game.id,
                "active_players": [p.user_id for p in active_players],
                "votes": set(),
                "initiator": requester_id,
                "message_id": None,
                "manager_override": False,
            }
        else:
            stop_request.setdefault("votes", set())
            stop_request.setdefault("active_players", [])
            stop_request.setdefault("manager_override", False)
            stop_request["active_players"] = [p.user_id for p in active_players]

        votes = set(stop_request.get("votes", set()))
        if requester_id in stop_request["active_players"]:
            votes.add(requester_id)
        stop_request["votes"] = votes

        message_text = self._render_stop_request_message(
            game=game,
            stop_request=stop_request,
            context=context,
        )

        message_id = await self._safe_edit_message_text(
            chat_id,
            stop_request.get("message_id"),
            message_text,
            reply_markup=self._build_stop_request_markup(),
            request_category=RequestCategory.GENERAL,
        )
        stop_request["message_id"] = message_id
        context.chat_data[KEY_STOP_REQUEST] = stop_request

    def _build_stop_request_markup(self) -> InlineKeyboardMarkup:
        """Return the inline keyboard used for stop confirmations."""

        keyboard = [
            [
                InlineKeyboardButton(
                    text="تأیید توقف", callback_data=STOP_CONFIRM_CALLBACK
                ),
                InlineKeyboardButton(
                    text="ادامه بازی", callback_data=STOP_RESUME_CALLBACK
                ),
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    def _render_stop_request_message(
        self,
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
            (p for p in game.seated_players() if p.user_id == initiator_id),
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
                (p for p in game.seated_players() if p.user_id == manager_id),
                None,
            )

        required_votes = (len(active_players) // 2) + 1 if active_players else 0
        confirmed_votes = len(votes & {p.user_id for p in active_players})

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

        if votes - {p.user_id for p in active_players}:
            extra_voters = votes - {p.user_id for p in active_players}
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
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle a confirmation vote for stopping the current hand."""

        game, chat_id = await self._get_game(update, context)
        stop_request = context.chat_data.get(KEY_STOP_REQUEST)
        if not stop_request or stop_request.get("game_id") != game.id:
            raise UserException("درخواست توقف فعالی وجود ندارد.")

        user_id = update.callback_query.from_user.id
        manager_id = context.chat_data.get("game_manager_id")

        active_ids = set(stop_request.get("active_players", []))
        votes = set(stop_request.get("votes", set()))

        if user_id not in active_ids and user_id != manager_id:
            raise UserException("تنها بازیکنان فعال یا مدیر می‌توانند رأی دهند.")

        votes.add(user_id)
        stop_request["votes"] = votes
        stop_request["manager_override"] = bool(manager_id and user_id == manager_id)

        message_text = self._render_stop_request_message(
            game=game,
            stop_request=stop_request,
            context=context,
        )

        message_id = await self._safe_edit_message_text(
            chat_id,
            stop_request.get("message_id"),
            message_text,
            reply_markup=self._build_stop_request_markup(),
            request_category=RequestCategory.GENERAL,
        )
        stop_request["message_id"] = message_id
        context.chat_data[KEY_STOP_REQUEST] = stop_request

        active_votes = len(votes & active_ids)
        required_votes = (len(active_ids) // 2) + 1 if active_ids else 0

        if stop_request.get("manager_override"):
            await self._cancel_hand(game, chat_id, context, stop_request)
            return

        if active_ids and active_votes >= required_votes:
            await self._cancel_hand(game, chat_id, context, stop_request)

    async def resume_stop_vote(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Cancel the stop request and keep the current game running."""

        game, chat_id = await self._get_game(update, context)
        stop_request = context.chat_data.get(KEY_STOP_REQUEST)
        if not stop_request or stop_request.get("game_id") != game.id:
            raise UserException("درخواست توقفی برای لغو وجود ندارد.")

        message_id = stop_request.get("message_id")
        context.chat_data.pop(KEY_STOP_REQUEST, None)

        resume_text = "✅ رأی به ادامه‌ی بازی داده شد. بازی ادامه می‌یابد."
        await self._safe_edit_message_text(
            chat_id,
            message_id,
            resume_text,
            reply_markup=None,
            request_category=RequestCategory.GENERAL,
        )

    async def _cancel_hand(
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

        context.chat_data.pop(KEY_STOP_REQUEST, None)

        await self._request_metrics.end_cycle(
            self._safe_int(chat_id), cycle_token=game.id
        )
        await self._clear_player_anchors(game)
        game.reset()
        await self._table_manager.save_game(chat_id, game)
        await self._view.send_message(chat_id, "🛑 بازی متوقف شد.")

    async def _start_game(
        self, context: CallbackContext, game: Game, chat_id: ChatId
    ) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""
        async with self._chat_guard(chat_id):
            await self._cancel_auto_start(context, chat_id, game)
            logger.info(
                "[Game] start_hand invoked",
                extra={
                    "chat_id": chat_id,
                    "game_id": getattr(game, "id", None),
                },
            )
            if game.ready_message_main_id:
                deleted_ready_message = False
                try:
                    await self._view.delete_message(chat_id, game.ready_message_main_id)
                    deleted_ready_message = True
                except Exception as e:
                    logger.warning(
                        "Failed to delete ready message",
                        extra={
                            "chat_id": chat_id,
                            "message_id": game.ready_message_main_id,
                            "error_type": type(e).__name__,
                        },
                    )
                if deleted_ready_message:
                    game.ready_message_main_id = None
                game.ready_message_main_text = ""

            # Ensure dealer_index is initialized before use
            if not hasattr(game, "dealer_index"):
                game.dealer_index = -1

            new_dealer_index = game.advance_dealer()
            if new_dealer_index == -1:
                new_dealer_index = game.next_occupied_seat(-1)
                game.dealer_index = new_dealer_index

            if game.dealer_index == -1:
                logger.warning("Cannot start game without an occupied dealer seat")
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
                except Exception as exc:
                    logger.debug(
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

            # این متد به تنهایی تمام کارهای لازم برای شروع راند را انجام می‌دهد.
            # از جمله تعیین بلایندها، تعیین نوبت اول و ارسال پیام نوبت.
            current_player = await self._round_rate.set_blinds(game, chat_id)
            assign_role_labels(game)

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

            # نیازی به هیچ کد دیگری در اینجا نیست.
            # کدهای اضافی حذف شدند.

            # ذخیره بازیکنان برای دست بعدی (این خط می‌تواند بماند)
            context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]

    async def _divide_cards(self, game: Game, chat_id: ChatId):
        """کارت‌ها را فقط در گروه همراه با کیبورد انتخابی توزیع می‌کند."""
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

    def _determine_winners(
        self, game: Game, contenders: List[Player]
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """
        مغز متفکر مالی ربات! (نسخه ۲.۰ - خود اصلاحگر)
        برندگان را با در نظر گرفتن Side Pot مشخص کرده و با استفاده از game.pot
        از صحت محاسبات اطمینان حاصل می‌کند.
        """
        if not contenders or game.pot == 0:
            return [], []

        # ۱. محاسبه قدرت دست هر بازیکن (بدون تغییر)
        contender_details = []
        for player in contenders:
            hand_type, score, best_hand_cards = self._winner_determine.get_hand_value(
                player.cards, game.cards_table
            )
            contender_details.append(
                {
                    "player": player,
                    "total_bet": player.total_bet,
                    "score": score,
                    "hand_cards": best_hand_cards,
                    "hand_type": hand_type,
                }
            )

        # ۲. شناسایی لایه‌های شرط‌بندی (Tiers) (بدون تغییر)
        bet_tiers = sorted(
            list(set(p["total_bet"] for p in contender_details if p["total_bet"] > 0))
        )

        winners_by_pot = []
        last_bet_tier = 0
        calculated_pot_total = 0  # برای پیگیری مجموع پات محاسبه شده

        # ۳. ساختن پات‌ها به صورت لایه به لایه (منطق اصلی بدون تغییر)
        for tier in bet_tiers:
            tier_contribution = tier - last_bet_tier
            eligible_for_this_pot = [
                p for p in contender_details if p["total_bet"] >= tier
            ]

            pot_size = tier_contribution * len(eligible_for_this_pot)
            calculated_pot_total += pot_size

            if pot_size > 0:
                best_score_in_pot = max(p["score"] for p in eligible_for_this_pot)

                pot_winners_info = [
                    {
                        "player": p["player"],
                        "hand_cards": p["hand_cards"],
                        "hand_type": p["hand_type"],
                    }
                    for p in eligible_for_this_pot
                    if p["score"] == best_score_in_pot
                ]

                winners_by_pot.append({"amount": pot_size, "winners": pot_winners_info})

            last_bet_tier = tier

        # --- FIX: مرحله حیاتی تطبیق و اصلاح نهایی ---
        # اینجا جادو اتفاق می‌افتد: ما پات محاسبه‌شده را با پات واقعی مقایسه می‌کنیم.
        # اگر پولی (مثل بلایندها) جا مانده باشد، آن را به پات اصلی اضافه می‌کنیم.
        discrepancy = game.pot - calculated_pot_total
        if discrepancy > 0 and winners_by_pot:
            # پول گمشده را به اولین پات (پات اصلی) اضافه کن
            winners_by_pot[0]["amount"] += discrepancy
        elif discrepancy < 0:
            # این حالت نباید رخ دهد، اما برای اطمینان لاگ می‌گیریم
            logger.error(
                "Pot calculation mismatch",
                extra={
                    "chat_id": game.chat_id if hasattr(game, "chat_id") else None,
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
            except Exception:
                pass

        # --- FIX 2: ادغام پات‌های غیرضروری ---
        # اگر در نهایت فقط یک پات وجود داشت، اما به اشتباه به چند بخش تقسیم شده بود
        # (مثل سناریوی شما)، همه را در یک پات اصلی ادغام می‌کنیم.
        if len(bet_tiers) == 1 and len(winners_by_pot) > 1:
            logger.info("Merging unnecessary side pots into a single main pot")
            main_pot = {"amount": game.pot, "winners": winners_by_pot[0]["winners"]}
            return [main_pot], contender_details

        return winners_by_pot, contender_details

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
            await self._go_to_next_street(game, chat_id, context)
            return None

        # شرط ۲: آیا دور شرط‌بندی فعلی به پایان رسیده است؟
        if self._is_betting_round_over(game):
            await self._go_to_next_street(game, chat_id, context)
            return None

        # شرط ۳: بازی ادامه دارد، نوبت را به بازیکن بعدی منتقل کن
        next_player_index = self._round_rate._find_next_active_player_index(
            game, game.current_player_index
        )

        if next_player_index != -1:
            game.current_player_index = next_player_index
            return game.players[next_player_index]

        # اگر هیچ بازیکن فعالی برای حرکت بعدی وجود ندارد (مثلاً همه All-in هستند)
        await self._go_to_next_street(game, chat_id, context)
        return None

    async def _send_turn_message(
        self,
        game: Game,
        player: Player,
        chat_id: ChatId,
    ):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف در آینده ذخیره می‌کند."""
        async with self._chat_guard(chat_id):
            stage_lock = await self._get_stage_lock(chat_id)
            async with stage_lock:
                game.chat_id = chat_id
                await self._view.update_player_anchors_and_keyboards(game)

                money = await player.wallet.value()
                recent_actions = list(game.last_actions)

                turn_update = await self._view.update_turn_message(
                    chat_id=chat_id,
                    game=game,
                    player=player,
                    money=money,
                    message_id=game.turn_message_id,
                    recent_actions=recent_actions,
                )

                if turn_update.message_id:
                    game.turn_message_id = turn_update.message_id

                game.last_turn_time = datetime.datetime.now()

                logger.debug(
                    "Turn message refreshed",
                    extra={
                        "chat_id": chat_id,
                        "turn_message_id": game.turn_message_id,
                    },
                )

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

    async def _go_to_next_street(
        self, game: Game, chat_id: ChatId, context: CallbackContext
    ) -> None:
        """
        بازی را به مرحله بعدی (street) می‌برد.
        این متد مسئولیت‌های زیر را بر عهده دارد:
        1. جمع‌آوری شرط‌های این دور و افزودن به پات اصلی.
        2. ریست کردن وضعیت‌های مربوط به دور (مثل has_acted و round_rate).
        3. تعیین اینکه آیا باید به مرحله بعد برویم یا بازی با showdown تمام می‌شود.
        4. پخش کردن کارت‌های جدید روی میز (فلاپ، ترن، ریور).
        5. پیدا کردن اولین بازیکن فعال برای شروع دور شرط‌بندی جدید.
        6. اگر فقط یک بازیکن باقی مانده باشد، او را برنده اعلام می‌کند.
        """
        async with self._chat_guard(chat_id):
            game.chat_id = chat_id
            # پیام‌های نوبت قبلی را حذف نمی‌کنیم
            if game.turn_message_id:
                logger.debug(
                    "Keeping turn message %s in chat %s",
                    game.turn_message_id,
                    chat_id,
                )

            # بررسی می‌کنیم چند بازیکن هنوز در بازی هستند (Active یا All-in)
            contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
            if len(contenders) <= 1:
                # اگر فقط یک نفر باقی مانده، مستقیم به showdown می‌رویم تا برنده مشخص شود
                await self._showdown(game, chat_id, context)
                return

            # جمع‌آوری پول‌های شرط‌بندی شده در این دور و ریست کردن وضعیت بازیکنان
            self._round_rate.collect_bets_for_pot(game)
            for p in game.players:
                p.has_acted = False  # <-- این خط برای دور بعدی حیاتی است

            # رفتن به مرحله بعدی بر اساس وضعیت فعلی بازی
            stage_transitions: Dict[GameState, Tuple[GameState, int, str]] = {
                GameState.ROUND_PRE_FLOP: (GameState.ROUND_FLOP, 3, "🃏 فلاپ"),
                GameState.ROUND_FLOP: (GameState.ROUND_TURN, 1, "🃏 ترن"),
                GameState.ROUND_TURN: (GameState.ROUND_RIVER, 1, "🃏 ریور"),
            }

            transition = stage_transitions.get(game.state)
            if transition:
                next_state, card_count, stage_label = transition
                game.state = next_state
                await self.add_cards_to_table(card_count, game, chat_id, stage_label)
                if card_count == 0:
                    await self._view.update_player_anchors_and_keyboards(game)
            elif game.state == GameState.ROUND_RIVER:
                # بعد از ریور، دور شرط‌بندی تمام شده و باید showdown انجام شود
                await self._showdown(game, chat_id, context)
                return  # <-- مهم: بعد از فراخوانی showdown، ادامه نمی‌دهیم

            # اگر هنوز بازیکنی برای بازی وجود دارد، نوبت را به نفر اول می‌دهیم
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if not active_players:
                # اگر هیچ بازیکن فعالی نمانده (همه All-in هستند)، مستقیم به مراحل بعدی می‌رویم
                # تا همه کارت‌ها رو شوند.
                await self._go_to_next_street(game, chat_id, context)
                return

            # پیدا کردن اولین بازیکن برای شروع دور جدید (معمولاً اولین فرد فعال بعد از دیلر)
            first_player_index = self._get_first_player_index(game)
            game.current_player_index = first_player_index

            # اگر بازیکنی برای بازی پیدا شد، حلقه بازی را مجدداً شروع می‌کنیم
            if game.current_player_index != -1:
                next_player = await self._process_playing(chat_id, game, context)
                if next_player:
                    await self._send_turn_message(game, next_player, chat_id)
            else:
                # اگر به هر دلیلی بازیکنی پیدا نشد، به مرحله بعد می‌رویم
                await self._go_to_next_street(game, chat_id, context)

    def _determine_all_scores(self, game: Game) -> List[Dict]:
        """
        برای تمام بازیکنان فعال، دست و امتیازشان را محاسبه کرده و لیستی از دیکشنری‌ها را برمی‌گرداند.
        این متد باید از نسخه بروز شده WinnerDetermination استفاده کند.
        """
        player_scores = []
        # بازیکنانی که فولد نکرده‌اند در تعیین نتیجه شرکت می‌کنند
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        if not contenders:
            return []

        for player in contenders:
            if not player.cards:
                continue

            # **نکته مهم**: متد get_hand_value در WinnerDetermination باید بروز شود تا سه مقدار برگرداند
            # score, best_hand, hand_type = self._winner_determine.get_hand_value(player.cards, game.cards_table)

            # پیاده‌سازی موقت تا زمان آپدیت winnerdetermination
            # در اینجا فرض می‌کنیم متد `get_hand_value_and_type` در کلاس `WinnerDetermination` وجود دارد
            try:
                score, best_hand, hand_type = (
                    self._winner_determine.get_hand_value_and_type(
                        player.cards, game.cards_table
                    )
                )
            except AttributeError:
                # اگر `get_hand_value_and_type` هنوز پیاده سازی نشده است، این بخش اجرا می شود.
                # این یک fallback موقت است.
                logger.warning(
                    "'get_hand_value_and_type' not found in WinnerDetermination",
                    extra={"chat_id": getattr(game, "chat_id", None)},
                )
                score, best_hand = self._winner_determine.get_hand_value(
                    player.cards, game.cards_table
                )
                # یک روش موقت برای حدس زدن نوع دست بر اساس امتیاز
                hand_type_value = score // (15**5)
                hand_type = (
                    HandsOfPoker(hand_type_value)
                    if hand_type_value > 0
                    else HandsOfPoker.HIGH_CARD
                )

            player_scores.append(
                {
                    "player": player,
                    "score": score,
                    "best_hand": best_hand,
                    "hand_type": hand_type,
                }
            )
        return player_scores

    def _find_winners_from_scores(
        self, player_scores: List[Dict]
    ) -> Tuple[List[Player], int]:
        """از لیست امتیازات، برندگان و بالاترین امتیاز را پیدا می‌کند."""
        if not player_scores:
            return [], 0

        highest_score = max(data["score"] for data in player_scores)
        winners = [
            data["player"] for data in player_scores if data["score"] == highest_score
        ]
        return winners, highest_score

    async def add_cards_to_table(
        self,
        count: int,
        game: Game,
        chat_id: ChatId,
        street_name: str,
        send_message: bool = True,
    ):
        """
        کارت‌های جدید را به میز اضافه کرده و پیام برد را به‌روزرسانی می‌کند.

        پیام‌های لنگر بازیکنان در جای دیگری (پیام نوبت مشترک) ویرایش می‌شوند تا
        وضعیت دکمه‌های اینلاین و خطوط کارت‌ها با هم هماهنگ بمانند.
        """
        async with self._chat_guard(chat_id):
            should_refresh_anchors = count > 0
            if count > 0:
                for _ in range(count):
                    if game.remain_cards:
                        game.cards_table.append(game.remain_cards.pop())

            if should_refresh_anchors:
                await self._view.update_player_anchors_and_keyboards(game)

            if not send_message:
                return
            if game.board_message_id:
                await self._view.delete_message(chat_id, game.board_message_id)
                if game.board_message_id in game.message_ids_to_delete:
                    game.message_ids_to_delete.remove(game.board_message_id)
                game.board_message_id = None

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            # Replacing underscore with space and title-casing the output
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"

    def _build_hand_statistics_results(
        self,
        game: Game,
        payouts: Dict[int, int],
        hand_labels: Dict[int, Optional[str]],
    ) -> List[PlayerHandResult]:
        results: List[PlayerHandResult] = []
        for player in game.seated_players():
            user_id = self._safe_int(player.user_id)
            total_bet = int(getattr(player, "total_bet", 0))
            payout = int(payouts.get(user_id, 0))
            net_profit = payout - total_bet
            if net_profit > 0 or (payout > 0 and total_bet == 0):
                result_flag = "win"
            elif net_profit < 0:
                result_flag = "loss"
            else:
                result_flag = "push"
            label = hand_labels.get(user_id)
            if not label and result_flag == "win" and player.state == PlayerState.ALL_IN:
                label = "پیروزی با آل-این"
            results.append(
                PlayerHandResult(
                    user_id=user_id,
                    display_name=player.mention_markdown,
                    total_bet=total_bet,
                    payout=payout,
                    net_profit=net_profit,
                    hand_type=label,
                    was_all_in=player.state == PlayerState.ALL_IN,
                    result=result_flag,
                )
            )
        return results

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
            self._player_report_cache.invalidate(user_id_int)

    async def _clear_game_messages(self, game: Game, chat_id: ChatId) -> None:
        """Deletes all temporary messages related to the current hand."""
        async with self._chat_guard(chat_id):
            logger.debug("Clearing game messages", extra={"chat_id": chat_id})

            ids_to_delete = set(game.message_ids_to_delete)

            if game.board_message_id:
                ids_to_delete.add(game.board_message_id)
                game.board_message_id = None

            if game.turn_message_id:
                ids_to_delete.add(game.turn_message_id)
                game.turn_message_id = None

            if game.seat_announcement_message_id:
                ids_to_delete.add(game.seat_announcement_message_id)
                game.seat_announcement_message_id = None

            game.chat_id = chat_id

            for player in game.seated_players():
                anchor_message_id: Optional[MessageId] = None
                if player.anchor_message and player.anchor_message[0] == chat_id:
                    anchor_message_id = player.anchor_message[1]
                if anchor_message_id:
                    ids_to_delete.discard(anchor_message_id)

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

        game.message_ids_to_delete.clear()
        game.message_ids.clear()

    async def _clear_player_anchors(self, game: Game) -> None:
        clear_method = getattr(self._view, "clear_all_player_anchors", None)
        if callable(clear_method):
            await clear_method(game)

    async def _showdown(
        self, game: Game, chat_id: ChatId, context: CallbackContext
    ) -> None:
        """Serialize showdown processing so Telegram calls stay ordered."""

        async with self._chat_guard(chat_id):
            await self._showdown_impl(game, chat_id, context)

    async def _showdown_impl(
        self, game: Game, chat_id: ChatId, context: CallbackContext
    ) -> None:
        """
        فرآیند پایان دست را با استفاده از خروجی دقیق _determine_winners مدیریت می‌کند.
        """

        async def _send_with_retry(func, *args, retries: int = 3):
            for attempt in range(retries):
                try:
                    await func(*args)
                    return
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    logger.error(
                        "Error sending message attempt",
                        extra={
                            "error_type": type(e).__name__,
                            "request_params": {"attempt": attempt + 1, "args": args},
                        },
                    )
                    if attempt + 1 >= retries:
                        return
                    # Background tasks now handle retries without manual pacing.

        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        game.chat_id = chat_id

        await self._clear_game_messages(game, chat_id)

        hand_id = game.id
        pot_total = game.pot
        payouts: Dict[int, int] = defaultdict(int)
        hand_labels: Dict[int, Optional[str]] = {}

        if not contenders:
            # سناریوی نادر که همه قبل از showdown فولد کرده‌اند
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
            # ۱. تعیین برندگان و تقسیم تمام پات‌ها (اصلی و فرعی)
            determine_output = self._determine_winners(game, contenders)
            if isinstance(determine_output, tuple):
                winners_by_pot = list(determine_output[0] or [])
                contender_details = (
                    list(determine_output[1] or [])
                    if len(determine_output) > 1
                    else []
                )
            else:
                winners_by_pot = list(determine_output or [])
                contender_details = []

            for detail in contender_details:
                player = detail.get("player")
                if not player:
                    continue
                label = self._hand_type_to_label(detail.get("hand_type"))
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
                            winner_label = self._hand_type_to_label(
                                winner.get("hand_type")
                            )
                            if winner_label and self._safe_int(player.user_id) not in hand_labels:
                                hand_labels[self._safe_int(player.user_id)] = winner_label
            else:
                await self._view.send_message(
                    chat_id,
                    "ℹ️ هیچ برنده‌ای در این دست مشخص نشد. مشکلی در منطق بازی رخ داده است.",
                )

            await _send_with_retry(
                self._view.send_showdown_results, chat_id, game, winners_by_pot
            )

        if self._stats_enabled():
            stats_results = self._build_hand_statistics_results(
                game,
                dict(payouts),
                hand_labels,
            )
            await self._stats.finish_hand(
                hand_id=hand_id,
                chat_id=self._safe_int(chat_id),
                results=stats_results,
                pot_total=pot_total,
            )
            self._player_report_cache.invalidate_many(
                self._safe_int(player.user_id) for player in game.players
            )

        game.pot = 0

        # ۳. آماده‌سازی برای دست بعدی
        remaining_players = []
        for p in game.players:
            if await p.wallet.value() > 0:
                remaining_players.append(p)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in remaining_players]

        await self._request_metrics.end_cycle(
            self._safe_int(chat_id), cycle_token=game.id
        )
        await self._clear_player_anchors(game)
        game.reset()
        await self._table_manager.save_game(chat_id, game)

        await _send_with_retry(self._view.send_new_hand_ready_message, chat_id)
        await self._send_join_prompt(game, chat_id)

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

        now = datetime.datetime.now()
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
