import json
import logging
import pickle
import time
from datetime import datetime
from typing import Dict, Optional, Tuple, Union

import redis.asyncio as aioredis
from redis import exceptions as redis_exceptions

from pokerapp.entities import ChatId, Game
from pokerapp.state_validator import (
    GameStateValidator,
    ValidationIssue,
    ValidationResult,
)
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
        state_validator: Optional[GameStateValidator] = None,
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
        self._state_validator = state_validator or GameStateValidator()

    # Keys ---------------------------------------------------------------
    @staticmethod
    def _game_key(chat_id: ChatId) -> str:
        return f"chat:{chat_id}:game"

    @staticmethod
    def _version_key(chat_id: ChatId) -> str:
        return f"game:{chat_id}:version"

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

        game, _ = await self.load_game(chat_id, validate=True)
        if game is None:
            game = Game()
            await self._save(chat_id, game)

        self._tables[chat_id] = game
        logging.getLogger(__name__).debug(
            "[LOCK_SECTION_END] chat_id=%s action=get_game elapsed=%.3fs",
            chat_id,
            time.time() - section_start,
        )
        return game

    async def load_game(
        self, chat_id: ChatId, *, validate: bool = True
    ) -> Tuple[Optional[Game], Optional[ValidationResult]]:
        """Load a game snapshot from Redis without caching it."""

        extra = {"chat_id": chat_id}
        data = await self._redis_ops.safe_get(
            self._game_key(chat_id), log_extra=extra
        )
        if not data:
            return None, None

        try:
            game = pickle.loads(data)
        except (
            pickle.UnpicklingError,
            AttributeError,
            EOFError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            self._logger.warning(
                "Failed to decode persisted game; deleting",
                extra={**extra, "error": str(exc)},
            )
            await self._redis_ops.safe_delete(
                self._game_key(chat_id), log_extra=extra
            )
            validation = ValidationResult(
                is_valid=False,
                issues=[ValidationIssue.CORRUPTED_JSON],
                recoverable=False,
                recovery_action="delete_and_recreate",
            )
            return None, validation

        self._rehydrate_wallets(game)

        validation_result: Optional[ValidationResult] = None
        if validate and self._state_validator is not None:
            validation_result = self._state_validator.validate_game(game)
            if not validation_result.is_valid:
                issues = [issue.value for issue in validation_result.issues]
                self._logger.warning(
                    "Loaded game has validation issues",
                    extra={
                        **extra,
                        "issues": issues,
                        "recoverable": validation_result.recoverable,
                    },
                )

                if not validation_result.recoverable:
                    await self._redis_ops.safe_delete(
                        self._game_key(chat_id), log_extra=extra
                    )
                    return None, validation_result

                game = self._state_validator.recover_game(game, validation_result)
                try:
                    await self._save(chat_id, game)
                except Exception:
                    self._logger.exception(
                        "Failed to persist recovered game", extra=extra
                    )
                else:
                    self._logger.warning(
                        "Recovered game by resetting to waiting",
                        extra={**extra, "issues": issues},
                    )

        return game, validation_result

    async def load_game_with_version(
        self, chat_id: ChatId, *, validate: bool = True
    ) -> Tuple[Optional[Game], int]:
        """Return the persisted game and its optimistic locking version."""

        game, _ = await self.load_game(chat_id, validate=validate)
        version_key = self._version_key(chat_id)
        version_raw = await self._redis.get(version_key)

        if version_raw is None:
            await self._redis.set(version_key, 0)
            version = 0
        else:
            if isinstance(version_raw, bytes):
                version_str = version_raw.decode("utf-8", "ignore")
            else:
                version_str = str(version_raw)
            try:
                version = int(version_str)
            except (TypeError, ValueError):
                version = 0
                await self._redis.set(version_key, version)

        return game, version

    async def save_game_with_version_check(
        self,
        chat_id: ChatId,
        game: Game,
        expected_version: int,
    ) -> bool:
        """Persist ``game`` if the Redis version matches ``expected_version``."""

        game_key = self._game_key(chat_id)
        version_key = self._version_key(chat_id)

        try:
            data = pickle.dumps(game)
        except Exception:  # noqa: BLE001 - mirror _save diagnostic context
            self._logger.exception("Failed to serialise game before saving", extra={"chat_id": chat_id})
            raise

        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(version_key)
                current_version_raw = await pipe.get(version_key)
                if current_version_raw is None:
                    current_version = 0
                else:
                    if isinstance(current_version_raw, bytes):
                        current_version_str = current_version_raw.decode("utf-8", "ignore")
                    else:
                        current_version_str = str(current_version_raw)
                    try:
                        current_version = int(current_version_str)
                    except (TypeError, ValueError):
                        current_version = 0

                if current_version != expected_version:
                    await pipe.unwatch()
                    self._logger.warning(
                        "Version conflict detected during save",
                        extra={
                            "chat_id": chat_id,
                            "expected_version": expected_version,
                            "current_version": current_version,
                        },
                    )
                    return False

                new_version = current_version + 1

                pipe.multi()
                pipe.set(game_key, data)
                pipe.set(version_key, new_version)
                await pipe.execute()

        except redis_exceptions.WatchError:
            self._logger.warning(
                "Concurrent modification detected (WatchError)",
                extra={"chat_id": chat_id, "expected_version": expected_version},
            )
            return False
        except redis_exceptions.RedisError as exc:
            self._logger.error(
                "Redis error during save_game_with_version_check",
                extra={
                    "chat_id": chat_id,
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                },
                exc_info=True,
            )
            raise

        await self._update_player_index(chat_id, game)
        self._tables[chat_id] = game
        self._logger.debug(
            "Game saved with version check",
            extra={
                "chat_id": chat_id,
                "old_version": expected_version,
                "new_version": new_version,
            },
        )
        return True

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
        # Use correct logger attribute from RedisSafeOps
        self._redis_ops._logger.debug(
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
            await self._redis.set(self._version_key(chat_id), 0, nx=True)
            await self._update_player_index(chat_id, game)
            self._redis_ops._logger.debug(
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

    def _rehydrate_wallets(self, game: Game) -> None:
        if self._wallet_redis is None:
            return

        from pokerapp.pokerbotmodel import WalletManagerModel

        for player in game.players:
            info = getattr(player, "_wallet_info", None) or {}
            user_id = info.get("user_id") or getattr(player, "user_id", None)
            if user_id is None:
                continue
            player.wallet = WalletManagerModel(user_id, self._wallet_redis)
            if hasattr(player, "_wallet_info"):
                delattr(player, "_wallet_info")

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
