import logging
import os
from typing import List, Optional, Tuple
from urllib.parse import urljoin


logger = logging.getLogger(__name__)


DEFAULT_WEBHOOK_LISTEN = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 3000
DEFAULT_WEBHOOK_PATH = "/telegram/webhook-poker2025"


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
        allow_polling_raw = os.getenv("POKERBOT_ALLOW_POLLING_FALLBACK")
        self.ALLOW_POLLING_FALLBACK: bool = (
            allow_polling_raw is not None
            and allow_polling_raw.strip().lower() in {"1", "true", "yes", "on"}
        )
        admin_chat_id = os.getenv("POKERBOT_ADMIN_CHAT_ID", "")
        self.ADMIN_CHAT_ID = int(admin_chat_id) if admin_chat_id else None
        self.WEBHOOK_LISTEN: str = (
            os.getenv("POKERBOT_WEBHOOK_LISTEN", DEFAULT_WEBHOOK_LISTEN).strip()
            or DEFAULT_WEBHOOK_LISTEN
        )
        self.WEBHOOK_PORT: int = self._parse_int_env(
            os.getenv("POKERBOT_WEBHOOK_PORT"),
            default=DEFAULT_WEBHOOK_PORT,
            env_var="POKERBOT_WEBHOOK_PORT",
        )
        webhook_path_env = os.getenv("POKERBOT_WEBHOOK_PATH")
        raw_webhook_path = (
            webhook_path_env.strip()
            if webhook_path_env is not None
            else DEFAULT_WEBHOOK_PATH
        )
        self.WEBHOOK_PATH: str = self._normalize_webhook_path(raw_webhook_path)
        raw_webhook_domain = os.getenv("POKERBOT_WEBHOOK_DOMAIN", "")
        self.WEBHOOK_DOMAIN: str = self._normalize_webhook_domain(raw_webhook_domain)
        explicit_public_url = os.getenv(
            "POKERBOT_WEBHOOK_PUBLIC_URL",
            default="",
        )
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
        allowed_updates_raw, _allowed_updates_source = self._get_first_nonempty_env(
            "POKERBOT_WEBHOOK_ALLOWED_UPDATES",
            "POKERBOT_ALLOWED_UPDATES",
        )
        self.ALLOWED_UPDATES: Optional[List[str]] = self._parse_allowed_updates(
            allowed_updates_raw
        )
        max_connections_raw, max_connections_source = self._get_first_nonempty_env(
            "POKERBOT_WEBHOOK_MAX_CONNECTIONS",
            "POKERBOT_MAX_CONNECTIONS",
        )
        self.MAX_CONNECTIONS: Optional[int] = self._parse_positive_int(
            max_connections_raw,
            env_var=max_connections_source,
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
        explicit_public_url = explicit_public_url.strip()
        if explicit_public_url:
            logger.debug(
                "Using explicit WEBHOOK_PUBLIC_URL provided via POKERBOT_WEBHOOK_PUBLIC_URL."
            )
            return explicit_public_url

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

        return ""

    @staticmethod
    def _get_first_nonempty_env(*keys: str) -> Tuple[Optional[str], Optional[str]]:
        for key in keys:
            value = os.getenv(key)
            if value is None:
                continue
            stripped = value.strip()
            if stripped:
                return stripped, key
        return None, None

    @staticmethod
    def _parse_allowed_updates(raw_value: Optional[str]) -> Optional[List[str]]:
        if not raw_value:
            return None
        updates = [
            update.strip()
            for update in raw_value.split(",")
            if update.strip()
        ]
        return updates or None

    @staticmethod
    def _parse_positive_int(
        raw_value: Optional[str], *, env_var: Optional[str]
    ) -> Optional[int]:
        if not raw_value:
            return None
        try:
            value = int(raw_value)
        except ValueError:
            if env_var:
                logger.warning(
                    "Invalid integer value '%s' for %s; ignoring it.",
                    raw_value,
                    env_var,
                )
            else:
                logger.warning(
                    "Invalid integer value '%s' provided for MAX_CONNECTIONS; ignoring it.",
                    raw_value,
                )
            return None
        if value <= 0:
            if env_var:
                logger.warning(
                    "%s must be greater than zero; ignoring %s.",
                    env_var,
                    raw_value,
                )
            else:
                logger.warning(
                    "MAX_CONNECTIONS must be greater than zero; ignoring %s.",
                    raw_value,
                )
            return None
        return value

    @staticmethod
    def _parse_int_env(
        raw_value: Optional[str], *, default: int, env_var: str
    ) -> int:
        if raw_value is None:
            return default
        raw_value = raw_value.strip()
        if not raw_value:
            return default
        try:
            return int(raw_value)
        except ValueError:
            logger.warning(
                "Invalid integer value '%s' for %s; falling back to default %s.",
                raw_value,
                env_var,
                default,
            )
            return default
