#!/usr/bin/env python3

import logging
import os
import sys

from dotenv import load_dotenv

from pokerapp.config import Config
from pokerapp.pokerbot import PokerBot
from pokerapp.logging_config import setup_logging

_DEBUG_ENV = os.getenv("POKERBOT_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
setup_logging(logging.INFO, debug_mode=_DEBUG_ENV)
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    cfg: Config = Config()
    setup_logging(logging.INFO, debug_mode=cfg.DEBUG)

    logger.info(
        "Ensure required configuration values are provided via environment or .env file.",
        extra={"category": "startup", "stage": "bootstrap"},
    )
    logger.info(
        "Set POKERBOT_ALLOW_POLLING_FALLBACK=1 to enable development polling when webhook settings are unavailable.",
        extra={"category": "startup", "stage": "bootstrap"},
    )

    missing_required_settings = []

    if cfg.TOKEN == "":
        missing_required_settings.append(
            (
                "MissingToken",
                "Environment variable POKERBOT_TOKEN is not set. "
                "Add it to your .env file or container environment.",
            )
        )

    if missing_required_settings:
        for error_type, message in missing_required_settings:
            logger.error(
                message,
                extra={
                    "error_type": error_type,
                    "category": "configuration",
                    "stage": "validation",
                },
            )
        sys.exit(1)

    webhook_missing_settings = []

    if not cfg.WEBHOOK_PATH:
        webhook_missing_settings.append(
            (
                "MissingWebhookPath",
                "Webhook path is not configured. Set POKERBOT_WEBHOOK_PATH in your "
                ".env file or container environment.",
            )
        )

    if not cfg.WEBHOOK_PUBLIC_URL:
        webhook_missing_settings.append(
            (
                "MissingWebhookPublicUrl",
                "Webhook public URL is not configured. Set POKERBOT_WEBHOOK_DOMAIN (recommended) "
                "together with POKERBOT_WEBHOOK_PATH, or provide POKERBOT_WEBHOOK_PUBLIC_URL in "
                "your .env file or container environment.",
            )
        )

    if not cfg.WEBHOOK_SECRET:
        webhook_missing_settings.append(
            (
                "MissingWebhookSecret",
                "Webhook secret token is not configured. Set POKERBOT_WEBHOOK_SECRET in your "
                ".env file or container environment.",
            )
        )

    use_polling = False
    if webhook_missing_settings:
        if getattr(cfg, "ALLOW_POLLING_FALLBACK", False):
            if not cfg.DEBUG:
                logger.warning(
                    "POKERBOT_ALLOW_POLLING_FALLBACK is enabled while DEBUG mode is off. "
                    "This fallback is intended for development only."
                )
            for error_type, message in webhook_missing_settings:
                logger.warning(
                    message,
                    extra={
                        "error_type": error_type,
                        "category": "configuration",
                        "stage": "validation",
                    },
                )
            logger.info(
                "Webhook configuration missing; falling back to long polling as requested by POKERBOT_ALLOW_POLLING_FALLBACK.",
                extra={"category": "configuration", "stage": "fallback", "debug_mode": cfg.DEBUG},
            )
            use_polling = True
        else:
            for error_type, message in webhook_missing_settings:
                logger.error(
                    message,
                    extra={
                        "error_type": error_type,
                        "category": "configuration",
                        "stage": "validation",
                    },
                )
            sys.exit(1)

    bot = PokerBot(token=cfg.TOKEN, cfg=cfg)
    if use_polling:
        bot.run_polling()
    else:
        bot.run()


if __name__ == "__main__":
    main()
