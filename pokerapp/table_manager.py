import pickle
from typing import Dict, Optional

import redis
import redis.asyncio as aioredis

from pokerapp.entities import Game, ChatId


class TableManager:
    """Manage a single poker game per chat and persist it in Redis."""

    def __init__(self, redis: aioredis.Redis, wallet_redis: Optional[redis.Redis] = None):
        self._redis = redis
        self._wallet_redis = wallet_redis
        # Keep games cached in memory keyed only by chat_id
        self._tables: Dict[ChatId, Game] = {}

    # Keys ---------------------------------------------------------------
    @staticmethod
    def _game_key(chat_id: ChatId) -> str:
        return f"chat:{chat_id}:game"

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
        raise LookupError(f"No game found for user {user_id}")

    # Internal -----------------------------------------------------------
    async def _save(self, chat_id: ChatId, game: Game) -> None:
        await self._redis.set(self._game_key(chat_id), pickle.dumps(game))
