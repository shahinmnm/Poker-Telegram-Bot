import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.countdown_manager import CountdownState, SmartCountdownManager


class DummyBot(SimpleNamespace):
    """Simple async-capable bot stub."""

    def __init__(self):
        super().__init__(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321)),
            edit_message_text=AsyncMock(return_value=None),
        )


def _make_manager(monkeypatch: pytest.MonkeyPatch) -> SmartCountdownManager:
    bot = DummyBot()
    redis_client = MagicMock()
    logger = MagicMock()
    manager = SmartCountdownManager(bot, redis_client, logger)
    # Ensure we operate on a deterministic milestone configuration for tests.
    manager.milestones = [30, 15, 5, 0]
    manager.duration = 30
    return manager


@pytest.mark.asyncio
async def test_countdown_respects_config_duration(monkeypatch: pytest.MonkeyPatch):
    manager = _make_manager(monkeypatch)

    captured: dict[str, int] = {}

    async def fake_run_countdown_milestones(self, chat_id: int, duration: int, countdown_id=None):
        captured["duration"] = duration
        return 0, False

    # Bind the patched coroutine to this manager instance only.
    manager._run_countdown_milestones = fake_run_countdown_milestones.__get__(manager, SmartCountdownManager)  # type: ignore[assignment]

    await manager.start_countdown(chat_id=777, duration=None, player_count=3, pot_size=150)

    active_tasks = list(manager._active_countdowns.values())
    if active_tasks:
        await asyncio.gather(*active_tasks)

    assert captured["duration"] == 30, "Expected YAML-configured 30s duration to be used"


@pytest.mark.asyncio
async def test_player_join_merges_state(monkeypatch: pytest.MonkeyPatch):
    manager = _make_manager(monkeypatch)

    chat_id = 101
    initial_state = CountdownState(
        chat_id=chat_id,
        remaining_seconds=30,
        total_seconds=30,
        player_count=2,
        pot_size=100,
    )

    manager._countdown_states[chat_id] = initial_state
    manager._pending_updates[chat_id] = deque()
    manager._countdown_messages[chat_id] = 4242
    manager._countdown_timer_info[chat_id] = {"start_time": 0.0, "duration": 30}

    class FakeMonotonic:
        def __init__(self, value: float) -> None:
            self.value = value

        def __call__(self) -> float:
            return self.value

    fake_clock = FakeMonotonic(10.0)
    monkeypatch.setattr("pokerapp.countdown_manager.time.monotonic", fake_clock)

    await manager.on_player_joined(chat_id=chat_id, player_id=404)

    pending_state = manager._pending_updates[chat_id][-1]
    assert pending_state.remaining_seconds == 20
    assert pending_state.player_count == 3

    updated_state = manager._countdown_states[chat_id]
    assert updated_state.remaining_seconds == 20
    assert updated_state.player_count == 3
    assert manager._metrics["players_joined_during_countdown"] == 1


@pytest.mark.asyncio
async def test_milestone_updates_only(monkeypatch: pytest.MonkeyPatch):
    manager = _make_manager(monkeypatch)

    class FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def monotonic(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    fake_clock = FakeClock()

    async def fast_sleep(delay: float, *_args, **_kwargs) -> None:
        fake_clock.advance(delay)

    monkeypatch.setattr("pokerapp.countdown_manager.time.monotonic", fake_clock.monotonic)
    monkeypatch.setattr("pokerapp.countdown_manager.asyncio.sleep", fast_sleep)

    chat_id = 202
    await manager.start_countdown(chat_id=chat_id, duration=30, player_count=4, pot_size=500)

    active_tasks = list(manager._active_countdowns.values())
    if active_tasks:
        await asyncio.gather(*active_tasks)

    assert manager.bot.edit_message_text.await_count == 4

