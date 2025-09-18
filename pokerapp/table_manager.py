import pickle
from typing import Dict, Optional

import redis.asyncio as aioredis

from pokerapp.entities import Game, ChatId


class TableManager:
    """Manage a single poker game per chat and persist it in Redis."""

    def __init__(self, redis: aioredis.Redis, wallet_redis: Optional[aioredis.Redis] = None):
        self._redis = redis
        self._wallet_redis = wallet_redis or redis
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
        if chat_id in self._tables:
            return self._tables[chat_id]

        data = await self._redis.get(self._game_key(chat_id))
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

        chat_id_data = await self._redis.get(self._player_chat_key(str(user_id)))
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
        await self._redis.set(self._game_key(chat_id), pickle.dumps(game))
        await self._update_player_index(chat_id, game)

    async def _update_player_index(self, chat_id: ChatId, game: Game) -> None:
        players = {str(player.user_id) for player in game.players}
        players_key = self._chat_players_key(chat_id)

        previous_players_raw = await self._redis.smembers(players_key)
        previous_players = {
            member.decode() if isinstance(member, bytes) else str(member)
            for member in previous_players_raw
        }

        stale_players = previous_players - players
        if stale_players:
            stale_keys = [self._player_chat_key(player_id) for player_id in stale_players]
            await self._redis.delete(*stale_keys)

        if players:
            mapping = {
                self._player_chat_key(player_id): str(chat_id)
                for player_id in players
            }
            await self._redis.mset(mapping)

        await self._redis.delete(players_key)
        if players:
            await self._redis.sadd(players_key, *players)
