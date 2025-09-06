#!/usr/bin/env python3

from dotenv import load_dotenv

from pokerapp.config import Config
from pokerapp.pokerbot import PokerBot
import logging
from pokerapp.pokerbot import PokerBot
from pokerapp.config import Config

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)

def main() -> None:
    load_dotenv()
    cfg: Config = Config()

    if cfg.TOKEN == "":
        print("Environment varaible POKERBOT_TOKEN is not set")
        exit(1)

    bot = PokerBot(token=cfg.TOKEN, cfg=cfg)
    bot.run()



if __name__ == "__main__":
    main()
