# pokerapp/message_delete_manager.py
#!/usr/bin/env python3
"""
MessageDeleteManager: ثبت و پاک‌سازی متمرکز پیام‌ها بر اساس (game_id, hand_id, tag).
- استفاده از JobQueue برای حذف زمان‌بندی‌شده (غیربلاکینگ و قابل اطمینان).
- پیام‌های protected (مثل نتایج) با purge معمولی حذف نمی‌شوند.
"""
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
    """مدیر مرکزی حذف پیام‌ها با API ساده و ایمن."""

    def __init__(self, bot: Bot, job_queue=None, default_ttl: Optional[int] = None) -> None:
        self._bot = bot
        self._job_queue = job_queue  # telegram.ext.JobQueue (در v13)
        self._default_ttl = default_ttl
        self._lock = threading.RLock()

        # رجیستری‌ها
        # (chat_id, message_id) -> {"ctx": (game_id, hand_id), "tag": str, "protected": bool}
        self._meta: Dict[MessageKey, Dict] = {}
        # (game_id, hand_id) -> set((chat_id, message_id))
        self._by_ctx: Dict[ContextKey, Set[MessageKey]] = {}
        # (game_id, hand_id, tag) -> set((chat_id, message_id))
        self._by_tag: Dict[Tuple[Optional[GameId], Optional[int], str], Set[MessageKey]] = {}

    # ---------- ثبت ----------

    def register(
        self,
        *,
        chat_id: ChatId,
        message_id: MessageId,
        game_id: Optional[GameId] = None,
        hand_id: Optional[int] = None,
        tag: str = "generic",
        protected: bool = False,
        ttl: Optional[int] = None,
    ) -> None:
        """ثبت پیام + زمان‌بندی حذف (اختیاری)."""
        mk: MessageKey = (chat_id, message_id)
        ctx: ContextKey = (game_id, hand_id)
        with self._lock:
            self._meta[mk] = {"ctx": ctx, "tag": tag, "protected": protected}
            self._by_ctx.setdefault(ctx, set()).add(mk)
            self._by_tag.setdefault((game_id, hand_id, tag), set()).add(mk)

        # حذف خودکار در صورت وجود ttl و job_queue
        if ttl is None:
            ttl = self._default_ttl
        if ttl and self._job_queue:
            self._job_queue.run_once(
                self._job_delete_job,
                when=ttl,
                context={"chat_id": chat_id, "message_id": message_id},
                name=f"del:{chat_id}:{message_id}",
            )

    # ---------- حذف تکی/گروهی ----------

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

    def delete_by_tag(
        self,
        *,
        game_id: Optional[GameId],
        hand_id: Optional[int],
        tag: str,
        include_protected: bool = False,
        reason: str = "by_tag",
    ) -> int:
        """حذف همه‌ی پیام‌های یک تگ در (game_id, hand_id)."""
        key = (game_id, hand_id, tag)
        with self._lock:
            targets = list(self._by_tag.get(key, set()))

        deleted = 0
        for chat_id, message_id in targets:
            with self._lock:
                meta = self._meta.get((chat_id, message_id))
                if not meta:
                    continue
                if meta.get("protected") and not include_protected:
                    continue
            if self.delete(chat_id, message_id, reason=reason):
                deleted += 1
        return deleted

    def purge_context(
        self,
        *,
        game_id: Optional[GameId],
        hand_id: Optional[int],
        include_protected: bool = False,
        reason: str = "purge_ctx",
    ) -> int:
        """حذف همۀ پیام‌های وابسته به یک دست/بازی (protected را نگه می‌دارد مگر اینکه True شود)."""
        ctx: ContextKey = (game_id, hand_id)
        with self._lock:
            targets = list(self._by_ctx.get(ctx, set()))

        deleted = 0
        for chat_id, message_id in targets:
            with self._lock:
                meta = self._meta.get((chat_id, message_id))
                if not meta:
                    continue
                if meta.get("protected") and not include_protected:
                    continue
            if self.delete(chat_id, message_id, reason=reason):
                deleted += 1
        return deleted

    # ---------- JobQueue callback ----------

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
