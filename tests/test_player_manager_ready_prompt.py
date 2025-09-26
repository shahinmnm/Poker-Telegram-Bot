import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pokerapp.entities import GameState
from pokerapp.player_manager import PlayerManager


@pytest.mark.asyncio
async def test_send_join_prompt_replaces_stale_ready_prompt(caplog):
    view = SimpleNamespace(send_message_return_id=AsyncMock(return_value=456))
    table_manager = SimpleNamespace(save_game=AsyncMock())
    manager = PlayerManager(
        view=view,
        table_manager=table_manager,
        logger=logging.getLogger("test.player_manager"),
    )

    players = [SimpleNamespace(ready_message_id="old-ready")]
    game = SimpleNamespace(
        state=GameState.INITIAL,
        ready_message_main_id=321,
        ready_message_main_text="old text",
        ready_message_game_id="old-game",
        ready_message_stage=GameState.INITIAL,
        players=players,
        id="new-game",
    )

    caplog.set_level(logging.INFO)

    await manager.send_join_prompt(game, chat_id=999)

    assert players[0].ready_message_id is None
    assert game.ready_message_main_id == 456
    assert game.ready_message_main_text == "برای نشستن سر میز دکمه را بزن"
    assert game.ready_message_game_id == "new-game"
    assert table_manager.save_game.await_count >= 1
    assert view.send_message_return_id.await_count == 1
    assert any(
        "Sent new ready prompt due to stale message" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_send_join_prompt_skips_when_prompt_current():
    view = SimpleNamespace(send_message_return_id=AsyncMock(return_value=789))
    table_manager = SimpleNamespace(save_game=AsyncMock())
    manager = PlayerManager(
        view=view,
        table_manager=table_manager,
        logger=logging.getLogger("test.player_manager"),
    )

    players = [SimpleNamespace(ready_message_id=None)]
    game = SimpleNamespace(
        state=GameState.INITIAL,
        ready_message_main_id=111,
        ready_message_main_text="existing",
        ready_message_game_id="game-1",
        ready_message_stage=GameState.INITIAL,
        players=players,
        id="game-1",
    )

    await manager.send_join_prompt(game, chat_id=555)

    assert view.send_message_return_id.await_count == 0
    table_manager.save_game.assert_not_awaited()
    assert game.ready_message_main_id == 111
