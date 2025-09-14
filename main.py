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

    if cfg.TOKEN == "":
        logger.error(
            "Environment variable POKERBOT_TOKEN is not set",
            extra={"error_type": "MissingToken"},
        )
        exit(1)

    bot = PokerBot(token=cfg.TOKEN, cfg=cfg)
    bot.run()


if __name__ == "__main__":
    main()
