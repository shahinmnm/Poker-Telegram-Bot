import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError, NoScriptError, ResponseError

from pokerapp.utils.redis_safeops import RedisSafeOps


class _DummyRedis(SimpleNamespace):
    pass


@pytest.mark.asyncio
async def test_call_success():
    redis = _DummyRedis()
    redis.get = AsyncMock(return_value=b"value")
    safeops = RedisSafeOps(redis, max_retries=0, timeout_seconds=0.1)

    result = await safeops.safe_get("key", log_extra={"test": "success"})

    assert result == b"value"
    assert redis.get.await_count == 1


@pytest.mark.asyncio
async def test_connection_error_retries(monkeypatch):
    redis = _DummyRedis()
    redis.get = AsyncMock(side_effect=[ConnectionError("fail"), b"ok"])
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr("pokerapp.utils.redis_safeops.asyncio.sleep", fake_sleep)
    safeops = RedisSafeOps(redis, max_retries=1, base_backoff=0.01, timeout_seconds=0.1)

    result = await safeops.safe_get("key")

    assert result == b"ok"
    assert redis.get.await_count == 2
    assert sleep_calls == [0.01]


@pytest.mark.asyncio
async def test_no_script_error_raises_without_retry():
    redis = _DummyRedis()
    redis.evalsha = AsyncMock(side_effect=NoScriptError("missing"))
    safeops = RedisSafeOps(redis, max_retries=5, timeout_seconds=0.1)

    with pytest.raises(NoScriptError):
        await safeops.call("evalsha", "sha")

    assert redis.evalsha.await_count == 1


@pytest.mark.asyncio
async def test_response_error_no_retry():
    redis = _DummyRedis()
    redis.get = AsyncMock(side_effect=ResponseError("bad"))
    safeops = RedisSafeOps(redis, max_retries=3, timeout_seconds=0.1)

    with pytest.raises(ResponseError):
        await safeops.safe_get("key")

    assert redis.get.await_count == 1


@pytest.mark.asyncio
async def test_async_timeout_retries(monkeypatch):
    redis = _DummyRedis()

    async def delayed_failure(*args, **kwargs):
        await asyncio.sleep(0)
        raise asyncio.TimeoutError()

    redis.get = AsyncMock(side_effect=delayed_failure)
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr("pokerapp.utils.redis_safeops.asyncio.sleep", fake_sleep)
    safeops = RedisSafeOps(redis, max_retries=2, base_backoff=0.01, timeout_seconds=0.01)

    with pytest.raises(asyncio.TimeoutError):
        await safeops.safe_get("key")

    assert redis.get.await_count == 3
    positive_delays = [delay for delay in sleep_calls if delay > 0]
    assert positive_delays == [0.01, 0.02]
