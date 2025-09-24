import asyncio
import logging
from typing import Any, Dict, Optional

import pytest
import fakeredis.aioredis

from pokerapp.utils.cache import AdaptivePlayerReportCache
from pokerapp.utils.player_report_cache import (
    PlayerReportCache as RedisPlayerReportCache,
)
from pokerapp.utils.redis_safeops import RedisSafeOps


class _FakeRedisOps:
    def __init__(self) -> None:
        self.storage: Dict[str, bytes] = {}
        self.safe_set_calls: int = 0
        self.safe_delete_calls: int = 0

    async def safe_set(
        self,
        key: str,
        value: bytes,
        *,
        expire: Optional[int] = None,
        log_extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self.safe_set_calls += 1
        self.storage[key] = value
        return True

    async def safe_get(
        self,
        key: str,
        *,
        log_extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[bytes]:
        return self.storage.get(key)

    async def safe_delete(
        self,
        key: str,
        *,
        log_extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        self.safe_delete_calls += 1
        if key in self.storage:
            del self.storage[key]
            return 1
        return 0


@pytest.mark.asyncio
async def test_default_ttl_hit_and_miss(monkeypatch):
    cache = AdaptivePlayerReportCache(default_ttl=100)
    calls: Dict[str, int] = {"count": 0}

    async def loader() -> Dict[str, int]:
        calls["count"] += 1
        return {"value": 42}

    # First access should miss and load via loader
    result_one = await cache.get_with_context(1, loader)
    assert result_one == {"value": 42}
    assert calls["count"] == 1

    # Second access should hit cache without calling loader again
    result_two = await cache.get_with_context(1, loader)
    assert result_two == {"value": 42}
    assert calls["count"] == 1

    metrics = cache.metrics()
    assert metrics["default"]["hits"] == 1
    assert metrics["default"]["misses"] == 1


@pytest.mark.asyncio
async def test_bonus_event_applies_shorter_ttl(monkeypatch):
    cache = AdaptivePlayerReportCache(default_ttl=120, bonus_ttl=30)
    fake_time = 0.0
    monkeypatch.setattr(cache, "_timer", lambda: fake_time)

    async def loader() -> Dict[str, int]:
        return {"value": 99}

    cache.invalidate_on_event([7], event_type="bonus_claimed")
    await cache.get_with_context(7, loader)
    assert cache.metrics()["bonus_claimed"]["misses"] == 1

    # Verify expiry was set using the bonus TTL
    expires_at = cache._expiry_map[7]
    assert expires_at - fake_time == pytest.approx(30)

    # Advance time just shy of TTL to confirm cache hit still works
    fake_time = 29.0
    result = await cache.get_with_context(7, loader)
    assert result == {"value": 99}
    assert cache.metrics()["default"]["hits"] >= 1


@pytest.mark.asyncio
async def test_invalidate_on_event_records_event_specific_ttls(monkeypatch):
    cache = AdaptivePlayerReportCache(default_ttl=120, bonus_ttl=45, post_hand_ttl=15)
    time_state = {"value": 0.0}
    monkeypatch.setattr(cache, "_timer", lambda: time_state["value"])

    async def loader() -> Dict[str, int]:
        return {"value": 1}

    cache.invalidate_on_event([11], event_type="hand_finished")
    cache.invalidate_on_event([12], event_type="bonus_claimed")

    assert cache._next_ttl[11] == ("hand_finished", 15)
    assert cache._next_ttl[12] == ("bonus_claimed", 45)

    await cache.get_with_context(11, loader)
    await cache.get_with_context(12, loader)

    assert cache._expiry_map[11] - time_state["value"] == pytest.approx(15)
    assert cache._expiry_map[12] - time_state["value"] == pytest.approx(45)


@pytest.mark.asyncio
async def test_player_report_cache_roundtrip_and_invalidate():
    redis = fakeredis.aioredis.FakeRedis()
    redis_ops = RedisSafeOps(redis, max_retries=0, timeout_seconds=0.1)
    cache = RedisPlayerReportCache(
        redis_ops,
        logger=logging.getLogger("test.player_report_cache"),
    )

    payload = {"formatted": "example"}
    stored = await cache.set_report(42, payload, ttl_seconds=30)
    assert stored is True

    fetched = await cache.get_report(42)
    assert fetched == payload

    removed = await cache.invalidate([42])
    assert removed == 1
    assert await cache.get_report(42) is None


@pytest.mark.asyncio
async def test_player_report_cache_respects_ttl_expiry():
    redis = fakeredis.aioredis.FakeRedis()
    redis_ops = RedisSafeOps(redis, max_retries=0, timeout_seconds=0.1)
    cache = RedisPlayerReportCache(
        redis_ops,
        logger=logging.getLogger("test.player_report_cache"),
    )

    await cache.set_report(77, {"formatted": "soon-expire"}, ttl_seconds=1)
    await asyncio.sleep(1.2)

    assert await cache.get_report(77) is None


@pytest.mark.asyncio
async def test_invalidate_on_event_triggers_loader_again():
    cache = AdaptivePlayerReportCache(default_ttl=100)
    calls = {"count": 0}

    async def loader() -> str:
        calls["count"] += 1
        return "report"

    await cache.get_with_context(33, loader)
    assert calls["count"] == 1

    cache.invalidate_on_event([33], event_type="hand_finished")
    await cache.get_with_context(33, loader)
    assert calls["count"] == 2
    assert cache.metrics()["hand_finished"]["misses"] == 1


@pytest.mark.asyncio
async def test_persistent_store_roundtrip():
    store = _FakeRedisOps()
    cache = AdaptivePlayerReportCache(default_ttl=50, persistent_store=store)

    calls = {"count": 0}

    async def loader() -> Dict[str, str]:
        calls["count"] += 1
        return {"player": "sam"}

    await cache.get_with_context(5, loader)
    assert store.safe_set_calls == 1
    key = "stats:5"
    assert key in store.storage
    assert calls["count"] == 1

    # Clear in-memory cache to force persistent retrieval
    cache._cache.clear()
    cache._expiry_map.clear()

    fetched = await cache.get_with_context(5, loader)
    assert fetched == {"player": "sam"}
    # Loader should not have been called a second time because of persistent hit
    assert store.safe_set_calls == 1
    assert calls["count"] == 1

    cache.invalidate_on_event([5], event_type="hand_finished")
    await asyncio.sleep(0)
    assert key not in store.storage
    assert store.safe_delete_calls >= 1


@pytest.mark.asyncio
async def test_concurrent_get_uses_single_loader_call():
    cache = AdaptivePlayerReportCache(default_ttl=90)
    calls = {"count": 0}
    loader_event = asyncio.Event()

    async def loader() -> str:
        calls["count"] += 1
        await loader_event.wait()
        return "payload"

    async def call_get() -> str:
        return await cache.get_with_context(77, loader)

    task_one = asyncio.create_task(call_get())
    await asyncio.sleep(0)
    task_two = asyncio.create_task(call_get())
    await asyncio.sleep(0)
    loader_event.set()
    results = await asyncio.gather(task_one, task_two)

    assert results == ["payload", "payload"]
    assert calls["count"] == 1

    # Subsequent call should hit cache immediately
    result = await cache.get_with_context(77, loader)
    assert result == "payload"
    assert cache.metrics()["default"]["hits"] >= 1


@pytest.mark.asyncio
async def test_hand_finished_event_sets_post_hand_ttl(monkeypatch):
    cache = AdaptivePlayerReportCache(default_ttl=120, post_hand_ttl=15)
    time_state = {"value": 0.0}
    monkeypatch.setattr(cache, "_timer", lambda: time_state["value"])

    calls = {"count": 0}

    async def loader() -> Dict[str, int]:
        calls["count"] += 1
        return {"value": calls["count"]}

    cache.invalidate_on_event([42], event_type="hand_finished")
    first = await cache.get_with_context(42, loader)
    assert first == {"value": 1}
    assert cache._expiry_map[42] - time_state["value"] == pytest.approx(15)

    time_state["value"] = 14.5
    hit = await cache.get_with_context(42, loader)
    assert hit == {"value": 1}
    assert calls["count"] == 1

    time_state["value"] = 15.1
    second = await cache.get_with_context(42, loader)
    assert second == {"value": 2}
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_cache_entry_expires_from_memory_and_persistent_store(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    redis_ops = RedisSafeOps(redis, max_retries=0, timeout_seconds=0.1)
    cache = AdaptivePlayerReportCache(
        default_ttl=60,
        post_hand_ttl=1,
        persistent_store=redis_ops,
    )

    time_state = {"value": 0.0}

    def fake_timer() -> float:
        return time_state["value"]

    monkeypatch.setattr(cache, "_timer", fake_timer)

    calls = {"count": 0}

    async def loader() -> Dict[str, int]:
        calls["count"] += 1
        return {"value": calls["count"]}

    cache.invalidate_on_event([7], event_type="hand_finished")
    first = await cache.get_with_context(7, loader)
    assert first == {"value": 1}
    ttl = await redis.ttl("stats:7")
    assert ttl in (1,)

    time_state["value"] = 0.5
    cached = await cache.get_with_context(7, loader)
    assert cached == {"value": 1}
    assert calls["count"] == 1

    time_state["value"] = 1.2
    await asyncio.sleep(1.2)
    assert await redis.exists("stats:7") == 0

    refreshed = await cache.get_with_context(7, loader)
    assert refreshed == {"value": 2}
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_unknown_event_type_falls_back_to_default_ttl(monkeypatch):
    cache = AdaptivePlayerReportCache(default_ttl=90, bonus_ttl=20, post_hand_ttl=10)
    time_state = {"value": 0.0}
    monkeypatch.setattr(cache, "_timer", lambda: time_state["value"])

    async def loader() -> Dict[str, int]:
        return {"value": 5}

    cache.invalidate_on_event([99], event_type="unexpected")
    await cache.get_with_context(99, loader)

    assert cache._expiry_map[99] - time_state["value"] == pytest.approx(90)
