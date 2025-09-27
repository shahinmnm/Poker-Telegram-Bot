from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.utils.request_metrics import RequestCategory


def _make_model(admin_chat_id=123, with_messaging: bool = True):
    view = SimpleNamespace(_admin_chat_id=admin_chat_id)
    view.send_message = AsyncMock()
    messaging_service = None
    if with_messaging:
        messaging_service = SimpleNamespace(
            send_message=AsyncMock(),
            send_last_save_error_to_admin=AsyncMock(),
        )

    model = object.__new__(PokerBotModel)
    model._view = view
    model._logger = MagicMock()
    model._messaging_service = messaging_service
    return model, messaging_service, view.send_message


@pytest.mark.asyncio
async def test_handle_admin_command_ignored_without_admin_chat():
    model, messaging_service, send_message = _make_model(
        admin_chat_id=None
    )

    await model.handle_admin_command("/get_save_error", [], None)

    if messaging_service is not None:
        messaging_service.send_message.assert_not_awaited()
        messaging_service.send_last_save_error_to_admin.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_usage_message():
    model, messaging_service, send_message = _make_model()

    await model.handle_admin_command("/get_save_error", [], 123)

    messaging_service.send_message.assert_awaited_once()
    kwargs = messaging_service.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert (
        kwargs["text"] == "Usage: /get_save_error <chat_id> [detailed]"
    )
    assert kwargs["request_category"] == RequestCategory.GENERAL
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_invalid_chat_id():
    model, messaging_service, send_message = _make_model()

    await model.handle_admin_command("/get_save_error", ["abc"], 123)

    messaging_service.send_message.assert_awaited_once()
    kwargs = messaging_service.send_message.await_args.kwargs
    assert kwargs["text"] == "Invalid chat_id: abc"
    assert kwargs["request_category"] == RequestCategory.GENERAL
    messaging_service.send_last_save_error_to_admin.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_unknown_command():
    model, messaging_service, send_message = _make_model()

    await model.handle_admin_command("/unknown", ["1"], 123)

    messaging_service.send_message.assert_not_awaited()
    messaging_service.send_last_save_error_to_admin.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_no_payload():
    model, messaging_service, send_message = _make_model()

    await model.handle_admin_command("/get_save_error", ["101"], 123)

    messaging_service.send_last_save_error_to_admin.assert_awaited_once_with(
        admin_chat_id=123, chat_id=101, detailed=False
    )
    messaging_service.send_message.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_basic_payload():
    model, messaging_service, send_message = _make_model()

    await model.handle_admin_command("/get_save_error", ["77"], 123)

    messaging_service.send_last_save_error_to_admin.assert_awaited_once_with(
        admin_chat_id=123, chat_id=77, detailed=False
    )
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_detailed_payload():
    model, messaging_service, send_message = _make_model()

    await model.handle_admin_command("/get_save_error", ["77", "detailed"], 123)

    messaging_service.send_last_save_error_to_admin.assert_awaited_once_with(
        admin_chat_id=123, chat_id=77, detailed=True
    )
    messaging_service.send_message.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_admin_command_fallback_raw_payload():
    model, messaging_service, send_message = _make_model(with_messaging=False)

    await model.handle_admin_command("/get_save_error", ["55"], 123)

    send_message.assert_awaited_once()
    call_args = send_message.await_args
    assert call_args.args[0] == 123
    assert (
        call_args.args[1]
        == "Messaging service unavailable; cannot retrieve save errors."
    )

