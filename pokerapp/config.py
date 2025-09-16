import logging
import os
from typing import List, Optional


logger = logging.getLogger(__name__)


class Config:
    def __init__(self):
        self.REDIS_HOST: str = os.getenv(
            "POKERBOT_REDIS_HOST",
            default="localhost",
        )
        self.REDIS_PORT: str = int(os.getenv(
            "POKERBOT_REDIS_PORT",
            default="6379"
        ))
        self.REDIS_PASS: str = os.getenv(
            "POKERBOT_REDIS_PASS",
            default="",
        )
        self.REDIS_DB: int = int(os.getenv(
            "POKERBOT_REDIS_DB",
            default="0"
        ))
        self.TOKEN: str = os.getenv(
            "POKERBOT_TOKEN",
            default="",
        )
        self.DEBUG: bool = bool(
            os.getenv("POKERBOT_DEBUG", default="0") == "1"
        )
        admin_chat_id = os.getenv("POKERBOT_ADMIN_CHAT_ID", "")
        self.ADMIN_CHAT_ID = int(admin_chat_id) if admin_chat_id else None
        self.WEBHOOK_LISTEN: str = os.getenv(
            "POKERBOT_WEBHOOK_LISTEN",
            default="127.0.0.1",
        )
        self.WEBHOOK_PORT: int = int(
            os.getenv(
                "POKERBOT_WEBHOOK_PORT",
                default="3000",
            )
        )
        self.WEBHOOK_PATH: str = os.getenv(
            "POKERBOT_WEBHOOK_PATH",
            default="/telegram/webhook-poker2025",
        )
        self.WEBHOOK_PUBLIC_URL: str = os.getenv(
            "POKERBOT_WEBHOOK_PUBLIC_URL",
            default="",
        )
        if not self.WEBHOOK_PUBLIC_URL:
            logger.warning(
                "POKERBOT_WEBHOOK_PUBLIC_URL is not set; webhook may not be accessible externally."
            )
        self.WEBHOOK_SECRET: str = os.getenv(
            "POKERBOT_WEBHOOK_SECRET",
            default="",
        )
        allowed_updates_raw = os.getenv("POKERBOT_ALLOWED_UPDATES", "").strip()
        self.ALLOWED_UPDATES: Optional[List[str]] = (
            [
                update.strip()
                for update in allowed_updates_raw.split(",")
                if update.strip()
            ]
            if allowed_updates_raw
            else None
        )
        max_connections = os.getenv("POKERBOT_MAX_CONNECTIONS", "").strip()
        self.MAX_CONNECTIONS: Optional[int] = (
            int(max_connections) if max_connections else None
        )
