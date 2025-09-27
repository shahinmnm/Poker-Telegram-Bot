import json
import logging
import pickle
import time
from datetime import datetime
from typing import Dict, Optional, Union

import redis.asyncio as aioredis

from pokerapp.entities import Game, ChatId
from pokerapp.utils.redis_safeops import RedisSafeOps


class TableManager:
    """Manage a single poker game per chat and persist it in Redis."""

    def __init__(
        self,
        redis: aioredis.Redis,
        wallet_redis: Optional[aioredis.Redis] = None,
        *,
        redis_ops: Optional[RedisSafeOps] = None,
        wallet_redis_ops: Optional[RedisSafeOps] = None,
    ):
        self._redis = redis
        self._wallet_redis = wallet_redis or redis
        base_logger = logging.getLogger(__name__)
        self._logger = base_logger
        self._redis_ops = redis_ops or RedisSafeOps(
            redis, logger=base_logger.getChild("redis_safeops")
        )
        if wallet_redis is None or wallet_redis is redis:
            self._wallet_ops = wallet_redis_ops or self._redis_ops
        else:
            self._wallet_ops = wallet_redis_ops or RedisSafeOps(
                self._wallet_redis,
                logger=base_logger.getChild("wallet_redis_safeops"),
            )
        # Keep games cached in memory keyed only by chat_id
        self._tables: Dict[ChatId, Game] = {}

    # Keys ---------------------------------------------------------------
    @staticmethod
    def _game_key(chat_id: ChatId) -> str:
        return f"chat:{chat_id}:game"

    @staticmethod
    def _player_chat_key(user_id: str) -> str:
        return f"player:{user_id}:chat"

    @staticmethod
    def _chat_players_key(chat_id: ChatId) -> str:
        return f"chat:{chat_id}:players"

    # Public API ---------------------------------------------------------
    async def create_game(self, chat_id: ChatId) -> Game:
        """Create a new game for the chat and persist it."""
        game = Game()
        self._tables[chat_id] = game
        await self._save(chat_id, game)
        return game

    async def get_game(self, chat_id: ChatId) -> Game:
        """Load the chat's game, creating one if necessary."""
        section_start = time.time()
        logging.getLogger(__name__).debug(
            "[LOCK_SECTION_START] chat_id=%s action=get_game",
            chat_id,
        )
        if chat_id in self._tables:
            logging.getLogger(__name__).debug(
                "[LOCK_SECTION_END] chat_id=%s action=get_game elapsed=%.3fs",
                chat_id,
                time.time() - section_start,
            )
            return self._tables[chat_id]

        extra = {"chat_id": chat_id}
        data = await self._redis_ops.safe_get(
            self._game_key(chat_id), log_extra=extra
        )
        if data:
            game = pickle.loads(data)
            if self._wallet_redis is not None:
                from pokerapp.pokerbotmodel import WalletManagerModel

                for player in game.players:
                    info = getattr(player, "_wallet_info", {"user_id": player.user_id})
                    player.wallet = WalletManagerModel(info["user_id"], self._wallet_redis)
                    if hasattr(player, "_wallet_info"):
                        delattr(player, "_wallet_info")
        else:
            game = Game()
            await self._save(chat_id, game)

        self._tables[chat_id] = game
        logging.getLogger(__name__).debug(
            "[LOCK_SECTION_END] chat_id=%s action=get_game elapsed=%.3fs",
            chat_id,
            time.time() - section_start,
        )
        return game

    async def save_game(self, chat_id: ChatId, game: Game) -> None:
        self._tables[chat_id] = game
        await self._save(chat_id, game)

    async def find_game_by_user(self, user_id: int) -> tuple[Game, ChatId]:
        """Return the game and chat id for the given user.

        Searches only the in-memory cache of games and raises ``LookupError``
        if the user is not currently associated with any cached game.
        """
        for chat_id, game in self._tables.items():
            if any(p.user_id == user_id for p in game.players):
                return game, chat_id

        chat_id_data = await self._redis_ops.safe_get(
            self._player_chat_key(str(user_id)),
            log_extra={"user_id": user_id},
        )
        if chat_id_data is None:
            raise LookupError(f"No game found for user {user_id}")

        if isinstance(chat_id_data, bytes):
            chat_id_value = chat_id_data.decode()
        else:
            chat_id_value = chat_id_data

        if isinstance(chat_id_value, str):
            try:
                chat_id_parsed = int(chat_id_value)
            except (TypeError, ValueError):
                chat_id_parsed = chat_id_value
        else:
            chat_id_parsed = chat_id_value

        game = await self.get_game(chat_id_parsed)
        return game, chat_id_parsed

    # Internal -----------------------------------------------------------
    async def _save(self, chat_id: ChatId, game: Game) -> None:
        section_start = time.time()
        self._redis_ops.logger.debug(
            "[LOCK_SECTION_START] chat_id=%s action=_save",
            chat_id,
        )
        try:
            data = pickle.dumps(game)
            await self._redis_ops.safe_set(
                self._game_key(chat_id),
                data,
                log_extra={"chat_id": chat_id},
            )
            await self._update_player_index(chat_id, game)
            self._redis_ops.logger.debug(
                "[LOCK_SECTION_END] chat_id=%s action=_save elapsed=%.3fs",
                chat_id,
                time.time() - section_start,
            )
        except Exception as exc:  # noqa: BLE001 - we need broad exception for logging context
            logger = getattr(self, "_logger", logging.getLogger(__name__))
            players_attr = getattr(game, "players", [])
            if callable(players_attr):
                try:
                    players_list = list(players_attr())
                except Exception:  # noqa: BLE001 - fallback to empty snapshot on failure
                    players_list = []
            else:
                players_list = list(players_attr or [])

            player_snapshots = []
            for player in players_list:
                wallet = getattr(player, "wallet", None)
                if wallet is not None:
                    wallet_repr = (
                        getattr(wallet, "_user_id", None)
                        or getattr(wallet, "user_id", None)
                        or repr(wallet)
                    )
                else:
                    wallet_repr = None
                player_snapshots.append(
                    {
                        "user_id": getattr(player, "user_id", None),
                        "seat_index": getattr(player, "seat_index", None),
                        "wallet": wallet_repr,
                    }
                )

            ready_users = getattr(game, "ready_users", None)
            context = {
                "chat_id": chat_id,
                "player_count": len(players_list),
                "players": player_snapshots,
                "game_state": getattr(getattr(game, "state", None), "name", None),
                "dealer_index": getattr(game, "dealer_index", None),
                "ready_users_len": len(ready_users) if ready_users is not None else None,
            }

            logger.exception("Failed to save game", extra=context)

            error_payload = json.dumps(
                {
                    "error": str(exc),
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
            error_key = f"chat:{chat_id}:last_save_error"
            try:
                await self._redis_ops.safe_set(
                    error_key,
                    error_payload,
                    log_extra={"chat_id": chat_id},
                )
            except Exception:
                logger.exception(
                    "Failed to record last save error",
                    extra={"chat_id": chat_id},
                )

            detailed_error_payload = {
                "chat_id": chat_id,
                "players": [
                    {
                        "user_id": getattr(player, "user_id", None),
                        "seat_index": getattr(player, "seat_index", None),
                        "role": (
                            getattr(player, "role_label", None)
                            or getattr(player, "role", None)
                        ),
                    }
                    for player in players_list
                ],
                "player_count": len(players_list),
                "game_state": (
                    getattr(getattr(game, "state", None), "name", None)
                    or str(getattr(game, "state", None))
                ),
                "exception": str(exc),
                "pickle_size": len(data) if "data" in locals() else None,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

            try:
                await self._redis_ops.safe_set(
                    f"chat:{chat_id}:last_save_error_detailed",
                    json.dumps(detailed_error_payload),
                    log_extra={"chat_id": chat_id},
                )
            except Exception as redis_exc:
                logger.warning(
                    "Failed to record last_save_error_detailed",
                    extra={"chat_id": chat_id, "error": str(redis_exc)},
                )

            raise

    async def _update_player_index(self, chat_id: ChatId, game: Game) -> None:
        players = {
            str(player.user_id)
            for player in game.players
            if getattr(player, "user_id", None) not in (None, "")
        }
        players_key = self._chat_players_key(chat_id)

        previous_players_raw = await self._redis_ops.safe_smembers(
            players_key, log_extra={"chat_id": chat_id}
        )
        previous_players = {
            member.decode() if isinstance(member, bytes) else str(member)
            for member in previous_players_raw
        }

        stale_players = previous_players - players
        if stale_players:
            for player_id in stale_players:
                stale_key = self._player_chat_key(player_id)
                try:
                    normalized_id: Union[int, str] = int(player_id)
                except (TypeError, ValueError):
                    normalized_id = player_id
                await self._redis_ops.safe_delete(
                    stale_key, log_extra={"user_id": normalized_id}
                )

        if players:
            mapping = {
                self._player_chat_key(player_id): str(chat_id)
                for player_id in players
            }
            await self._redis_ops.safe_mset(
                mapping,
                log_extra={"chat_id": chat_id, "players": list(players)},
            )

        await self._redis_ops.safe_delete(players_key, log_extra={"chat_id": chat_id})
        if players:
            await self._redis_ops.safe_sadd(
                players_key, *players, log_extra={"chat_id": chat_id}
            )
