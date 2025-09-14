from collections import defaultdict, deque
import asyncio
import time
from typing import Optional, Deque, Dict, Any, Iterable

class MessageDeleteManager:
    def __init__(self, max_per_chat: int = 500):
        self._bot = None
        self._max_per_chat = max_per_chat
        self._store: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)

    def set_bot(self, bot):
        self._bot = bot

    def register(
        self,
        chat_id: int,
        message_id: int,
        game_id: Optional[int] = None,
        hand_id: Optional[int] = None,
        tag: Optional[str] = None,
        protected: bool = False,
        ttl: Optional[int] = None,
    ):
        now = int(time.time())
        exp = now + ttl if ttl else None
        q = self._store[chat_id]
        q.append(
            {
                "message_id": message_id,
                "game_id": game_id,
                "hand_id": hand_id,
                "tag": tag,
                "protected": protected,
                "expires_at": exp,
                "ts": now,
            }
        )
        while len(q) > self._max_per_chat:
            q.popleft()

    async def delete_messages_sequential(
        self,
        chat_id: int,
        message_ids: Iterable[int],
        delay: float = 0.3,
    ) -> None:
        """Delete messages one by one with a delay between each call."""
        if self._bot is None:
            return
        for mid in message_ids:
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
            await asyncio.sleep(delay)

    def purge_context(
        self,
        chat_id: Optional[int] = None,
        game_id: Optional[int] = None,
        hand_id: Optional[int] = None,
        include_protected: bool = False,
        reason: str = "",
    ):
        if self._bot is None:
            return
        chats = [chat_id] if chat_id is not None else list(self._store.keys())
        for cid in chats:
            if cid not in self._store:
                continue
            kept: Deque[Dict[str, Any]] = deque()
            for item in self._store[cid]:
                if not include_protected and item.get("protected"):
                    kept.append(item)
                    continue
                if game_id is not None and item.get("game_id") != game_id:
                    kept.append(item)
                    continue
                if hand_id is not None and item.get("hand_id") != hand_id:
                    kept.append(item)
                    continue
                try:
                    self._bot.delete_message(cid, item["message_id"])
                except Exception:
                    pass
            self._store[cid] = kept

    def purge_by_tag(
        self, chat_id: int, tag: str, include_protected: bool = False
    ):
        if self._bot is None or chat_id not in self._store:
            return
        kept: Deque[Dict[str, Any]] = deque()
        for item in self._store[chat_id]:
            if item.get("tag") != tag:
                kept.append(item)
                continue
            if not include_protected and item.get("protected"):
                kept.append(item)
                continue
            try:
                self._bot.delete_message(chat_id, item["message_id"])
            except Exception:
                pass
        self._store[chat_id] = kept

    def purge_expired(self, chat_id: Optional[int] = None):
        now = int(time.time())
        chats = [chat_id] if chat_id is not None else list(self._store.keys())
        for cid in chats:
            if cid not in self._store:
                continue
            kept: Deque[Dict[str, Any]] = deque()
            for item in self._store[cid]:
                exp = item.get("expires_at")
                if exp and exp <= now:
                    try:
                        self._bot.delete_message(cid, item["message_id"])
                    except Exception:
                        pass
                else:
                    kept.append(item)
            self._store[cid] = kept
