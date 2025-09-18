import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram.error import TelegramError

from pokerapp.pokerbot import PokerBot


def _create_bot(allow_polling_fallback: bool) -> PokerBot:
    bot = PokerBot.__new__(PokerBot)
    bot._cfg = SimpleNamespace(ALLOW_POLLING_FALLBACK=allow_polling_fallback)
    return bot


def test_run_falls_back_to_polling_when_enabled(caplog):
    bot = _create_bot(True)
    bot.run_webhook = Mock(side_effect=TelegramError("webhook failure"))
    bot.run_polling = Mock()

    with caplog.at_level(logging.ERROR):
        bot.run()

    bot.run_polling.assert_called_once()
    assert "falling back to polling mode" in caplog.text


def test_run_reraises_when_fallback_disabled():
    bot = _create_bot(False)
    bot.run_webhook = Mock(side_effect=OSError("address already in use"))
    bot.run_polling = Mock()

    with pytest.raises(OSError):
        bot.run()

    bot.run_polling.assert_not_called()
