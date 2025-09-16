import logging
import os
from typing import List, Optional
from urllib.parse import urljoin


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
        default_webhook_path = "/telegram/webhook-poker2025"
        webhook_path_env = os.getenv("POKERBOT_WEBHOOK_PATH")
        raw_webhook_path = (
            webhook_path_env.strip()
            if webhook_path_env is not None
            else default_webhook_path
        )
        self.WEBHOOK_PATH: str = self._normalize_webhook_path(raw_webhook_path)
        raw_webhook_domain = os.getenv("POKERBOT_WEBHOOK_DOMAIN", "")
        self.WEBHOOK_DOMAIN: str = self._normalize_webhook_domain(raw_webhook_domain)
        explicit_public_url = os.getenv(
            "POKERBOT_WEBHOOK_PUBLIC_URL",
            default="",
        ).strip()
        self.WEBHOOK_PUBLIC_URL: str = self._build_public_url(
            explicit_public_url=explicit_public_url,
        )
        if not self.WEBHOOK_PUBLIC_URL:
            logger.warning(
                "Webhook public URL is not set; define POKERBOT_WEBHOOK_DOMAIN together with "
                "POKERBOT_WEBHOOK_PATH or provide POKERBOT_WEBHOOK_PUBLIC_URL."
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

    @staticmethod
    def _normalize_webhook_path(path: str) -> str:
        normalized_path = path.strip()
        if not normalized_path:
            return ""
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        return normalized_path

    @staticmethod
    def _normalize_webhook_domain(domain: str) -> str:
        normalized_domain = domain.strip()
        if not normalized_domain:
            return ""
        if not normalized_domain.startswith(("http://", "https://")):
            logger.debug(
                "POKERBOT_WEBHOOK_DOMAIN missing scheme; defaulting to https://%s",
                normalized_domain,
            )
            normalized_domain = f"https://{normalized_domain}"
        return normalized_domain.rstrip("/")

    def _build_public_url(self, explicit_public_url: str) -> str:
        if self.WEBHOOK_DOMAIN and self.WEBHOOK_PATH:
            combined_url = urljoin(
                f"{self.WEBHOOK_DOMAIN.rstrip('/')}/",
                self.WEBHOOK_PATH.lstrip("/"),
            )
            logger.debug(
                "Derived WEBHOOK_PUBLIC_URL from domain and path using %s and %s",
                self.WEBHOOK_DOMAIN,
                self.WEBHOOK_PATH,
            )
            return combined_url

        explicit_public_url = explicit_public_url.strip()
        if explicit_public_url:
            logger.debug(
                "Using explicit WEBHOOK_PUBLIC_URL provided via POKERBOT_WEBHOOK_PUBLIC_URL."
            )
        return explicit_public_url
