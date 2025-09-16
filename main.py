#!/usr/bin/env python3

from dotenv import load_dotenv

from pokerapp.config import Config
from pokerapp.pokerbot import PokerBot
import logging
from pokerapp.logging_config import setup_logging

setup_logging(logging.DEBUG)
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    cfg: Config = Config()

    missing_settings = []

    if cfg.TOKEN == "":
        missing_settings.append(
            (
                "MissingToken",
                "Environment variable POKERBOT_TOKEN is not set. "
                "Add it to your .env file or container environment.",
            )
        )

    if not cfg.WEBHOOK_PATH:
        missing_settings.append(
            (
                "MissingWebhookPath",
                "Webhook path is not configured. Set POKERBOT_WEBHOOK_PATH in your "
                ".env file or container environment.",
            )
        )

    if not cfg.WEBHOOK_PUBLIC_URL:
        missing_settings.append(
            (
                "MissingWebhookPublicUrl",
                "Webhook public URL is not configured. Set POKERBOT_WEBHOOK_DOMAIN (recommended) "
                "together with POKERBOT_WEBHOOK_PATH, or provide POKERBOT_WEBHOOK_PUBLIC_URL in "
                "your .env file or container environment.",
            )
        )

    if not cfg.WEBHOOK_SECRET:
        missing_settings.append(
            (
                "MissingWebhookSecret",
                "Webhook secret token is not configured. Set POKERBOT_WEBHOOK_SECRET in your "
                ".env file or container environment.",
            )
        )

    if missing_settings:
        for error_type, message in missing_settings:
            logger.error(message, extra={"error_type": error_type})
        exit(1)

    bot = PokerBot(token=cfg.TOKEN, cfg=cfg)
    bot.run()


if __name__ == "__main__":
    main()
