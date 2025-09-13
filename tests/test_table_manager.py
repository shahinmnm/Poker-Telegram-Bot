import pytest
import fakeredis.aioredis

from pokerapp.table_manager import TableManager


@pytest.mark.anyio("asyncio")
async def test_table_manager_multiple_groups():
    redis = fakeredis.aioredis.FakeRedis()
    tm = TableManager(redis)

    chat1, chat2 = 100, 200
    t1 = await tm.new_table(chat1)
    t2 = await tm.new_table(chat1)
    t3 = await tm.new_table(chat2)

    game1 = await tm.get_game(chat1, t1)
    game2 = await tm.get_game(chat1, t2)
    game3 = await tm.get_game(chat2, t3)

    game1.pot = 10
    game2.pot = 20
    game3.pot = 30

    await tm.save_game(chat1, t1, game1)
    await tm.save_game(chat1, t2, game2)
    await tm.save_game(chat2, t3, game3)

    assert set(await tm.list_tables(chat1)) == {t1, t2}
    assert set(await tm.list_tables(chat2)) == {t3}

    g1_loaded = await tm.get_game(chat1, t1)
    g2_loaded = await tm.get_game(chat1, t2)
    g3_loaded = await tm.get_game(chat2, t3)

    assert g1_loaded.pot == 10
    assert g2_loaded.pot == 20
    assert g3_loaded.pot == 30
