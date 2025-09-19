import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from telegram.error import BadRequest

from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.stats import PlayerHandResult, PlayerIdentity, StatsService


def _build_model(stats_service: StatsService):
    send_message = AsyncMock()

    async def safe_send_message(chat_id, text, *args, **kwargs):
        if "user_[test]" in text:
            raise BadRequest("Bad markdown detected: user_[test]")
        return None

    send_message.side_effect = safe_send_message
    view = SimpleNamespace(
        send_message=send_message,
    )
    bot = SimpleNamespace()
    cfg = SimpleNamespace(DEBUG=False)
    kv = fakeredis.aioredis.FakeRedis()
    table_manager = MagicMock()
    model = PokerBotModel(
        view,
        bot,
        cfg,
        kv,
        table_manager,
        stats_service=stats_service,
    )
    return model, view


def _make_update(
    user_id: int,
    chat_id: int,
    username: str = "player",
    *,
    full_name: str | None = None,
) -> SimpleNamespace:
    chat = SimpleNamespace(id=chat_id, type="private", PRIVATE="private")
    name = full_name or f"{username} tester"
    user = SimpleNamespace(
        id=user_id,
        full_name=name,
        first_name=name,
        username=username,
    )
    return SimpleNamespace(effective_chat=chat, effective_user=user)


@pytest.mark.asyncio
async def test_statistics_command_formats_report(tmp_path):
    db_path = tmp_path / "stats.sqlite3"
    service = StatsService(f"sqlite+aiosqlite:///{db_path}")
    await service.ensure_ready()

    try:
        model, view = _build_model(service)
        identity = PlayerIdentity(
            user_id=42,
            display_name="user_[test]",
            username="ali",
        )

        base_time = dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

        await service.start_hand(
            "hand-1",
            chat_id=777,
            players=[identity],
            start_time=base_time,
        )
        await service.finish_hand(
            "hand-1",
            chat_id=777,
            results=[
                PlayerHandResult(
                    user_id=identity.user_id,
                    display_name=identity.display_name,
                    total_bet=50,
                    payout=200,
                    net_profit=150,
                    hand_type="رویال فلاش",
                    was_all_in=True,
                    result="win",
                )
            ],
            pot_total=200,
            end_time=base_time + dt.timedelta(minutes=2),
        )

        await service.start_hand(
            "hand-2",
            chat_id=777,
            players=[identity],
            start_time=base_time + dt.timedelta(minutes=3),
        )
        await service.finish_hand(
            "hand-2",
            chat_id=777,
            results=[
                PlayerHandResult(
                    user_id=identity.user_id,
                    display_name=identity.display_name,
                    total_bet=60,
                    payout=120,
                    net_profit=60,
                    hand_type="فول هاوس",
                    was_all_in=False,
                    result="win",
                )
            ],
            pot_total=120,
            end_time=base_time + dt.timedelta(minutes=5),
        )

        await service.start_hand(
            "hand-3",
            chat_id=777,
            players=[identity],
            start_time=base_time + dt.timedelta(minutes=6),
        )
        await service.finish_hand(
            "hand-3",
            chat_id=777,
            results=[
                PlayerHandResult(
                    user_id=identity.user_id,
                    display_name=identity.display_name,
                    total_bet=40,
                    payout=0,
                    net_profit=-40,
                    hand_type="استریت",
                    was_all_in=False,
                    result="loss",
                )
            ],
            pot_total=80,
            end_time=base_time + dt.timedelta(minutes=7),
        )

        update = _make_update(
            identity.user_id,
            999,
            username="ali",
            full_name="user_[test]",
        )
        context = SimpleNamespace()

        await model._send_statistics_report(update, context)

        assert view.send_message.await_count == 1
        args, kwargs = view.send_message.await_args
        message = args[1]

        assert "🎮 مجموع دست‌ها: 3" in message
        assert "🏆 بردها: 2 | ❌ باخت‌ها: 1" in message
        assert "🔥 طولانی‌ترین برد متوالی: 2 دست" in message
        assert "💎 بزرگ‌ترین برد: 150$" in message
        assert "⚔️ دفعات آل-این: 1 (موفقیت 100.0٪)" in message
        assert "📐 بازده سرمایه (ROI): 700.0%" in message
        assert "🥇 پراکندگی دست‌های برنده:" in message
        assert "رویال فلاش" in message
        assert "📝 پنج دست اخیر:" in message
        assert "👤 نام: user\\_\\[test]" in message
        assert kwargs.get("reply_markup") is not None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_statistics_command_without_history(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    service = StatsService(f"sqlite+aiosqlite:///{db_path}")
    await service.ensure_ready()

    try:
        model, view = _build_model(service)
        update = _make_update(99, 555, username="new")
        context = SimpleNamespace()

        await model._send_statistics_report(update, context)

        assert view.send_message.await_count == 1
        args, _kwargs = view.send_message.await_args
        message = args[1]
        assert "ℹ️ هنوز داده‌ای برای نمایش وجود ندارد" in message
    finally:
        await service.close()
