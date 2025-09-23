from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError, NoScriptError
from telegram import ReplyKeyboardMarkup, User
from telegram.helpers import mention_markdown as format_mention_markdown

from pokerapp.entities import ChatId, Player, UserId, Wallet
from pokerapp.player_manager import PlayerManager
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.stats import BaseStatsService, PlayerIdentity
from pokerapp.table_manager import TableManager
from pokerapp.utils.markdown import escape_markdown_v1
from pokerapp.utils.request_metrics import RequestMetrics

T = TypeVar("T")


@dataclass(slots=True)
class PrivateMatchPlayerInfo:
    user_id: int
    chat_id: Optional[int]
    display_name: str
    username: Optional[str] = None


class PrivateMatchService:
    PRIVATE_MATCH_QUEUE_KEY = "pokerbot:private_matchmaking:queue"
    PRIVATE_MATCH_USER_KEY_PREFIX = "pokerbot:private_matchmaking:user:"
    PRIVATE_MATCH_RECORD_KEY_PREFIX = "pokerbot:private_matchmaking:match:"
    PRIVATE_MATCH_QUEUE_TTL = 180  # seconds
    PRIVATE_MATCH_STATE_TTL = 3600  # seconds

    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_BACKOFF_SECONDS = 0.2

    def __init__(
        self,
        kv: aioredis.Redis,
        table_manager: TableManager,
        logger: logging.Logger,
    ) -> None:
        self._kv = kv
        self._table_manager = table_manager
        self._logger = logger
        self._max_attempts = self.DEFAULT_MAX_ATTEMPTS
        self._base_backoff = self.DEFAULT_BACKOFF_SECONDS
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
        data = await self._execute_with_retry(lambda: self._kv.hgetall(key))
        if not data:
            return {}
        return self._decode_hash(data)

    async def cleanup_private_queue(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff_ts = int(now.timestamp()) - self.PRIVATE_MATCH_QUEUE_TTL
        expired = await self._execute_with_retry(
            lambda: self._kv.zrangebyscore(
                self.PRIVATE_MATCH_QUEUE_KEY, "-inf", cutoff_ts
            )
        )
        if not expired:
            return
        for raw_user_id in expired:
            if isinstance(raw_user_id, bytes):
                user_id_str = raw_user_id.decode()
            else:
                user_id_str = str(raw_user_id)
            await self._execute_with_retry(
                lambda member=raw_user_id: self._kv.zrem(
                    self.PRIVATE_MATCH_QUEUE_KEY, member
                )
            )
            state = await self.get_private_match_state(user_id_str)
            key = self.private_user_key(user_id_str)
            await self._execute_with_retry(lambda key=key: self._kv.delete(key))
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
        popped = await self._execute_with_retry(
            lambda: self._kv.zpopmin(self.PRIVATE_MATCH_QUEUE_KEY, 2)
        )
        if not popped:
            return None
        if len(popped) < 2:
            member, score = popped[0]
            await self._execute_with_retry(
                lambda member=member, score=score: self._kv.zadd(
                    self.PRIVATE_MATCH_QUEUE_KEY, {member: score}
                )
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
                await self._execute_with_retry(
                    lambda user_id_str=user_id_str, score_value=score_value: self._kv.zadd(
                        self.PRIVATE_MATCH_QUEUE_KEY, {user_id_str: score_value}
                    )
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
            await self._execute_with_retry(
                lambda state_key=state_key, state=state, opponent=opponent: self._kv.hset(
                    state_key,
                    mapping={
                        "status": "matched",
                        "opponent": str(opponent.user_id),
                        "matched_at": str(now_ts),
                        "chat_id": state.get("chat_id", ""),
                        "display_name": state.get("display_name", ""),
                        "username": state.get("username", ""),
                    },
                )
            )
            await self._execute_with_retry(
                lambda state_key=state_key: self._kv.expire(
                    state_key, self.PRIVATE_MATCH_STATE_TTL
                )
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
        await self._execute_with_retry(
            lambda state_key=state_key: self._kv.hset(
                state_key,
                mapping={
                    "status": "queued",
                    "timestamp": str(timestamp),
                    "chat_id": str(chat_id),
                    "display_name": display_name,
                    "username": username,
                },
            )
        )
        await self._execute_with_retry(
            lambda state_key=state_key: self._kv.expire(
                state_key, self.PRIVATE_MATCH_STATE_TTL
            )
        )
        await self._execute_with_retry(
            lambda user=user, timestamp=timestamp: self._kv.zadd(
                self.PRIVATE_MATCH_QUEUE_KEY,
                {str(self._safe_int(user.id)): timestamp},
            )
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
        await self._execute_with_retry(
            lambda match_key=match_key: self._kv.hset(
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
        )
        await self._execute_with_retry(
            lambda match_key=match_key: self._kv.expire(
                match_key, self.PRIVATE_MATCH_STATE_TTL
            )
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
            await self._execute_with_retry(
                lambda state_key=state_key, info=info, opponent=opponent: self._kv.hset(
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
            )
            await self._execute_with_retry(
                lambda state_key=state_key: self._kv.expire(
                    state_key, self.PRIVATE_MATCH_STATE_TTL
                )
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
        return f"{self.PRIVATE_MATCH_USER_KEY_PREFIX}{self._safe_int(user_id)}"

    @staticmethod
    def private_match_key(match_id: str) -> str:
        return f"{PrivateMatchService.PRIVATE_MATCH_RECORD_KEY_PREFIX}{match_id}"

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

    async def _execute_with_retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        delay = self._base_backoff
        while True:
            try:
                return await operation()
            except (NoScriptError, RedisConnectionError) as exc:
                attempt += 1
                if attempt >= self._max_attempts:
                    self._logger.exception(
                        "Redis operation failed after %s attempts", attempt
                    )
                    raise
                self._logger.warning(
                    "Redis operation failed (attempt %s/%s): %s",
                    attempt,
                    self._max_attempts,
                    exc,
                )
                await asyncio.sleep(delay)
                delay *= 2

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

