#!/usr/bin/env python3
import threading
import time
import logging
from typing import Dict, List, Set, Tuple, Optional
from telegram.error import BadRequest, Unauthorized
from pokerapp.entities import ChatId, MessageId, GameId  # فرض بر اینکه GameId در entities تعریف شده باشد

class MessageDeleteManager:
    """
    کلاس مرکزی برای مدیریت و حذف پیام‌ها با قابلیت فازبندی، 
    برچسب‌گذاری و حذف امن.
    """

    def __init__(self, bot):
        self._bot = bot
        self._store: Dict[Tuple[GameId, GameId], List[Tuple[ChatId, MessageId, str]]] = {}
        self._whitelist_tags: Set[str] = set()
        self._lock = threading.Lock()

    def whitelist_tag(self, tag: str) -> None:
        """tag‌ پیام‌هایی که نباید حذف شوند را whitelisted می‌کند."""
        with self._lock:
            self._whitelist_tags.add(tag)

    def add_message(
        self,
        game_id: GameId,
        hand_id: GameId,
        chat_id: ChatId,
        msg_id: MessageId,
        tag: Optional[str] = None
    ) -> None:
        """ثبت پیام جدید برای دست خاص."""
        key = (game_id, hand_id)
        with self._lock:
            if key not in self._store:
                self._store[key] = []
            self._store[key].append((chat_id, msg_id, tag or ""))

    def delete_all_for_hand(
        self,
        game_id: GameId,
        hand_id: GameId,
        delay: float = 0.0
    ) -> None:
        """
        حذف همه پیام‌های مربوط به یک دست خاص، 
        بجز پیام‌هایی که tag آنها در whitelist است.
        """
        key = (game_id, hand_id)
        with self._lock:
            messages = self._store.pop(key, [])

        if not messages:
            logging.debug(f"No messages stored for game={game_id}, hand={hand_id}")
            return

        # مرتب‌سازی از قدیمی به جدید
        messages.sort(key=lambda x: x[1])

        for chat_id, msg_id, tag in messages:
            if tag in self._whitelist_tags:
                continue
            # استفاده از صف Task Manager
            self._bot._add_task(
                chat_id,
                lambda cid=chat_id, mid=msg_id: self._safe_delete(cid, mid),
                "delete"
            )
            if delay:
                time.sleep(delay)

    def _safe_delete(self, chat_id: ChatId, message_id: MessageId) -> None:
        """حذف پیام با هندل خطاهای رایج."""
        try:
            self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            err = str(e).lower()
            if any(sub in err for sub in [
                "message to delete not found",
                "message can't be deleted",
                "message identifier is not specified"
            ]):
                logging.info(f"Message {message_id} in chat {chat_id} already deleted or cannot be deleted.")
            else:
                logging.warning(f"BadRequest deleting message {message_id} in chat {chat_id}: {e}")
        except Unauthorized as e:
            logging.info(f"Bot is unauthorized to delete message {message_id} in chat {chat_id}: {e}")
        except Exception as e:
            logging.error(f"Unexpected error deleting message {message_id} in chat {chat_id}: {e}")
