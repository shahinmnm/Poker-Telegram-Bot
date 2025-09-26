import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from pokerapp.utils.request_metrics import RequestCategory
from pokerapp.utils.telegram_safeops import TelegramSafeOps


class _DummyView:
    def __init__(self):
        self.calls = SimpleNamespace(edit=0, send=0, delete=0)
        self._edit_side_effects = []
        self._send_result = None

    def queue_edit_side_effects(self, *effects):
        self._edit_side_effects = list(effects)

    def set_send_result(self, value):
        self._send_result = value

    async def edit_message_text(
        self,
        *,
        chat_id,
        message_id,
        text,
        reply_markup,
        request_category,
        parse_mode,
        suppress_exceptions,
        current_game_id=None,
    ):
        self.calls.edit += 1
        if self._edit_side_effects:
            effect = self._edit_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return message_id

    async def send_message_return_id(
        self,
        chat_id,
        text,
        reply_markup,
        request_category,
        suppress_exceptions,
    ):
        self.calls.send += 1
        return self._send_result

    async def delete_message(
        self,
        chat_id,
        message_id,
        *,
        suppress_exceptions,
        **_kwargs,
    ):
        self.calls.delete += 1
        return None


@pytest.fixture
def logger():
    logging.basicConfig(level=logging.DEBUG)
    return logging.getLogger("telegram_safeops.test")


@pytest.mark.asyncio
async def test_retry_after_triggers_sleep(monkeypatch, logger):
    view = _DummyView()
    view.queue_edit_side_effects(RetryAfter(retry_after=1), 42)

    sleep_calls = []

    async def fake_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    safe_ops = TelegramSafeOps(
        view,
        logger=logger,
        max_retries=3,
        base_delay=0.2,
        max_delay=1.0,
        backoff_multiplier=2.0,
    )

    result = await safe_ops.edit_message_text(
        chat_id=100,
        message_id=200,
        text="test",
        request_category=RequestCategory.GENERAL,
    )

    assert result == 42
    assert sleep_calls == [1]
    assert view.calls.edit == 2


@pytest.mark.asyncio
async def test_network_error_exhausts_retries(monkeypatch, logger):
    view = _DummyView()
    view.queue_edit_side_effects(
        TimedOut("timeout"), TimedOut("timeout"), TimedOut("timeout")
    )

    sleep_calls = []

    async def fake_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    safe_ops = TelegramSafeOps(
        view,
        logger=logger,
        max_retries=2,
        base_delay=0.5,
        max_delay=1.5,
        backoff_multiplier=2.0,
    )

    result = await safe_ops.edit_message_text(
        chat_id=10,
        message_id=20,
        text="boom",
        request_category=RequestCategory.GENERAL,
    )

    assert result is None
    assert sleep_calls == [0.5, 1.0]
    assert view.calls.edit == 3  # initial + 2 retries
    assert view.calls.send == 1


@pytest.mark.asyncio
async def test_bad_request_falls_back_to_send(monkeypatch, logger):
    view = _DummyView()
    view.queue_edit_side_effects(BadRequest("bad"))
    view.set_send_result(555)

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    safe_ops = TelegramSafeOps(
        view,
        logger=logger,
        max_retries=1,
        base_delay=0.1,
        max_delay=0.2,
        backoff_multiplier=2.0,
    )

    result = await safe_ops.edit_message_text(
        chat_id=5,
        message_id=6,
        text="fallback",
        request_category=RequestCategory.GENERAL,
    )

    assert result == 555
    assert view.calls.send == 1
    assert view.calls.delete == 1


@pytest.mark.asyncio
async def test_send_message_safe_retries_retry_after(monkeypatch, logger):
    view = _DummyView()

    attempts = {"count": 0}

    async def flaky_call():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RetryAfter(retry_after=0.25)
        return "ok"

    sleep_calls = []

    async def fake_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    safe_ops = TelegramSafeOps(
        view,
        logger=logger,
        max_retries=2,
        base_delay=0.1,
        max_delay=1.0,
        backoff_multiplier=2.0,
    )

    result = await safe_ops.send_message_safe(
        call=flaky_call,
        chat_id=123,
        operation="custom_send",
        log_extra={"game_id": "game-123"},
    )

    assert result == "ok"
    assert attempts["count"] == 2
    assert sleep_calls == [0.25]


@pytest.mark.asyncio
async def test_delete_message_safe_exhausts_retries(monkeypatch, logger):
    view = _DummyView()

    attempts = {"count": 0}

    async def always_fail():
        attempts["count"] += 1
        raise TimedOut("timeout")

    sleep_calls = []

    async def fake_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    safe_ops = TelegramSafeOps(
        view,
        logger=logger,
        max_retries=1,
        base_delay=0.2,
        max_delay=0.5,
        backoff_multiplier=2.0,
    )

    with pytest.raises(TimedOut):
        await safe_ops.delete_message_safe(
            call=always_fail,
            chat_id=999,
            message_id=555,
            operation="custom_delete",
            log_extra={"game_id": "game-999"},
        )

    assert sleep_calls == [0.2]
    assert attempts["count"] == 2
