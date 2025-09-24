import io
import json
import logging
from types import SimpleNamespace

import pytest

from pokerapp.logging_config import ContextJsonFormatter
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics
from pokerapp.utils.logging_helpers import STANDARD_CONTEXT_KEYS, add_context


class _DummyBot:
    async def send_message(self, **kwargs):  # pragma: no cover - simple stub
        return SimpleNamespace(message_id=kwargs.get("message_id", 101))


class _FailingBot:
    async def send_message(self, **kwargs):  # pragma: no cover - simple stub
        raise ValueError("simulated failure")


def test_context_json_formatter_includes_common_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ContextJsonFormatter())

    logger = logging.getLogger("test.logging.formatter")
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "structured message",
        extra={"chat_id": 42, "stage": "turn", "error_type": "ExampleError"},
    )

    handler.flush()
    output = stream.getvalue().strip()
    logger.removeHandler(handler)
    assert output, "log output should not be empty"

    payload = json.loads(output)
    assert payload["chat_id"] == 42
    assert payload["stage"] == "turn"
    assert payload["error_type"] == "ExampleError"
    assert payload["message"] == "structured message"
    assert "timestamp" in payload
    assert payload["timestamp"].endswith("+00:00") or payload["timestamp"].endswith("Z")


def test_logger_adapter_injects_standard_context_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ContextJsonFormatter())

    base_logger = logging.getLogger("test.logging.adapter")
    base_logger.addHandler(handler)
    base_logger.propagate = False
    base_logger.setLevel(logging.INFO)

    adapter = add_context(base_logger, event_type="unit_test")
    adapter.info("adapter message", extra={"chat_id": 101})

    handler.flush()
    output = stream.getvalue().strip()
    base_logger.removeHandler(handler)
    assert output, "log output should not be empty"

    payload = json.loads(output)
    for key in STANDARD_CONTEXT_KEYS:
        assert key in payload
    assert payload["event_type"] == "unit_test"
    assert payload["chat_id"] == 101
    assert payload["message"] == "adapter message"


@pytest.mark.asyncio
async def test_messaging_service_logs_include_context(caplog):
    logger = logging.getLogger("test.messaging.service")
    metrics = RequestMetrics(logger_=logger)
    service = MessagingService(
        _DummyBot(),
        logger_=logger,
        request_metrics=metrics,
    )

    context = {"game_id": "game-123", "stage": "TURN"}

    expected_hash = MessagingService._content_hash("hello", None)
    with caplog.at_level(logging.INFO, logger="test.messaging.service"):
        await service.send_message(
            chat_id=555,
            text="hello",
            request_category=RequestCategory.GENERAL,
            context=context,
        )

    record = next(r for r in caplog.records if getattr(r, "action", "") == "API_CALL")
    assert record.chat_id == 555
    assert record.game_id == "game-123"
    assert record.stage == "TURN"
    assert record.method == "sendMessage"
    assert record.category == "general"
    assert record.content_hash == expected_hash


@pytest.mark.asyncio
async def test_messaging_service_logs_errors_with_context(caplog):
    logger = logging.getLogger("test.messaging.service.error")
    metrics = RequestMetrics(logger_=logger)
    service = MessagingService(
        _FailingBot(),
        logger_=logger,
        request_metrics=metrics,
    )

    with pytest.raises(ValueError):
        with caplog.at_level(logging.ERROR, logger="test.messaging.service.error"):
            await service.send_message(
                chat_id=999,
                text="boom",
                request_category=RequestCategory.GENERAL,
                context={"game_id": "g-error"},
            )

    error_record = next(r for r in caplog.records if getattr(r, "action", "") == "API_ERROR")
    assert error_record.chat_id == 999
    assert error_record.error_type == "ValueError"
    assert error_record.game_id == "g-error"
    assert error_record.method == "sendMessage"
