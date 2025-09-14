#!/usr/bin/env python3

import logging
import redis
import redis.asyncio as aioredis
from telegram.ext import ApplicationBuilder

from pokerapp.config import Config
from pokerapp.pokerbotcontrol import PokerBotCotroller
from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.table_manager import TableManager
from pokerapp.logging_config import setup_logging

setup_logging(logging.INFO)


class PokerBot:
    """Telegram bot wrapper using PTB v20 async Application."""

    def __init__(self, token: str, cfg: Config):
        self._application = ApplicationBuilder().token(token).build()

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
        self._application.run_polling()
