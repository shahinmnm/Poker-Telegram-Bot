import asyncio
import logging

from telegram.constants import ParseMode
from telegram.helpers import MessageLimit

from pokerapp.telegram_validation import TelegramPayloadValidator


def test_normalize_text_truncates_overlong_messages(caplog):
    validator = TelegramPayloadValidator(
        enable_url_head_check=False, logger_=logging.getLogger("validation.truncate")
    )
    long_text = "A" * (MessageLimit.MAX_TEXT_LENGTH.value + 50)

    with caplog.at_level(logging.WARNING):
        sanitized = validator.normalize_text(
            long_text, parse_mode=None, context={"method": "test_truncate"}
        )

    assert len(sanitized) == MessageLimit.MAX_TEXT_LENGTH.value
    assert any("Truncated text exceeding limit" in rec.message for rec in caplog.records)


def test_normalize_text_escapes_invalid_markdown(caplog):
    validator = TelegramPayloadValidator(logger_=logging.getLogger("validation.markdown"))
    malformed = "*oops"

    with caplog.at_level(logging.WARNING):
        sanitized = validator.normalize_text(
            malformed,
            parse_mode=ParseMode.MARKDOWN,
            context={"method": "test_markdown"},
        )

    assert sanitized == "\\*oops"
    assert any("Escaped invalid Markdown" in rec.message for rec in caplog.records)


def test_validate_remote_media_rejects_invalid_url(caplog):
    validator = TelegramPayloadValidator(logger_=logging.getLogger("validation.url"))

    with caplog.at_level(logging.ERROR):
        is_valid = asyncio.run(
            validator.validate_remote_media("notaurl", context={"method": "test_media"})
        )

    assert is_valid is False
    assert any("Rejected media due to invalid URL" in rec.message for rec in caplog.records)

