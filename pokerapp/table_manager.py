import pickle
from collections import defaultdict
from typing import Dict, List

import redis.asyncio as aioredis

from pokerapp.entities import Game, ChatId


class TableManager:
    """Manage multiple game tables per chat and persist them in Redis."""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
        self._tables: Dict[ChatId, Dict[int, Game]] = defaultdict(dict)

    # Keys ---------------------------------------------------------------
    @staticmethod
    def _tables_key(chat_id: ChatId) -> str:
        return f"chat:{chat_id}:tables"

    @staticmethod
    def _table_key(chat_id: ChatId, table_id: int) -> str:
        return f"chat:{chat_id}:table:{table_id}"

    # Public API ---------------------------------------------------------
    async def new_table(self, chat_id: ChatId) -> int:
        """Create a new table for chat and persist it."""
        existing = await self.list_tables(chat_id)
        new_id = max(existing, default=0) + 1
        game = Game()
        self._tables[chat_id][new_id] = game
        await self._save(chat_id, new_id, game)
        return new_id

    async def get_game(self, chat_id: ChatId, table_id: int) -> Game:
        """Load a game for chat/table, creating if missing."""
        tables = self._tables[chat_id]
        if table_id in tables:
            return tables[table_id]
        data = await self._redis.get(self._table_key(chat_id, table_id))
        if data:
            game = pickle.loads(data)
        else:
            game = Game()
            await self._save(chat_id, table_id, game)
        tables[table_id] = game
        return game

    async def list_tables(self, chat_id: ChatId) -> List[int]:
        ids = await self._redis.smembers(self._tables_key(chat_id))
        return sorted(int(x) for x in ids) if ids else []

    async def save_game(self, chat_id: ChatId, table_id: int, game: Game) -> None:
        self._tables[chat_id][table_id] = game
        await self._save(chat_id, table_id, game)

    # Internal -----------------------------------------------------------
    async def _save(self, chat_id: ChatId, table_id: int, game: Game) -> None:
        await self._redis.sadd(self._tables_key(chat_id), table_id)
        await self._redis.set(self._table_key(chat_id, table_id), pickle.dumps(game))
