# pokerapp/message_delete_manager.py
#!/usr/bin/env python3
import time
from typing import Dict, Optional, Set, Tuple
import threading
import logging
from telegram import Bot
from telegram.error import BadRequest, Unauthorized

GameId = int
ChatId = int
MessageId = int
ContextKey = Tuple[Optional[GameId], Optional[int]]
MessageKey = Tuple[ChatId, MessageId]

class MessageDeleteManager:
    def __init__(self, bot, job_queue=None, default_ttl=None, delete_delay: float = 0.2) -> None:
        self._bot = bot
        self._job_queue = job_queue
        self._default_ttl = default_ttl
        self._delete_delay = delete_delay
        self._lock = threading.RLock()
        self._meta = {}
        self._by_ctx = {}
        self._by_tag = {}
        self._by_chat = {}

def register(self, *, chat_id, message_id, game_id=None, hand_id=None, tag="generic", protected=False, ttl=None):
    key = (chat_id, message_id)
    with self._lock:
        self._meta[key] = {"game_id": game_id, "hand_id": hand_id, "tag": tag, "protected": protected, "ttl": ttl}
        ctx = (game_id, hand_id)
        self._by_ctx.setdefault(ctx, set()).add((chat_id, message_id))
        self._by_tag.setdefault((game_id, hand_id, tag), set()).add((chat_id, message_id))
        self._by_chat.setdefault(chat_id, set()).add((chat_id, message_id))

    def delete(self, chat_id: ChatId, message_id: MessageId, reason: str = "manual") -> bool:
        """حذف تکی پیام و پاک‌سازی رجیستری‌ها."""
        mk: MessageKey = (chat_id, message_id)
        with self._lock:
            meta = self._meta.get(mk)

        ok = self._try_delete_telegram(chat_id, message_id, reason=reason)

        if meta:
            with self._lock:
                self._meta.pop(mk, None)
                ctx = meta["ctx"]
                tag = meta["tag"]
                s = self._by_ctx.get(ctx)
                if s:
                    s.discard(mk)
                    if not s:
                        self._by_ctx.pop(ctx, None)
                st = self._by_tag.get((ctx[0], ctx[1], tag))
                if st:
                    st.discard(mk)
                    if not st:
                        self._by_tag.pop((ctx[0], ctx[1], tag), None)
        return ok

    def delete_by_tag(self, *, game_id, hand_id, tag, include_protected=False, reason="by_tag") -> int:
        key = (game_id, hand_id, tag)
        with self._lock:
            targets = list(self._by_tag.get(key, set()))
        cnt = 0
        for chat_id, message_id in targets:
            with self._lock:
                meta = self._meta.get((chat_id, message_id))
                if not meta:
                    continue
                if meta.get("protected") and not include_protected:
                    continue
            if self.delete(chat_id, message_id, reason=reason):
                cnt += 1
            if self._delete_delay:
                time.sleep(self._delete_delay)
        return cnt
    
    def purge_context(self, *, game_id, hand_id, include_protected=False, reason="purge_ctx") -> int:
        ctx = (game_id, hand_id)
        with self._lock:
            targets = list(self._by_ctx.get(ctx, set()))
        cnt = 0
        for chat_id, message_id in targets:
            with self._lock:
                meta = self._meta.get((chat_id, message_id))
                if not meta:
                    continue
                if meta.get("protected") and not include_protected:
                    continue
            if self.delete(chat_id, message_id, reason=reason):
                cnt += 1
            if self._delete_delay:
                time.sleep(self._delete_delay)
        return cnt
    
    def purge_chat(self, *, chat_id, include_protected=False, reason="purge_chat") -> int:
        with self._lock:
            targets = list(self._by_chat.get(chat_id, set()))
        cnt = 0
        for c_id, message_id in targets:
            with self._lock:
                meta = self._meta.get((c_id, message_id))
                if not meta:
                    continue
                if meta.get("protected") and not include_protected:
                    continue
            if self.delete(c_id, message_id, reason=reason):
                cnt += 1
            if self._delete_delay:
                time.sleep(self._delete_delay)
        return cnt


    def _job_delete_job(self, context) -> None:
        job = getattr(context, "job", None)
        data = job.context if job else context
        chat_id = data.get("chat_id")
        message_id = data.get("message_id")
        if chat_id is None or message_id is None:
            return
        self.delete(chat_id, message_id, reason="ttl")

    # ---------- تلگرام + لاگ ----------

    def _try_delete_telegram(self, chat_id: ChatId, message_id: MessageId, reason: str) -> bool:
        try:
            self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            logging.debug("Deleted message %s in chat %s (%s)", message_id, chat_id, reason)
            return True
        except BadRequest as e:
            # نمونه پیام‌ها: "message to delete not found", "message can't be deleted"
            logging.info("Skip deleting %s in %s: %s (%s)", message_id, chat_id, e, reason)
            return False
        except Unauthorized as e:
            logging.info("Unauthorized to delete %s in %s: %s (%s)", message_id, chat_id, e, reason)
            return False
        except Exception as e:
            logging.error("Unexpected delete error %s in %s: %s (%s)", message_id, chat_id, e, reason)
            return False

    # ---------- دیباگ ----------

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                "messages_tracked": len(self._meta),
                "contexts": {str(k): len(v) for k, v in self._by_ctx.items()},
                "tags": {str(k): len(v) for k, v in self._by_tag.items()},
            }
