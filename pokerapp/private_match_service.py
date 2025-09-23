from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import redis.asyncio as aioredis
from telegram import ReplyKeyboardMarkup, User
from telegram.helpers import mention_markdown as format_mention_markdown

from pokerapp.entities import ChatId, Player, UserId, Wallet
from pokerapp.player_manager import PlayerManager
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.stats import BaseStatsService, PlayerIdentity
from pokerapp.table_manager import TableManager
from pokerapp.utils.markdown import escape_markdown_v1
from pokerapp.utils.request_metrics import RequestMetrics
from pokerapp.utils.redis_safeops import RedisSafeOps
from pokerapp.config import GameConstants, get_game_constants


_DEFAULT_REDIS_CONSTANTS = get_game_constants().redis


def _positive_int(value: Optional[int], default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed

@dataclass(slots=True)
class PrivateMatchPlayerInfo:
    user_id: int
    chat_id: Optional[int]
    display_name: str
    username: Optional[str] = None


class PrivateMatchService:
    PRIVATE_MATCH_QUEUE_KEY = _DEFAULT_REDIS_CONSTANTS.get(
        "private_match_queue_key", "pokerbot:private_matchmaking:queue"
    )
    PRIVATE_MATCH_USER_KEY_PREFIX = _DEFAULT_REDIS_CONSTANTS.get(
        "private_match_user_key_prefix", "pokerbot:private_matchmaking:user:"
    )
    PRIVATE_MATCH_RECORD_KEY_PREFIX = _DEFAULT_REDIS_CONSTANTS.get(
        "private_match_record_key_prefix", "pokerbot:private_matchmaking:match:"
    )
    PRIVATE_MATCH_QUEUE_TTL = _positive_int(
        _DEFAULT_REDIS_CONSTANTS.get("private_match_queue_ttl"), 180
    )
    PRIVATE_MATCH_STATE_TTL = _positive_int(
        _DEFAULT_REDIS_CONSTANTS.get("private_match_state_ttl"), 3600
    )

    def __init__(
        self,
        kv: aioredis.Redis,
        table_manager: TableManager,
        logger: logging.Logger,
        *,
        constants: Optional[GameConstants] = None,
        redis_ops: Optional[RedisSafeOps] = None,
    ) -> None:
        self._table_manager = table_manager
        self._logger = logger
        self._redis_ops = redis_ops or RedisSafeOps(
            kv, logger=logger.getChild("redis_safeops")
        )
        self._constants = constants or get_game_constants()
        redis_constants = self._constants.redis or {}
        self._queue_key = str(
            redis_constants.get("private_match_queue_key", self.PRIVATE_MATCH_QUEUE_KEY)
        )
        self._user_key_prefix = str(
            redis_constants.get(
                "private_match_user_key_prefix",
                self.PRIVATE_MATCH_USER_KEY_PREFIX,
            )
        )
        self._record_key_prefix = str(
            redis_constants.get(
                "private_match_record_key_prefix",
                self.PRIVATE_MATCH_RECORD_KEY_PREFIX,
            )
        )
        self._queue_ttl = _positive_int(
            redis_constants.get("private_match_queue_ttl"),
            self.PRIVATE_MATCH_QUEUE_TTL,
        )
        self._state_ttl = _positive_int(
            redis_constants.get("private_match_state_ttl"),
            self.PRIVATE_MATCH_STATE_TTL,
        )
        self._safe_int_fn: Optional[Callable[[UserId], int]] = None
        self._build_private_menu: Optional[Callable[[], ReplyKeyboardMarkup]] = None
        self._view: Optional[PokerBotViewer] = None
        self._player_manager: Optional[PlayerManager] = None
        self._request_metrics: Optional[RequestMetrics] = None
        self._stats: Optional[BaseStatsService] = None
        self._stats_enabled: Optional[Callable[[], bool]] = None
        self._build_identity_from_player: Optional[Callable[[Player], PlayerIdentity]] = None
        self._clear_player_anchors: Optional[Callable[[object], Awaitable[None]]] = None
        self._wallet_factory: Optional[Callable[[int], Wallet]] = None

    def configure(
        self,
        *,
        safe_int: Callable[[UserId], int],
        build_private_menu: Callable[[], ReplyKeyboardMarkup],
        view: PokerBotViewer,
        player_manager: PlayerManager,
        request_metrics: RequestMetrics,
        stats_service: BaseStatsService,
        stats_enabled: Callable[[], bool],
        build_identity_from_player: Callable[[Player], PlayerIdentity],
        clear_player_anchors: Callable[[object], Awaitable[None]],
        wallet_factory: Callable[[int], Wallet],
    ) -> None:
        self._safe_int_fn = safe_int
        self._build_private_menu = build_private_menu
        self._view = view
        self._player_manager = player_manager
        self._request_metrics = request_metrics
        self._stats = stats_service
        self._stats_enabled = stats_enabled
        self._build_identity_from_player = build_identity_from_player
        self._clear_player_anchors = clear_player_anchors
        self._wallet_factory = wallet_factory

    async def get_private_match_state(self, user_id: UserId) -> Dict[str, str]:
        key = self.private_user_key(user_id)
        extra = {"user_id": self._safe_int(user_id)}
        data = await self._redis_ops.safe_hgetall(key, log_extra=extra)
        if not data:
            return {}
        return self._decode_hash(data)

    async def cleanup_private_queue(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff_ts = int(now.timestamp()) - self._queue_ttl
        expired = await self._redis_ops.safe_zrangebyscore(
            self._queue_key,
            "-inf",
            cutoff_ts,
            log_extra={"queue": self._queue_key},
        )
        if not expired:
            return
        for raw_user_id in expired:
            if isinstance(raw_user_id, bytes):
                user_id_str = raw_user_id.decode()
            else:
                user_id_str = str(raw_user_id)
            user_extra = {"user_id": self._safe_int(user_id_str)}
            await self._redis_ops.safe_zrem(
                self._queue_key,
                raw_user_id,
                log_extra=user_extra,
            )
            state = await self.get_private_match_state(user_id_str)
            key = self.private_user_key(user_id_str)
            await self._redis_ops.safe_delete(key, log_extra=user_extra)
            chat_id = (
                self._coerce_optional_int(state.get("chat_id")) if state else None
            )
            if chat_id:
                view = self._require_view()
                reply_markup = self._require_private_menu_builder()()
                await view.send_message(
                    chat_id,
                    "⏳ زمان انتظار شما به پایان رسید و از صف بازی خصوصی خارج شدید.",
                    reply_markup=reply_markup,
                )

    async def try_pop_match(self) -> Optional[List[PrivateMatchPlayerInfo]]:
        popped = await self._redis_ops.safe_zpopmin(
            self._queue_key,
            2,
            log_extra={"queue": self._queue_key},
        )
        if not popped:
            return None
        if len(popped) < 2:
            member, score = popped[0]
            await self._redis_ops.safe_zadd(
                self._queue_key,
                {member: score},
                log_extra={"queue": self._queue_key},
            )
            return None
        states: List[Tuple[str, Dict[str, str], float]] = []
        for member, score in popped:
            user_id_str = member.decode() if isinstance(member, bytes) else str(member)
            state = await self.get_private_match_state(user_id_str)
            states.append((user_id_str, state, score))
        valid = [item for item in states if item[1].get("status") == "queued"]
        if len(valid) < 2:
            for user_id_str, state, score in states:
                timestamp = state.get("timestamp") if state else None
                score_value = int(timestamp) if timestamp else score
                await self._redis_ops.safe_zadd(
                    self._queue_key,
                    {user_id_str: score_value},
                    log_extra={"user_id": self._safe_int(user_id_str)},
                )
            return None
        players = [
            self._build_player_info_from_state(user_id_str, state)
            for user_id_str, state, _ in valid[:2]
        ]
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for idx, (user_id_str, state, _) in enumerate(valid[:2]):
            opponent = players[1 - idx]
            state_key = self.private_user_key(user_id_str)
            user_extra = {"user_id": self._safe_int(user_id_str)}
            await self._redis_ops.safe_hset(
                state_key,
                {
                    "status": "matched",
                    "opponent": str(opponent.user_id),
                    "matched_at": str(now_ts),
                    "chat_id": state.get("chat_id", ""),
                    "display_name": state.get("display_name", ""),
                    "username": state.get("username", ""),
                },
                log_extra=user_extra,
            )
            await self._redis_ops.safe_expire(
                state_key, self._state_ttl, log_extra=user_extra
            )
        return players

    async def enqueue_private_player(
        self, user: User, chat_id: int
    ) -> Dict[str, object]:
        existing_state = await self.get_private_match_state(user.id)
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
        state_key = self.private_user_key(user.id)
        user_extra = {
            "user_id": self._safe_int(user.id),
            "chat_id": chat_id,
        }
        await self._redis_ops.safe_hset(
            state_key,
            {
                "status": "queued",
                "timestamp": str(timestamp),
                "chat_id": str(chat_id),
                "display_name": display_name,
                "username": username,
            },
            log_extra=user_extra,
        )
        await self._redis_ops.safe_expire(
            state_key, self._state_ttl, log_extra=user_extra
        )
        await self._redis_ops.safe_zadd(
            self._queue_key,
            {str(self._safe_int(user.id)): timestamp},
            log_extra=user_extra,
        )

        players = await self.try_pop_match()
        if players:
            return {"status": "matched", "players": players}
        return {"status": "queued"}

    async def start_private_headsup_game(
        self, players: List[PrivateMatchPlayerInfo]
    ) -> str:
        if len(players) != 2:
            raise ValueError("Private heads-up games require exactly two players")
        match_id = f"pm_{uuid.uuid4().hex}"
        chat_id: ChatId = f"private:{match_id}"
        game = await self._table_manager.create_game(chat_id)
        await self._require_request_metrics().end_cycle(
            self._safe_int(chat_id), cycle_token=game.id
        )
        await self._require_clear_player_anchors()(game)
        game.reset()
        for index, info in enumerate(players):
            safe_user_id = self._safe_int(info.user_id)
            wallet_factory = self._require_wallet_factory()
            wallet = wallet_factory(safe_user_id)
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
                self._require_player_manager().private_chat_ids[safe_user_id] = info.chat_id
        await self._table_manager.save_game(chat_id, game)

        started_at = datetime.datetime.now(datetime.timezone.utc)
        match_key = self.private_match_key(match_id)
        match_extra = {"match_id": match_id, "chat_id": chat_id}
        await self._redis_ops.safe_hset(
            match_key,
            {
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
            log_extra=match_extra,
        )
        await self._redis_ops.safe_expire(
            match_key, self._state_ttl, log_extra=match_extra
        )

        if self._require_stats_enabled()():
            identities = [
                self._require_build_identity_from_player()(p) for p in game.players
            ]
            await self._require_stats().start_hand(
                match_id, chat_id, identities, start_time=started_at
            )

        for idx, info in enumerate(players):
            opponent = players[1 - idx]
            state_key = self.private_user_key(info.user_id)
            state_extra = {
                "user_id": self._safe_int(info.user_id),
                "match_id": match_id,
                "chat_id": chat_id,
            }
            await self._redis_ops.safe_hset(
                state_key,
                {
                    "status": "playing",
                    "match_id": match_id,
                    "opponent": str(self._safe_int(opponent.user_id)),
                    "opponent_name": opponent.display_name,
                    "chat_id": str(info.chat_id or ""),
                    "display_name": info.display_name,
                    "username": info.username or "",
                },
                log_extra=state_extra,
            )
            await self._redis_ops.safe_expire(
                state_key, self._state_ttl, log_extra=state_extra
            )
            if info.chat_id:
                opponent_name_raw = (
                    opponent.display_name or str(self._safe_int(opponent.user_id))
                )
                opponent_name = escape_markdown_v1(opponent_name_raw)
                message = (
                    "🤝 حریف شما پیدا شد!\n"
                    f"🎮 بازی خصوصی با {opponent_name} تا لحظاتی دیگر آغاز می‌شود.\n"
                    f"🆔 شناسه بازی: {match_id}"
                )
                reply_markup = self._require_private_menu_builder()()
                await self._require_view().send_message(
                    info.chat_id,
                    message,
                    reply_markup=reply_markup,
                )

        return match_id

    def private_user_key(self, user_id: UserId) -> str:
        return f"{self._user_key_prefix}{self._safe_int(user_id)}"

    def private_match_key(self, match_id: str) -> str:
        return f"{self._record_key_prefix}{match_id}"

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

    def _safe_int(self, value: UserId) -> int:
        if self._safe_int_fn is None:
            raise RuntimeError("PrivateMatchService safe_int dependency not configured")
        return self._safe_int_fn(value)

    def _require_view(self) -> PokerBotViewer:
        if self._view is None:
            raise RuntimeError("PrivateMatchService view dependency not configured")
        return self._view

    def _require_player_manager(self) -> PlayerManager:
        if self._player_manager is None:
            raise RuntimeError(
                "PrivateMatchService player_manager dependency not configured"
            )
        return self._player_manager

    def _require_request_metrics(self) -> RequestMetrics:
        if self._request_metrics is None:
            raise RuntimeError(
                "PrivateMatchService request_metrics dependency not configured"
            )
        return self._request_metrics

    def _require_stats(self) -> BaseStatsService:
        if self._stats is None:
            raise RuntimeError("PrivateMatchService stats dependency not configured")
        return self._stats

    def _require_stats_enabled(self) -> Callable[[], bool]:
        if self._stats_enabled is None:
            raise RuntimeError(
                "PrivateMatchService stats_enabled dependency not configured"
            )
        return self._stats_enabled

    def _require_build_identity_from_player(self) -> Callable[[Player], PlayerIdentity]:
        if self._build_identity_from_player is None:
            raise RuntimeError(
                "PrivateMatchService build_identity_from_player not configured"
            )
        return self._build_identity_from_player

    def _require_clear_player_anchors(self) -> Callable[[object], Awaitable[None]]:
        if self._clear_player_anchors is None:
            raise RuntimeError(
                "PrivateMatchService clear_player_anchors dependency not configured"
            )
        return self._clear_player_anchors

    def _require_private_menu_builder(self) -> Callable[[], ReplyKeyboardMarkup]:
        if self._build_private_menu is None:
            raise RuntimeError(
                "PrivateMatchService build_private_menu dependency not configured"
            )
        return self._build_private_menu

    def _require_wallet_factory(self) -> Callable[[int], Wallet]:
        if self._wallet_factory is None:
            raise RuntimeError(
                "PrivateMatchService wallet_factory dependency not configured"
            )
        return self._wallet_factory

    @property
    def queue_key(self) -> str:
        return self._queue_key

    @property
    def queue_ttl(self) -> int:
        return self._queue_ttl

    @property
    def state_ttl(self) -> int:
        return self._state_ttl

