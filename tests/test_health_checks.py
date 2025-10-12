"""Unit tests for pruning health check instrumentation."""

from unittest.mock import MagicMock

from pokerapp.health_checks import PruningHealthCheck


def test_pruning_health_ttl_expiry():
    """Recorded prune operations should remain visible until TTL expiry."""

    model = MagicMock()
    health = PruningHealthCheck(model)

    health.record_prune(chat_id=123, success=True, duration_ms=5.0)

    assert 123 in health._last_prune_times

    status = health.get_health_status()
    assert status["active_games"] >= 1


def test_pruning_health_memory_bound():
    """TTL cache should honour the configured maximum size."""

    model = MagicMock()
    health = PruningHealthCheck(model)

    for chat_id in range(1500):
        health.record_prune(chat_id=chat_id, success=True, duration_ms=5.0)

    assert len(health._last_prune_times) <= 1000
