from types import NoneType
from typing import Union
import redis

from pokerapp.entities import (
    ChatId,
    MessageId,
    UserId,
)

class UserPrivateChatModel:
    """
    📌 این کلاس وظیفه دارد چت خصوصی (Private Chat) کاربر 
    را در دیتابیس Redis ذخیره، بازیابی و مدیریت کند.

    استفاده اصلی:
    - ذخیره `chat_id` زمانی که کاربر در حالت چت خصوصی با ربات است.
    - نگه داشتن و مدیریت پیام‌هایی که می‌خواهیم بعداً حذف کنیم.
    """

    def __init__(self, user_id: UserId, kv: redis.Redis):
        """
        ⚙️ سازنده کلاس
        :param user_id: آیدی تلگرام کاربر
        :param kv: اتصال Redis برای ذخیره داده‌ها
        """
        self.user_id = user_id
        self._kv = kv

    @property
    def _key(self) -> str:
        """ 🔑 کلید ذخیره‌سازی منحصر به فرد برای این کاربر در Redis """
        return "pokerbot:chats:" + str(self.user_id)

    def get_chat_id(self) -> Union[ChatId, NoneType]:
        """ 📥 دریافت آیدی چت خصوصی کاربر (در صورت وجود) """
        return self._kv.get(self._key)

    def set_chat_id(self, chat_id: ChatId) -> None:
        """ 📤 ذخیره/به‌روزرسانی آیدی چت خصوصی کاربر """
        return self._kv.set(self._key, chat_id)

    def delete(self) -> None:
        """
        🗑 حذف اطلاعات چت خصوصی کاربر و لیست پیام‌های مرتبط
        (وقتی کاربر از بازی خارج می‌شود یا نیاز به پاک‌سازی داریم)
        """
        self._kv.delete(self._key + ":messages")
        return self._kv.delete(self._key)

    def pop_message(self) -> Union[MessageId, NoneType]:
        """
        📄 دریافت و حذف آخرین پیام ذخیره شده مربوط به کاربر
        (برای مواقعی که می‌خواهیم پیام‌های قبلی را پاک کنیم)
        """
        return self._kv.rpop(self._key + ":messages")

    def push_message(self, message_id: MessageId) -> None:
        """
        ➕ اضافه کردن پیام جدید برای پیگیری بعدی
        (مثلاً وقتی کارت خصوصی به کاربر ارسال می‌کنیم)
        """
        return self._kv.rpush(self._key + ":messages", message_id)
