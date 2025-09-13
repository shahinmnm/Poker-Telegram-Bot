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

    tm._tables.pop(chat)

    loaded_game = await tm.get_game(chat)
    loaded_player = loaded_game.seats[0]

    assert loaded_player.wallet is not None
    assert isinstance(loaded_player.wallet, WalletManagerModel)


def test_game_pickle():
    pickle.dumps(Game())
