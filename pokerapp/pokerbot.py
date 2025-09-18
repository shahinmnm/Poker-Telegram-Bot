#!/usr/bin/env python3
import logging
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

import redis.asyncio as aioredis
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, JobQueue

from pokerapp.config import Config
from pokerapp.pokerbotcontrol import PokerBotCotroller
from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.table_manager import TableManager
from pokerapp.logging_config import setup_logging

setup_logging(logging.INFO)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookSettings:
    secret_token: Optional[str]
    max_connections: Optional[int]
    allowed_updates: Optional[Sequence[str]]
    drop_pending_updates: bool = True


if TYPE_CHECKING:
    from telegram.ext import Application


class PokerBot:
    """Telegram bot wrapper using PTB v20 async Application."""

    def __init__(self, token: str, cfg: Config):
        self._cfg = cfg
        self._webhook_settings = WebhookSettings(
            secret_token=cfg.WEBHOOK_SECRET or None,
            max_connections=getattr(
                cfg,
                "WEBHOOK_MAX_CONNECTIONS",
                getattr(cfg, "MAX_CONNECTIONS", None),
            ),
            allowed_updates=cfg.ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        self._job_queue = JobQueue()

        builder = (
            ApplicationBuilder()
            .token(token)
            .post_stop(self._cleanup_webhook)
            .job_queue(self._job_queue)
        )
        self._application = builder.build()
        self._application.add_error_handler(self._handle_error)

        # Shared async Redis client for both wallet operations and
        # asynchronous table persistence
        kv_async = aioredis.Redis(
            host=cfg.REDIS_HOST,
            port=cfg.REDIS_PORT,
            db=cfg.REDIS_DB,
            password=cfg.REDIS_PASS if cfg.REDIS_PASS != "" else None,
        )

        table_manager = TableManager(kv_async)
        view = PokerBotViewer(
            bot=self._application.bot,
            admin_chat_id=cfg.ADMIN_CHAT_ID,
            rate_limit_per_minute=cfg.RATE_LIMIT_PER_MINUTE,
        )
        model = PokerBotModel(
            view=view,
            bot=self._application.bot,
            kv=kv_async,
            cfg=cfg,
            table_manager=table_manager,
        )
        self._controller = PokerBotCotroller(model, self._application)

    def run(self) -> None:
        """Start the bot using the webhook listener."""
        try:
            self.run_webhook()
        except (TelegramError, OSError) as exc:
            if not self._handle_webhook_start_failure(exc):
                raise
        except Exception as exc:
            if not self._handle_webhook_start_failure(exc):
                raise

    def run_webhook(self) -> None:
        """Start the bot using webhook delivery."""
        logger.info(
            "Starting webhook listener on %s:%s%s targeting %s",
            self._cfg.WEBHOOK_LISTEN,
            self._cfg.WEBHOOK_PORT,
            self._cfg.WEBHOOK_PATH,
            self._cfg.WEBHOOK_PUBLIC_URL,
        )
        self._schedule_webhook_verification()
        settings = self._webhook_settings
        try:
            self._application.run_webhook(
                listen=self._cfg.WEBHOOK_LISTEN,
                port=self._cfg.WEBHOOK_PORT,
                url_path=self._cfg.WEBHOOK_PATH,
                webhook_url=self._cfg.WEBHOOK_PUBLIC_URL,
                secret_token=settings.secret_token,
                allowed_updates=settings.allowed_updates,
                drop_pending_updates=settings.drop_pending_updates,
                max_connections=settings.max_connections,
            )
        except Exception:
            logger.exception("Webhook run terminated due to an error.")
            raise
        finally:
            logger.info("Webhook listener stopped.")

    def run_polling(self) -> None:
        """Start the bot using long polling."""
        logger.info(
            "Starting polling mode for development; webhook configuration will be ignored."
        )
        try:
            self._application.run_polling(
                allowed_updates=self._webhook_settings.allowed_updates,
                drop_pending_updates=self._webhook_settings.drop_pending_updates,
            )
        except Exception:
            logger.exception("Polling run terminated due to an error.")
            raise
        finally:
            logger.info("Polling stopped.")

    def _handle_webhook_start_failure(self, exc: Exception) -> bool:
        """Handle failures when starting the webhook listener.

        Returns True when the failure was handled (e.g., by falling back to
        polling) and False when the caller should re-raise the exception.
        """

        if getattr(self._cfg, "ALLOW_POLLING_FALLBACK", False):
            logger.error(
                "Webhook startup failed; falling back to polling mode because "
                "ALLOW_POLLING_FALLBACK is enabled. Error: %s",
                exc,
            )
            logger.warning("Using polling mode as a fallback.")
            self.run_polling()
            return True

        return False

    def _schedule_webhook_verification(self) -> None:
        try:
            self._application.job_queue.run_once(
                self._webhook_verification_job,
                when=1.0,
                name="webhook-verification",
            )
        except Exception:
            logger.exception("Unable to schedule webhook verification job.")

    async def _webhook_verification_job(
        self, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._verify_webhook_registration()

    async def _verify_webhook_registration(self) -> None:
        try:
            webhook_info = await self._application.bot.get_webhook_info()
        except Exception:
            logger.exception("Unable to confirm webhook registration with Telegram.")
            return

        expected_url = self._cfg.WEBHOOK_PUBLIC_URL or ""
        if webhook_info.url == expected_url:
            logger.info("Webhook registered at expected URL %s", webhook_info.url)
        else:
            logger.warning(
                "Webhook URL mismatch: expected %s, got %s",
                expected_url,
                webhook_info.url,
            )

        settings = self._webhook_settings
        expected_secret = settings.secret_token or ""
        registered_secret = getattr(webhook_info, "secret_token", None)
        if expected_secret and not registered_secret:
            logger.info(
                "Webhook secret token not returned by Telegram; configured secret not verifiable."
            )
        elif expected_secret and registered_secret == expected_secret:
            logger.info("Webhook secret token matches configured value.")
        elif expected_secret and registered_secret != expected_secret:
            logger.warning("Webhook secret token does not match the configured value.")
        elif not expected_secret and registered_secret:
            logger.warning("Webhook secret token set unexpectedly.")
        else:
            logger.info("Webhook secret token is not configured, as expected.")

        registered_allowed_updates = (
            tuple(webhook_info.allowed_updates)
            if getattr(webhook_info, "allowed_updates", None)
            else ()
        )
        expected_allowed_updates = tuple(settings.allowed_updates or ())
        if expected_allowed_updates == registered_allowed_updates:
            logger.info("Webhook allowed updates match configured values: %s", registered_allowed_updates)
        else:
            logger.warning(
                "Webhook allowed updates mismatch: expected %s, got %s",
                expected_allowed_updates,
                registered_allowed_updates,
            )

        registered_max_connections = getattr(webhook_info, "max_connections", None)
        if settings.max_connections is None:
            logger.info(
                "Webhook max_connections not configured; accepting Telegram-reported value: %s",
                registered_max_connections,
            )
        elif registered_max_connections is None:
            logger.info(
                "Webhook max_connections not reported by Telegram; configured value is %s",
                settings.max_connections,
            )
        elif settings.max_connections == registered_max_connections:
            logger.info(
                "Webhook max_connections matches configured value: %s",
                registered_max_connections,
            )
        else:
            logger.warning(
                "Webhook max_connections mismatch: expected %s, got %s",
                settings.max_connections,
                registered_max_connections,
            )

        logger.info(
            "Webhook configured to drop pending updates: %s",
            settings.drop_pending_updates,
        )

    async def _cleanup_webhook(self, application: "Application") -> None:
        drop_updates = bool(self._webhook_settings.drop_pending_updates)
        logger.info("Removing webhook; drop_pending_updates=%s", drop_updates)
        try:
            await application.bot.delete_webhook(drop_pending_updates=drop_updates)
        except Exception:
            logger.exception("Failed to delete webhook during shutdown.")
            return

        try:
            webhook_info = await application.bot.get_webhook_info()
        except Exception:
            logger.exception("Unable to verify webhook removal.")
            return

        if webhook_info.url:
            logger.warning(
                "Webhook still registered at %s after deletion attempt.",
                webhook_info.url,
            )
        else:
            logger.info("Webhook successfully removed from Telegram.")

    async def _handle_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        error = getattr(context, "error", None)
        if isinstance(error, BaseException):
            logger.error(
                "Error while processing update %s", getattr(update, "update_id", update),
                exc_info=error,
            )
        else:
            logger.error(
                "Error while processing update %s with payload %s",
                getattr(update, "update_id", update),
                error,
            )
