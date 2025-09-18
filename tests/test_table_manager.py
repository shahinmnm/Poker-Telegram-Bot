import pickle

import pytest
import fakeredis
import fakeredis.aioredis

from pokerapp.entities import Game, Player
from pokerapp.pokerbotmodel import WalletManagerModel
from pokerapp.table_manager import TableManager


@pytest.mark.asyncio
async def test_table_manager_multiple_chats():
    server = fakeredis.FakeServer()
    redis_async = fakeredis.aioredis.FakeRedis(server=server)
    redis_sync = fakeredis.FakeRedis(server=server)
    tm = TableManager(redis_async, redis_sync)

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


@pytest.mark.asyncio
async def test_wallet_recreated_on_load():
    server = fakeredis.FakeServer()
    redis_sync = fakeredis.FakeRedis(server=server)
    redis_async = fakeredis.aioredis.FakeRedis(server=server)
    tm = TableManager(redis_async, redis_sync)

    chat = 123
    game = await tm.create_game(chat)

    wallet = WalletManagerModel("user1", redis_sync)
    player = Player(user_id="user1", mention_markdown="@u1", wallet=wallet, ready_message_id="ready")
    game.add_player(player, seat_index=0)
    await tm.save_game(chat, game)

    # Use a new TableManager to ensure the game is loaded from Redis
    tm = TableManager(redis_async, redis_sync)
    loaded_game = await tm.get_game(chat)
    loaded_player = loaded_game.seats[0]

    assert loaded_player.wallet is not None
    assert isinstance(loaded_player.wallet, WalletManagerModel)


@pytest.mark.asyncio
async def test_find_game_by_user():
    server = fakeredis.FakeServer()
    redis_async = fakeredis.aioredis.FakeRedis(server=server)
    redis_sync = fakeredis.FakeRedis(server=server)
    tm = TableManager(redis_async, redis_sync)

    chat = 321
    game = await tm.create_game(chat)
    wallet = WalletManagerModel("user1", redis_sync)
    player = Player(user_id="user1", mention_markdown="@u1", wallet=wallet, ready_message_id="ready")
    game.add_player(player, seat_index=0)
    await tm.save_game(chat, game)

    assert await tm.find_game_by_user("user1") == (game, chat)
    with pytest.raises(LookupError):
        await tm.find_game_by_user("user2")


@pytest.mark.asyncio
async def test_find_game_by_user_after_restart_loads_from_disk():
    server = fakeredis.FakeServer()
    redis_async = fakeredis.aioredis.FakeRedis(server=server)
    redis_sync = fakeredis.FakeRedis(server=server)
    tm = TableManager(redis_async, redis_sync)

    chat = -100123
    game = await tm.create_game(chat)
    wallet = WalletManagerModel("user42", redis_sync)
    player = Player(
        user_id="user42",
        mention_markdown="@u42",
        wallet=wallet,
        ready_message_id="ready",
    )
    game.add_player(player, seat_index=0)
    game.pot = 77
    await tm.save_game(chat, game)

    # simulate application restart by creating a new TableManager instance
    redis_async_new = fakeredis.aioredis.FakeRedis(server=server)
    redis_sync_new = fakeredis.FakeRedis(server=server)
    tm_restarted = TableManager(redis_async_new, redis_sync_new)

    loaded_game, loaded_chat = await tm_restarted.find_game_by_user("user42")

    assert loaded_chat == chat
    assert loaded_game.pot == 77
    assert loaded_game.seats[0].user_id == "user42"
    assert tm_restarted._tables[loaded_chat] is loaded_game


def test_game_pickle():
    pickle.dumps(Game())
