import pytest
import logging
import fakeredis
import fakeredis.aioredis
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pokerapp.config import Config
from pokerapp.table_manager import TableManager
from pokerapp.pokerbotmodel import PokerBotModel, KEY_CHAT_DATA_GAME
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.utils.request_metrics import RequestMetrics


@pytest.mark.asyncio
async def test_end_hand_persists_game_and_reuses_instance():
    server = fakeredis.FakeServer()
    redis_async = fakeredis.aioredis.FakeRedis(server=server)
    redis_sync = fakeredis.FakeRedis(server=server)
    table_manager = TableManager(redis_async, redis_sync)

    bot = SimpleNamespace(send_message=AsyncMock())
    view = SimpleNamespace(
        send_message=AsyncMock(),
        send_message_return_id=AsyncMock(return_value=1),
        request_metrics=RequestMetrics(
            logger_=logging.getLogger("test.end_hand.request_metrics")
        ),
    )
    cfg = Config()
    private_match_service = PrivateMatchService(
        kv=redis_sync,
        table_manager=table_manager,
        logger=logging.getLogger("test.private_match"),
        constants=cfg.constants,
    )
    model = PokerBotModel(
        view,
        bot,
        cfg,
        redis_sync,
        table_manager,
        private_match_service=private_match_service,
    )

    chat_id = 123
    game = await table_manager.create_game(chat_id)
    context = SimpleNamespace(bot=bot, chat_data={})

    await model._end_hand(game, chat_id, context)

    new_game = context.chat_data[KEY_CHAT_DATA_GAME]
    assert await table_manager.get_game(chat_id) is new_game

    new_context = SimpleNamespace(bot=bot, chat_data={})
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))
    loaded_game, _ = await model._get_game(update, new_context)
    assert loaded_game is new_game
