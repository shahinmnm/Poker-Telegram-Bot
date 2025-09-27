import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.pokerbotmodel import PokerBotModel


def _make_model(admin_chat_id=123):
    view = SimpleNamespace(_admin_chat_id=admin_chat_id)
    view.send_message = AsyncMock()
    safe_get = AsyncMock()
    redis_ops = SimpleNamespace(safe_get=safe_get)
    table_manager = SimpleNamespace(_redis_ops=redis_ops)

    model = object.__new__(PokerBotModel)
    model._view = view
    model._table_manager = table_manager
    model._logger = MagicMock()
    return model, view.send_message, safe_get


@pytest.mark.asyncio
async def test_handle_admin_command_ignored_without_admin_chat():
    model, send_message, safe_get = _make_model(admin_chat_id=None)

    await model.handle_admin_command("/get_save_error", [])

    send_message.assert_not_awaited()
    safe_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_usage_message():
    model, send_message, safe_get = _make_model()

    await model.handle_admin_command("/get_save_error", [])

    send_message.assert_awaited_once()
    args, _ = send_message.await_args
    assert args[0] == 123
    assert args[1] == "Usage: /get_save_error <chat_id> [detailed]"
    safe_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_invalid_chat_id():
    model, send_message, safe_get = _make_model()

    await model.handle_admin_command("/get_save_error", ["abc"])

    send_message.assert_awaited_once()
    args, _ = send_message.await_args
    assert args[1] == "Invalid chat_id: abc"
    safe_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_unknown_command():
    model, send_message, safe_get = _make_model()

    await model.handle_admin_command("/unknown", ["1"])

    send_message.assert_not_awaited()
    safe_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_no_payload():
    model, send_message, safe_get = _make_model()
    safe_get.return_value = None

    await model.handle_admin_command("/get_save_error", ["101"])

    safe_get.assert_awaited_once_with(
        "chat:101:last_save_error", log_extra={"chat_id": 101}
    )
    args, _ = send_message.await_args
    assert args[1] == "No save error found for chat 101"


@pytest.mark.asyncio
async def test_handle_admin_command_basic_payload():
    model, send_message, safe_get = _make_model()
    safe_get.return_value = json.dumps(
        {"error": "boom", "timestamp": "2024-01-01T00:00:00Z"}
    )

    await model.handle_admin_command("/get_save_error", ["77"])

    safe_get.assert_awaited_once_with(
        "chat:77:last_save_error", log_extra={"chat_id": 77}
    )
    args, _ = send_message.await_args
    assert args[1] == "Error: boom\nTime: 2024-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_handle_admin_command_detailed_payload():
    model, send_message, safe_get = _make_model()
    safe_get.return_value = json.dumps(
        {
            "chat_id": 77,
            "timestamp": "2024",
            "game_state": "WAITING",
            "exception": "boom",
            "player_count": 1,
            "players": [{"user_id": 1, "seat_index": 2, "role": "dealer"}],
        }
    )

    await model.handle_admin_command("/get_save_error", ["77", "detailed"])

    safe_get.assert_awaited_once_with(
        "chat:77:last_save_error_detailed", log_extra={"chat_id": 77}
    )
    args, _ = send_message.await_args
    assert "Players (1):" in args[1]
    assert " - 1 seat 2 role dealer" in args[1]


@pytest.mark.asyncio
async def test_handle_admin_command_fallback_raw_payload():
    model, send_message, safe_get = _make_model()
    safe_get.return_value = b"not-json"

    await model.handle_admin_command("/get_save_error", ["55"])

    args, _ = send_message.await_args
    assert "Raw: not-json" in args[1]

