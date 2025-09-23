"""Pytest configuration shared across the test suite."""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestMetrics


@pytest.fixture(autouse=True)
def _inject_viewer_dependencies(monkeypatch):
    """Provide default dependencies for PokerBotViewer in unit tests."""

    original_init = PokerBotViewer.__init__

    def _wrapped(self, *args, **kwargs):
        metrics = kwargs.get("request_metrics")
        if not isinstance(metrics, RequestMetrics):
            metrics = RequestMetrics(
                logger_=logging.getLogger("tests.request_metrics")
            )
            kwargs["request_metrics"] = metrics

        factory = kwargs.get("messaging_service_factory")

        if factory is None:

            def _factory(
                *,
                bot,
                deleted_messages,
                deleted_messages_lock,
                last_message_hash,
                last_message_hash_lock,
                cache_ttl: int = 3,
                cache_maxsize: int = 500,
            ) -> MessagingService:
                return MessagingService(
                    bot,
                    cache_ttl=cache_ttl,
                    cache_maxsize=cache_maxsize,
                    logger_=logging.getLogger("tests.messaging_service"),
                    request_metrics=metrics,
                    deleted_messages=deleted_messages,
                    deleted_messages_lock=deleted_messages_lock,
                    last_message_hash=last_message_hash,
                    last_message_hash_lock=last_message_hash_lock,
                )

            kwargs["messaging_service_factory"] = _factory

        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(PokerBotViewer, "__init__", _wrapped)
    yield
    monkeypatch.setattr(PokerBotViewer, "__init__", original_init)

