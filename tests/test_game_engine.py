import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pokerapp.entities import Game, GameState, Player, Wallet
from pokerapp.game_engine import GameEngine


if not hasattr(GameState, "WAITING"):
    GameState.WAITING = GameState.INITIAL  # type: ignore[attr-defined]


class DummyWallet(Wallet):
    @staticmethod
    def _prefix(id: int, suffix: str = ""):
        return f"wallet:{id}{suffix}"

    async def add_daily(self, amount: int) -> int:
        return amount

    async def has_daily_bonus(self) -> bool:
        return False

    async def inc(self, amount: int = 0) -> int:
        return amount

    async def inc_authorized_money(self, game_id: str, amount: int) -> None:
        return None

    async def authorized_money(self, game_id: str) -> int:
        return 0

    async def authorize(self, game_id: str, amount: int) -> None:
        return None

    async def authorize_all(self, game_id: str) -> int:
        return 0

    async def value(self) -> int:
        return 0

    async def approve(self, game_id: str) -> None:
        return None

    async def cancel(self, game_id: str) -> None:
        return None


def _make_player(user_id: str) -> Player:
    return Player(
        user_id=user_id,
        mention_markdown=f"@player{user_id}",
        wallet=DummyWallet(),
        ready_message_id="ready",
    )


@pytest.fixture
def countdown_game_engine():
    table_manager = MagicMock()
    table_manager.get_game = AsyncMock()
    table_manager.save_game = AsyncMock()

    view = MagicMock()
    view._bot = None

    request_metrics = MagicMock()
    request_metrics.end_cycle = AsyncMock()

    stats_reporter = MagicMock()
    stats_reporter.invalidate_players = AsyncMock()

    player_manager = MagicMock()
    player_manager.clear_player_anchors = AsyncMock()
    player_manager.cleanup_ready_prompt = AsyncMock()
    player_manager.send_join_prompt = AsyncMock()

    async def _passthrough_send_message_safe(*, call, **_kwargs):
        return await call()

    telegram_safe_ops = SimpleNamespace(
        edit_message_text=AsyncMock(return_value=None),
        send_message_safe=AsyncMock(side_effect=_passthrough_send_message_safe),
    )

    @asynccontextmanager
    async def _noop_guard(*_args, **_kwargs):
        yield

    lock_manager = SimpleNamespace(
        trace_guard=_noop_guard,
        table_write_lock=_noop_guard,
        table_read_lock=_noop_guard,
        _resolve_lock_category=MagicMock(return_value="engine_stage"),
        _resolve_level=MagicMock(return_value=1),
        _log_lock_snapshot_on_timeout=MagicMock(),
        detect_deadlock=MagicMock(return_value={}),
    )

    engine = GameEngine(
        table_manager=table_manager,
        view=view,
        winner_determination=MagicMock(),
        request_metrics=request_metrics,
        round_rate=MagicMock(),
        player_manager=player_manager,
        matchmaking_service=MagicMock(),
        stats_reporter=stats_reporter,
        clear_game_messages=AsyncMock(),
        build_identity_from_player=lambda player: player,
        safe_int=int,
        old_players_key="old_players",
        telegram_safe_ops=telegram_safe_ops,
        lock_manager=lock_manager,
        logger=MagicMock(),
        adaptive_player_report_cache=MagicMock(),
    )

    engine._ensure_smart_countdown_manager_started = AsyncMock()

    return SimpleNamespace(
        engine=engine,
        table_manager=table_manager,
        player_manager=player_manager,
    )


@pytest.mark.asyncio
async def test_countdown_completion_starts_game(countdown_game_engine):
    engine = countdown_game_engine.engine
    table_manager = countdown_game_engine.table_manager

    game = Game()
    game.state = GameState.WAITING
    game.ready_message_main_id = 999
    game.add_player(_make_player("1"), seat_index=0)
    game.add_player(_make_player("2"), seat_index=1)

    table_manager.get_game = AsyncMock(return_value=game)

    class CountdownStub:
        def __init__(self) -> None:
            self.calls = []
            self.is_active = False

        async def start_countdown(
            self,
            chat_id: int,
            duration: int | None = None,
            player_count: int = 0,
            pot_size: int = 0,
            on_complete=None,
            message_id: int | None = None,
        ) -> bool:
            self.calls.append(
                {
                    "chat_id": chat_id,
                    "duration": duration,
                    "player_count": player_count,
                    "pot_size": pot_size,
                    "message_id": message_id,
                }
            )
            self.is_active = True
            if on_complete:
                await on_complete(chat_id)
            self.is_active = False
            return True

        def is_countdown_active(self, _chat_id: int) -> bool:
            return self.is_active

        async def update_countdown_display(self, *args, **kwargs):
            return True

        async def cancel_countdown(self, _chat_id: int) -> None:
            self.is_active = False

    stub = CountdownStub()
    engine._smart_countdown_manager = stub
    engine.start_game = AsyncMock()

    context = SimpleNamespace(chat_data={})

    await engine._start_prestart_countdown(
        chat_id=555,
        duration_seconds=30,
        context=context,
    )

    engine.start_game.assert_awaited_once_with(context, game, 555)


@pytest.mark.asyncio
async def test_countdown_rejected_when_active(countdown_game_engine):
    engine = countdown_game_engine.engine
    table_manager = countdown_game_engine.table_manager

    game = Game()
    game.state = GameState.WAITING
    game.ready_message_main_id = 888
    game.add_player(_make_player("1"), seat_index=0)
    game.add_player(_make_player("2"), seat_index=1)

    table_manager.get_game = AsyncMock(return_value=game)

    class RejectingCountdownStub:
        def __init__(self) -> None:
            self.results: list[bool] = []
            self.calls: list[dict[str, int | None]] = []
            self.is_active = False

        def is_countdown_active(self, _chat_id: int) -> bool:
            return self.is_active

        async def start_countdown(
            self,
            chat_id: int,
            duration: int | None = None,
            player_count: int = 0,
            pot_size: int = 0,
            on_complete=None,
            message_id: int | None = None,
        ) -> bool:
            call = {
                "chat_id": chat_id,
                "duration": duration,
                "player_count": player_count,
                "pot_size": pot_size,
                "message_id": message_id,
            }
            self.calls.append(call)
            result = not self.is_active
            self.results.append(result)
            if not self.is_active:
                self.is_active = True
            return result

        async def update_countdown_display(self, *args, **kwargs) -> bool:
            return False

        async def cancel_countdown(self, _chat_id: int) -> None:
            self.is_active = False

    stub = RejectingCountdownStub()
    engine._smart_countdown_manager = stub
    engine.start_game = AsyncMock()

    await engine.start_waiting_countdown(chat_id=42, trigger="initial")
    await engine.start_waiting_countdown(chat_id=42, trigger="duplicate")

    assert stub.results == [True, False]

    warning_calls = engine._logger.warning.call_args_list
    assert any(
        call.kwargs.get("extra", {}).get("event_type") == "countdown_start_rejected"
        for call in warning_calls
    )
    assert engine.start_game.await_count == 0

