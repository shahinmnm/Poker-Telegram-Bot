import json

import fakeredis.aioredis
import pytest

from pokerapp.pokerbotview import CallbackTokenManager


@pytest.mark.asyncio
async def test_generate_token_persists_payload() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    manager = CallbackTokenManager(redis_client=redis, token_ttl=60)

    token, nonce, timestamp = await manager.generate_token(
        game_id=101, user_id=202, action="raise"
    )

    assert token
    assert nonce
    assert isinstance(timestamp, int)

    stored_bytes = await redis.get(f"action_token:101:202:{nonce}")
    assert stored_bytes is not None

    payload = json.loads(stored_bytes.decode("utf-8"))
    assert payload["token"] == token
    assert payload["used"] is False
    assert payload["action"] == "raise"


@pytest.mark.asyncio
async def test_validate_token_marks_token_as_used() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    manager = CallbackTokenManager(redis_client=redis, token_ttl=60)

    token, nonce, timestamp = await manager.generate_token(
        game_id=303, user_id=404, action="call"
    )

    is_valid, error = await manager.validate_token(
        game_id=303,
        user_id=404,
        token=token,
        nonce=nonce,
        timestamp=timestamp,
    )

    assert is_valid is True
    assert error == ""

    stored_bytes = await redis.get(f"action_token:303:404:{nonce}")
    assert stored_bytes is not None

    payload = json.loads(stored_bytes.decode("utf-8"))
    assert payload["used"] is True
    assert payload["used_at"] >= timestamp
