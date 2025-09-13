import pytest
import fakeredis.aioredis

from pokerapp.table_manager import TableManager


@pytest.mark.asyncio
async def test_table_manager_multiple_chats():
    redis = fakeredis.aioredis.FakeRedis()
    tm = TableManager(redis)

    chat1, chat2 = 100, 200

    game1 = await tm.create_game(chat1)
    game2 = await tm.create_game(chat2)

    game1.pot = 10
    game2.pot = 20

    await tm.save_game(chat1, game1)
    await tm.save_game(chat2, game2)

    g1_loaded = await tm.get_game(chat1)
    g2_loaded = await tm.get_game(chat2)

    assert g1_loaded.pot == 10
    assert g2_loaded.pot == 20
