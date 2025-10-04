import asyncio
import logging
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from pokerapp.game_engine import GameEngine


class FullStub:
    """Production-like stub with parse_mode support."""

    async def send_message(
        self, chat_id: int, text: str, parse_mode: Optional[str] = None
    ) -> str:
        await asyncio.sleep(0)  # ensure coroutine behaviour
        return f"Sent with {parse_mode}"


class MinimalStub:
    """Test stub without parse_mode."""

    async def send_message(self, chat_id: int, text: str) -> str:
        await asyncio.sleep(0)
        return "Sent plain"


async def _noop_clear_game_messages(game, chat_id):
    return None


def _build_engine(messaging) -> GameEngine:
    return GameEngine(
        table_manager=AsyncMock(),
        view=messaging,
        winner_determination=Mock(),
        request_metrics=Mock(),
        round_rate=object(),
        player_manager=Mock(),
        matchmaking_service=Mock(),
        stats_reporter=Mock(),
        clear_game_messages=_noop_clear_game_messages,
        build_identity_from_player=lambda player: None,
        safe_int=int,
        old_players_key="old_players",
        telegram_safe_ops=Mock(),
        lock_manager=Mock(),
        logger=logging.getLogger("test"),
        constants=None,
        adaptive_player_report_cache=None,
        player_factory=None,
    )


@pytest.mark.asyncio
async def test_full_stub_keeps_parse_mode():
    """Verify production stubs receive parse_mode."""

    engine = _build_engine(FullStub())

    task = engine._create_send_message_task(
        chat_id=123,
        text="Test",
        parse_mode="Markdown",
    )

    assert task is not None
    result = await task
    assert "Markdown" in result  # ✅ parse_mode was passed


@pytest.mark.asyncio
async def test_minimal_stub_removes_parse_mode():
    """Verify test stubs work without parse_mode."""

    engine = _build_engine(MinimalStub())

    task = engine._create_send_message_task(
        chat_id=123,
        text="Test",
        parse_mode="Markdown",  # Would normally fail
    )

    assert task is not None
    result = await task
    assert result == "Sent plain"  # ✅ Called without parse_mode
