"""Pytest configuration shared across the test suite."""

import logging
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import fakeredis
import fakeredis.aioredis
import pytest

from pokerapp.entities import Game
from pokerapp.game_engine import GameEngine
from pokerapp.lock_manager import LockManager
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestMetrics


class DummyTableManager:
    def __init__(self, game: Game) -> None:
        self._game = game
        self.save_count = 0

    async def load_game(self, chat_id: int):
        return self._game, None

    async def save_game(self, chat_id: int, game: Game) -> None:
        self._game = game
        self.save_count += 1


class DummyView:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> Optional[int]:
        self.messages.append((chat_id, text))
        return None


class DummySafeOps:
    def __init__(self, view: DummyView) -> None:
        self._view = view
        self.calls: list[tuple[int, str]] = []

    async def send_message_safe(
        self,
        *,
        call: Callable[[], Awaitable[Optional[int]]],
        chat_id: int,
        operation: Optional[str] = None,
        log_extra: Optional[dict] = None,
    ):
        self.calls.append((chat_id, operation or "send_message"))
        return await call()


def _build_engine_for_game(
    *,
    game: Game,
    redis_pool,
    logger_name: str = "engine-action",
) -> Tuple[GameEngine, DummyTableManager, DummyView]:
    logger = logging.getLogger(logger_name)
    lock_manager = LockManager(logger=logger, redis_pool=redis_pool)
    table_manager = DummyTableManager(game)
    view = DummyView()
    safe_ops = DummySafeOps(view)

    engine = GameEngine.__new__(GameEngine)
    engine._lock_manager = lock_manager
    engine._table_manager = table_manager
    engine._safe_ops = safe_ops
    engine._telegram_ops = safe_ops
    engine._view = view
    engine._logger = logger
    engine._valid_player_actions = {"fold", "check", "call", "raise"}
    engine._action_lock_ttl = 1
    engine._action_lock_feedback_text = "⚠️ Action in progress, please wait..."

    return engine, table_manager, view


@pytest.fixture
def redis_pool():
    server = fakeredis.FakeServer()
    return fakeredis.aioredis.FakeRedis(server=server)


@pytest.fixture
def game_engine_factory(redis_pool):
    def _factory(
        *,
        game: Game,
        logger_name: str = "test-engine",
    ) -> Tuple[GameEngine, DummyTableManager, DummyView]:
        return _build_engine_for_game(
            game=game, redis_pool=redis_pool, logger_name=logger_name
        )

    return _factory


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
                    table_manager=None,
                )

            kwargs["messaging_service_factory"] = _factory

        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(PokerBotViewer, "__init__", _wrapped)
    yield
    monkeypatch.setattr(PokerBotViewer, "__init__", original_init)

