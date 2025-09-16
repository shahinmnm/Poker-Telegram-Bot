#!/usr/bin/env python3

import logging
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

import redis
import redis.asyncio as aioredis
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
            max_connections=cfg.MAX_CONNECTIONS,
            allowed_updates=cfg.ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        builder = (
            ApplicationBuilder()
            .token(token)
            .job_queue(JobQueue())
            .post_init(self._apply_webhook_settings)
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
            max_connections = (
                self._webhook_settings.max_connections
                if self._webhook_settings.max_connections is not None
                else 40
            )
            self._application.run_webhook(
                listen=self._cfg.WEBHOOK_LISTEN,
                port=self._cfg.WEBHOOK_PORT,
                url_path=self._cfg.WEBHOOK_PATH,
                webhook_url=self._cfg.WEBHOOK_PUBLIC_URL,
                secret_token=self._webhook_settings.secret_token or None,
                max_connections=max_connections,
                allowed_updates=self._webhook_settings.allowed_updates,
                drop_pending_updates=self._webhook_settings.drop_pending_updates,
            )
        except Exception:
            logger.exception("Webhook run terminated due to an error.")
            raise
        finally:
            logger.info("Webhook listener stopped.")

    async def _apply_webhook_settings(self, application: "Application") -> None:
        application.bot_data["webhook_settings"] = self._webhook_settings
        logger.debug("Webhook settings applied: %s", self._webhook_settings)

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
