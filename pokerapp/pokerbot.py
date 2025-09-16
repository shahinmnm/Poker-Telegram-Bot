#!/usr/bin/env python3

import logging
from dataclasses import dataclass
from functools import wraps
from typing import Optional, Sequence, TYPE_CHECKING

import redis
import redis.asyncio as aioredis
from telegram.ext import ApplicationBuilder, ContextTypes

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
        builder = (
            ApplicationBuilder()
            .token(token)
            .job_queue(True)
            .post_init(self._apply_webhook_settings)
            .post_stop(self._cleanup_webhook)
        )
        self._application = builder.build()
        self._application.add_error_handler(self._handle_error)

        # Separate Redis clients for synchronous wallet operations and
        # asynchronous table persistence
        kv_sync = redis.Redis(
            host=cfg.REDIS_HOST,
            port=cfg.REDIS_PORT,
            db=cfg.REDIS_DB,
            password=cfg.REDIS_PASS if cfg.REDIS_PASS != "" else None,
        )
        kv_async = aioredis.Redis(
            host=cfg.REDIS_HOST,
            port=cfg.REDIS_PORT,
            db=cfg.REDIS_DB,
            password=cfg.REDIS_PASS if cfg.REDIS_PASS != "" else None,
        )

        table_manager = TableManager(kv_async, kv_sync)
        view = PokerBotViewer(bot=self._application.bot, admin_chat_id=cfg.ADMIN_CHAT_ID)
        model = PokerBotModel(
            view=view,
            bot=self._application.bot,
            kv=kv_sync,
            cfg=cfg,
            table_manager=table_manager,
        )
        self._controller = PokerBotCotroller(model, self._application)

    def run(self) -> None:
        """Start the bot."""
        logger.info(
            "Starting webhook listener on %s:%s%s targeting %s",
            self._cfg.WEBHOOK_LISTEN,
            self._cfg.WEBHOOK_PORT,
            self._cfg.WEBHOOK_PATH,
            self._cfg.WEBHOOK_PUBLIC_URL,
        )
        try:
            self._application.run_webhook(
                listen=self._cfg.WEBHOOK_LISTEN,
                port=self._cfg.WEBHOOK_PORT,
                url_path=self._cfg.WEBHOOK_PATH,
                webhook_url=self._cfg.WEBHOOK_PUBLIC_URL,
            )
        except Exception:
            logger.exception("Webhook run terminated due to an error.")
            raise
        finally:
            logger.info("Webhook listener stopped.")

    async def _apply_webhook_settings(self, application: "Application") -> None:
        application.bot_data["webhook_settings"] = self._webhook_settings

        updater = getattr(application, "updater", None)
        if updater is None:
            logger.warning("Application updater is not available; webhook settings not applied.")
            return

        original_start_webhook = getattr(updater, "start_webhook", None)
        if original_start_webhook is None:
            logger.warning("Updater.start_webhook is not available; webhook settings not applied.")
            return

        if getattr(original_start_webhook, "__pokerbot_wrapped__", False):
            logger.debug("Webhook settings already applied to updater.")
            return

        @wraps(original_start_webhook)
        async def start_webhook_with_settings(*args, **kwargs):
            settings = self._webhook_settings
            if settings.secret_token:
                kwargs.setdefault("secret_token", settings.secret_token)
            if settings.allowed_updates is not None:
                kwargs.setdefault("allowed_updates", settings.allowed_updates)
            if settings.drop_pending_updates is not None:
                kwargs.setdefault("drop_pending_updates", settings.drop_pending_updates)
            if settings.max_connections is not None:
                kwargs.setdefault("max_connections", settings.max_connections)

            webhook_url = kwargs.get("webhook_url") or self._cfg.WEBHOOK_PUBLIC_URL
            if webhook_url:
                logger.info("Registering webhook with Telegram at %s", webhook_url)
            else:
                logger.info(
                    "Registering webhook listener on %s:%s%s",
                    self._cfg.WEBHOOK_LISTEN,
                    self._cfg.WEBHOOK_PORT,
                    self._cfg.WEBHOOK_PATH,
                )

            try:
                result = await original_start_webhook(*args, **kwargs)
            except Exception:
                logger.exception("Failed to register webhook with Telegram.")
                raise

            try:
                webhook_info = await application.bot.get_webhook_info()
            except Exception:
                logger.exception("Unable to confirm webhook registration with Telegram.")
            else:
                if webhook_info.url:
                    logger.info("Webhook registered at %s", webhook_info.url)
                else:
                    logger.warning("Webhook registration returned an empty URL.")

            return result

        start_webhook_with_settings.__pokerbot_wrapped__ = True
        updater.start_webhook = start_webhook_with_settings
        logger.debug("Webhook settings applied: %s", self._webhook_settings)

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
