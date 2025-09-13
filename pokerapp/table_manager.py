import pickle
from typing import Dict

import redis.asyncio as aioredis

from pokerapp.entities import Game, ChatId


class TableManager:
    """Manage a single poker game per chat and persist it in Redis."""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
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
        else:
            game = Game()
            await self._save(chat_id, game)

        self._tables[chat_id] = game
        return game

    async def save_game(self, chat_id: ChatId, game: Game) -> None:
        self._tables[chat_id] = game
        await self._save(chat_id, game)

    # Internal -----------------------------------------------------------
    async def _save(self, chat_id: ChatId, game: Game) -> None:
        await self._redis.set(self._game_key(chat_id), pickle.dumps(game))
