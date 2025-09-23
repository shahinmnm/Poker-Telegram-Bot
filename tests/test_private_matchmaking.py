import datetime
from types import SimpleNamespace
from typing import Tuple
from unittest.mock import AsyncMock, MagicMock
import logging

import fakeredis.aioredis
import pytest
from telegram.error import BadRequest

from pokerapp.config import Config
from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.stats import BaseStatsService
from pokerapp.table_manager import TableManager
from pokerapp.private_match_service import PrivateMatchService


def _build_update(
    user_id: int,
    chat_id: int,
    *,
    full_name: str | None = None,
) -> Tuple[SimpleNamespace, SimpleNamespace]:
    name = full_name or f"Player {user_id}"
    user = SimpleNamespace(
        id=user_id,
        full_name=name,
        first_name=name,
        username=f"player{user_id}",
    )
    chat = SimpleNamespace(id=chat_id, type="private", PRIVATE="private")
    update = SimpleNamespace(
        message=SimpleNamespace(text="🤝 بازی با ناشناس", chat=chat),
        effective_chat=chat,
        effective_user=user,
    )
    return update, chat


async def _build_model():
    kv = fakeredis.aioredis.FakeRedis()
    view = MagicMock()
    view.send_message = AsyncMock()
    view.send_message_return_id = AsyncMock(return_value=None)
    view.update_player_anchors_and_keyboards = AsyncMock()
    view.clear_all_player_anchors = AsyncMock(return_value=None)
    bot = MagicMock()
    cfg = Config()
    table_manager = TableManager(kv)
    stats = MagicMock(spec=BaseStatsService)
    stats.start_hand = AsyncMock()
    stats.finish_hand = AsyncMock()
    stats.register_player_profile = AsyncMock()
    stats.build_player_report = AsyncMock()
    stats.format_report = MagicMock()
    private_match_service = PrivateMatchService(
        kv=kv,
        table_manager=table_manager,
        logger=logging.getLogger("test.private_match"),
        constants=cfg.constants,
    )
    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=cfg,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
        stats_service=stats,
    )
    return model, kv, view, stats


@pytest.mark.asyncio
async def test_private_matchmaking_pairs_players_and_starts_match():
    model, kv, view, stats = await _build_model()

    update1, chat1 = _build_update(101, 201)
    context1 = SimpleNamespace(chat_data={}, bot_data={}, user_data={})
    await model.handle_private_matchmaking_request(update1, context1)

    assert view.send_message.await_count == 1
    first_call = view.send_message.await_args_list[0]
    assert first_call.args[0] == chat1.id
    assert "صف بازی خصوصی" in first_call.args[1]

    view.send_message.reset_mock()

    update2, chat2 = _build_update(202, 302)
    context2 = SimpleNamespace(chat_data={}, bot_data={}, user_data={})
    await model.handle_private_matchmaking_request(update2, context2)

    assert view.send_message.await_count == 2
    sent_chats = {call.args[0] for call in view.send_message.await_args_list}
    assert sent_chats == {chat1.id, chat2.id}
    assert stats.start_hand.await_count == 1
    match_id = stats.start_hand.await_args.args[0]
    assert match_id.startswith("pm_")

    await kv.flushall()


@pytest.mark.asyncio
async def test_private_matchmaking_cancellation_removes_user_from_queue():
    model, kv, view, _stats = await _build_model()

    update, chat = _build_update(303, 404)
    context = SimpleNamespace(chat_data={}, bot_data={}, user_data={})
    await model.handle_private_matchmaking_request(update, context)
    view.send_message.reset_mock()

    await model.handle_private_matchmaking_request(update, context)

    assert view.send_message.await_count == 1
    cancel_call = view.send_message.await_args_list[0]
    assert "از صف" in cancel_call.args[1]
    queue_members = await kv.zrange(
        model._private_match_service.queue_key, 0, -1
    )
    assert queue_members == []

    await kv.flushall()


@pytest.mark.asyncio
async def test_private_matchmaking_timeout_notifies_user():
    model, kv, view, _stats = await _build_model()

    update, chat = _build_update(404, 505)
    context = SimpleNamespace(chat_data={}, bot_data={}, user_data={})
    await model.handle_private_matchmaking_request(update, context)

    await kv.zadd(
        model._private_match_service.queue_key,
        {str(404): int(datetime.datetime.now().timestamp()) - 1000},
    )

    view.send_message.reset_mock()
    await model._private_match_service.cleanup_private_queue()

    assert view.send_message.await_count == 1
    timeout_call = view.send_message.await_args_list[0]
    assert "زمان انتظار" in timeout_call.args[1]
    queue_members = await kv.zrange(
        model._private_match_service.queue_key, 0, -1
    )
    assert queue_members == []

    await kv.flushall()


@pytest.mark.asyncio
async def test_private_matchmaking_reports_results_updates_stats():
    model, kv, view, stats = await _build_model()

    update1, _ = _build_update(505, 606)
    update2, _ = _build_update(606, 707)
    context1 = SimpleNamespace(chat_data={}, bot_data={}, user_data={})
    context2 = SimpleNamespace(chat_data={}, bot_data={}, user_data={})

    await model.handle_private_matchmaking_request(update1, context1)
    view.send_message.reset_mock()
    await model.handle_private_matchmaking_request(update2, context2)

    match_id = stats.start_hand.await_args.args[0]
    await model.report_private_match_result(match_id, 505)

    assert stats.finish_hand.await_count == 1
    finish_call = stats.finish_hand.await_args
    assert finish_call.args[0] == match_id
    results = finish_call.args[2]
    expected_pot_total = sum(result.payout for result in results)
    assert finish_call.args[3] == expected_pot_total == 1
    messages = [call.args[1] for call in view.send_message.await_args_list]
    assert any("برنده" in message for message in messages)

    await kv.flushall()


@pytest.mark.asyncio
async def test_private_matchmaking_escapes_markdown_names():
    model, kv, view, stats = await _build_model()

    unsafe_names = {"user_[test]", "ally_[test]"}

    async def safe_send_message(chat_id, text, *args, **kwargs):
        for unsafe in unsafe_names:
            if unsafe in text:
                raise BadRequest(f"Bad markdown detected: {unsafe}")
        return None

    view.send_message.side_effect = safe_send_message

    update1, chat1 = _build_update(1010, 2010, full_name="user_[test]")
    context1 = SimpleNamespace(chat_data={}, bot_data={}, user_data={})
    await model.handle_private_matchmaking_request(update1, context1)

    update2, chat2 = _build_update(2020, 3030, full_name="ally_[test]")
    context2 = SimpleNamespace(chat_data={}, bot_data={}, user_data={})
    await model.handle_private_matchmaking_request(update2, context2)

    match_id = stats.start_hand.await_args.args[0]

    await model.report_private_match_result(match_id, 1010)

    sent_texts = [call.args[1] for call in view.send_message.await_args_list]
    assert any("user\\_\\[test]" in text for text in sent_texts)
    assert any("ally\\_\\[test]" in text for text in sent_texts)

    await kv.flushall()

