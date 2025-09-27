import json
import logging
from types import SimpleNamespace
from typing import Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics


def _make_service(table_manager=None) -> Tuple[MessagingService, AsyncMock]:
    service = MessagingService(
        bot=SimpleNamespace(),
        logger_=logging.getLogger("test"),
        request_metrics=MagicMock(spec=RequestMetrics),
        table_manager=table_manager,
    )
    send_message = AsyncMock()
    service.send_message = send_message  # type: ignore[assignment]
    return service, send_message


@pytest.mark.asyncio
async def test_send_last_save_error_no_table_manager():
    service, send_message = _make_service()

    await service.send_last_save_error_to_admin(
        admin_chat_id=10,
        chat_id=42,
    )

    send_message.assert_awaited_once_with(
        chat_id=10,
        text="TableManager not available, cannot fetch save error.",
        request_category=RequestCategory.GENERAL,
        context={"admin_chat_id": 10, "chat_id": 42, "detailed": False},
    )


@pytest.mark.asyncio
async def test_send_last_save_error_redis_failure():
    safe_get = AsyncMock(side_effect=RuntimeError("boom"))
    table_manager = SimpleNamespace(_redis_ops=SimpleNamespace(safe_get=safe_get))
    service, send_message = _make_service(table_manager)

    await service.send_last_save_error_to_admin(
        admin_chat_id=11,
        chat_id=7,
    )

    send_message.assert_awaited_once()
    kwargs = send_message.await_args.kwargs
    assert kwargs["chat_id"] == 11
    assert kwargs["request_category"] == RequestCategory.GENERAL
    assert "Error fetching from Redis" in kwargs["text"]
    assert kwargs["context"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_send_last_save_error_missing_payload():
    safe_get = AsyncMock(return_value=None)
    table_manager = SimpleNamespace(_redis_ops=SimpleNamespace(safe_get=safe_get))
    service, send_message = _make_service(table_manager)

    await service.send_last_save_error_to_admin(
        admin_chat_id=12,
        chat_id=55,
    )

    send_message.assert_awaited_once_with(
        chat_id=12,
        text="No save error found for chat 55",
        request_category=RequestCategory.GENERAL,
        context={"admin_chat_id": 12, "chat_id": 55, "detailed": False},
    )
    safe_get.assert_awaited_once_with(
        "chat:55:last_save_error",
        log_extra={"chat_id": 55},
    )


@pytest.mark.asyncio
async def test_send_last_save_error_basic_payload():
    safe_get = AsyncMock(
        return_value='{"error": "boom", "timestamp": "2024"}'
    )
    table_manager = SimpleNamespace(_redis_ops=SimpleNamespace(safe_get=safe_get))
    service, send_message = _make_service(table_manager)

    await service.send_last_save_error_to_admin(
        admin_chat_id=13,
        chat_id=99,
    )

    send_message.assert_awaited_once()
    kwargs = send_message.await_args.kwargs
    assert kwargs["chat_id"] == 13
    assert "Error: boom" in kwargs["text"]
    assert "Time: 2024" in kwargs["text"]


@pytest.mark.asyncio
async def test_send_last_save_error_raw_payload():
    safe_get = AsyncMock(return_value=b"not-json")
    table_manager = SimpleNamespace(_redis_ops=SimpleNamespace(safe_get=safe_get))
    service, send_message = _make_service(table_manager)

    await service.send_last_save_error_to_admin(
        admin_chat_id=14,
        chat_id=101,
    )

    send_message.assert_awaited_once()
    kwargs = send_message.await_args.kwargs
    assert "Raw: not-json" in kwargs["text"]


@pytest.mark.asyncio
async def test_send_last_save_error_detailed_payload():
    safe_get = AsyncMock(
        return_value=json.dumps(
            {
                "chat_id": 21,
                "timestamp": "2024-02-02",
                "game_state": "WAITING",
                "exception": "boom",
                "pickle_size": 123,
                "player_count": 1,
                "players": [
                    {"user_id": 1, "seat_index": 2, "role": "dealer"}
                ],
            }
        )
    )
    table_manager = SimpleNamespace(_redis_ops=SimpleNamespace(safe_get=safe_get))
    service, send_message = _make_service(table_manager)

    await service.send_last_save_error_to_admin(
        admin_chat_id=15,
        chat_id=21,
        detailed=True,
    )

    send_message.assert_awaited_once()
    kwargs = send_message.await_args.kwargs
    assert "Game State: WAITING" in kwargs["text"]
    assert "Pickle Size: 123" in kwargs["text"]
    assert "Players (1):" in kwargs["text"]
    assert "User 1 seat 2 role dealer" in kwargs["text"]
    safe_get.assert_awaited_once_with(
        "chat:21:last_save_error_detailed",
        log_extra={"chat_id": 21},
    )
