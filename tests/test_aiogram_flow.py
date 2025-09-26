import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import logging
from types import SimpleNamespace

from pokerapp.aiogram_flow import (
    ActionButton,
    GameState,
    PlayerInfo,
    PokerMessagingOrchestrator,
    RequestManager,
    TurnState,
)


@pytest.mark.asyncio
async def test_request_manager_skips_empty_text():
    bot = AsyncMock()
    manager = RequestManager(bot, queue_delay=0)

    result = await manager.send_message(chat_id=1, text="   ")

    assert result is None
    bot.send_message.assert_not_awaited()
    await manager.close()


@pytest.mark.asyncio
async def test_request_manager_deduplicates_edits():
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=10))
    manager = RequestManager(bot, queue_delay=0)

    await manager.send_message(chat_id=123, text="hello")
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=10))

    await manager.edit_message_text(chat_id=123, message_id=10, text="hello")

    bot.edit_message_text.assert_not_awaited()
    await manager.close()


@pytest.mark.asyncio
async def test_orchestrator_creates_anchor_and_turn_messages():
    bot = AsyncMock()
    bot.send_message = AsyncMock(
        side_effect=[
            MagicMock(message_id=101),
            MagicMock(message_id=201),
        ]
    )

    orchestrator = PokerMessagingOrchestrator(
        bot=bot,
        chat_id=555,
        queue_delay=0,
    )

    players = [
        PlayerInfo(
            player_id=1,
            name="علی",
            seat_number=1,
            roles=("Dealer", "Small Blind"),
            buttons=(
                ActionButton(label="CALL", callback_data="call"),
                ActionButton(label="FOLD", callback_data="fold"),
            ),
        )
    ]
    turn_state = TurnState(
        board_cards=("4♥", "A♠"),
        pot=120,
        stack=1500,
        current_bet=40,
        max_bet=80,
        notice="دکمه‌های نوبت شما…",
    )

    await orchestrator.start_hand(players, turn_state=turn_state)

    assert orchestrator.state == GameState.IN_HAND
    assert bot.send_message.await_count == 2
    anchor_call = bot.send_message.await_args_list[0]
    assert anchor_call.kwargs["text"] == "\n".join(
        [
            "🎮 علی",
            "🪑 صندلی: 1",
            "🎖️ نقش: Dealer، Small Blind",
        ]
    )
    turn_call = bot.send_message.await_args_list[-1]
    text = turn_call.kwargs["text"]
    assert "🎰 مرحله بازی: Pre-Flop" in text
    assert "🃏 Board: 4♥     A♠" in text
    await orchestrator.request_manager.close()


@pytest.mark.asyncio
async def test_record_action_updates_turn_message():
    bot = AsyncMock()
    bot.send_message = AsyncMock(
        side_effect=[
            MagicMock(message_id=11),
            MagicMock(message_id=22),
        ]
    )
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=22))

    orchestrator = PokerMessagingOrchestrator(
        bot=bot,
        chat_id=777,
        queue_delay=0,
    )

    players = [
        PlayerInfo(player_id=1, name="Sara", seat_number=2),
    ]

    await orchestrator.start_hand(players, turn_state=TurnState())
    bot.edit_message_text.reset_mock()

    await orchestrator.record_action("Sara bet 50")

    assert bot.edit_message_text.await_count == 1
    edited_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "🎬 اکشن‌های اخیر:" in edited_text
    assert "• Sara bet 50" in edited_text
    await orchestrator.request_manager.close()


@pytest.mark.asyncio
async def test_voting_flow_updates_message():
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=55))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=55))
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)

    orchestrator = PokerMessagingOrchestrator(
        bot=bot,
        chat_id=999,
        queue_delay=0,
    )

    await orchestrator.start_voting(["Ali", "Reza"])
    bot.edit_message_text.reset_mock()
    bot.edit_message_reply_markup.reset_mock()

    await orchestrator.vote_continue("Ali")
    assert "✔️ Ali" in bot.edit_message_text.await_args.kwargs["text"]

    await orchestrator.vote_join("Sara")
    last_markup = bot.edit_message_text.await_args_list[-1].kwargs["reply_markup"]
    assert any(
        button.callback_data == "seat:join"
        for row in last_markup.inline_keyboard
        for button in row
    )

    approved = await orchestrator.end_voting()
    assert set(approved) == {"Ali", "Reza", "Sara"}
    bot.delete_message.assert_awaited()
    await orchestrator.request_manager.close()


@pytest.mark.asyncio
async def test_ready_prompt_edit_passes_current_game_id():
    bot = AsyncMock()
    messaging_service = AsyncMock()
    table_manager = AsyncMock()
    orchestrator = PokerMessagingOrchestrator(
        bot=bot,
        chat_id=222,
        queue_delay=0,
    )

    game = SimpleNamespace(
        id="game-1",
        ready_message_main_id=51,
        ready_message_game_id="game-1",
        state=SimpleNamespace(name="RUNNING"),
        seated_players=lambda: [1],
    )

    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()
    messaging_service.edit_message_text = AsyncMock(return_value=51)

    send_new = AsyncMock()

    await orchestrator.edit_ready_prompt(
        messaging_service=messaging_service,
        table_manager=table_manager,
        game=game,
        text="Ready players",
        send_new_prompt=send_new,
    )

    messaging_service.edit_message_text.assert_awaited_once()
    kwargs = messaging_service.edit_message_text.await_args.kwargs
    assert kwargs["current_game_id"] == "game-1"
    table_manager.save_game.assert_not_awaited()
    send_new.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_prompt_sends_new_when_stale(caplog):
    bot = AsyncMock()
    messaging_service = AsyncMock()
    table_manager = AsyncMock()
    orchestrator = PokerMessagingOrchestrator(
        bot=bot,
        chat_id=333,
        queue_delay=0,
    )

    game = SimpleNamespace(
        id="game-2",
        ready_message_main_id=77,
        ready_message_game_id="old-game",
        ready_message_main_text="old",
        ready_message_stage=None,
        state=SimpleNamespace(name="RUNNING"),
        seated_players=lambda: [1],
    )

    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()
    messaging_service.edit_message_text = AsyncMock()
    send_new = AsyncMock(return_value=88)

    caplog.set_level(logging.INFO)

    new_id = await orchestrator.edit_ready_prompt(
        messaging_service=messaging_service,
        table_manager=table_manager,
        game=game,
        text="new",
        send_new_prompt=send_new,
    )

    assert new_id == 88
    messaging_service.edit_message_text.assert_not_awaited()
    send_new.assert_awaited_once()
    table_manager.save_game.assert_awaited()
    assert game.ready_message_main_id == 88
    assert game.ready_message_game_id == "game-2"
    assert any(
        "Sent new ready prompt due to stale message ID" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_ready_prompt_waiting_without_players(caplog):
    bot = AsyncMock()
    messaging_service = AsyncMock()
    table_manager = AsyncMock()
    orchestrator = PokerMessagingOrchestrator(
        bot=bot,
        chat_id=444,
        queue_delay=0,
    )

    game = SimpleNamespace(
        id="game-3",
        ready_message_main_id=90,
        ready_message_game_id="game-3",
        ready_message_main_text="text",
        ready_message_stage=None,
        state=SimpleNamespace(name="WAITING"),
        seated_players=lambda: [],
    )

    table_manager.get_game = AsyncMock(return_value=game)
    table_manager.save_game = AsyncMock()
    send_new = AsyncMock(return_value=91)

    caplog.set_level(logging.INFO)

    new_id = await orchestrator.edit_ready_prompt(
        messaging_service=messaging_service,
        table_manager=table_manager,
        game=game,
        text="updated",
        send_new_prompt=send_new,
    )

    assert new_id == 91
    send_new.assert_awaited_once()
    table_manager.save_game.assert_awaited()
    assert game.ready_message_main_id == 91
    assert any(
        "Sent new ready prompt due to stale message ID" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_showdown_updates_and_clears_messages():
    bot = AsyncMock()
    bot.send_message = AsyncMock(
        side_effect=[
            MagicMock(message_id=31),
            MagicMock(message_id=32),
        ]
    )
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=32))
    bot.delete_message = AsyncMock(return_value=True)

    orchestrator = PokerMessagingOrchestrator(
        bot=bot,
        chat_id=1234,
        queue_delay=0,
    )

    players = [
        PlayerInfo(player_id=1, name="Nima", seat_number=5),
    ]

    await orchestrator.start_hand(players, turn_state=TurnState(board_cards=("4♥",)))
    bot.edit_message_text.reset_mock()

    await orchestrator.showdown(
        summary_lines=["Nima wins the pot"],
        chip_counts={1: "Stack: 2000"},
    )

    assert orchestrator.state == GameState.SHOWDOWN
    assert bot.edit_message_text.await_count == 1
    edited_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Nima wins the pot" in edited_text
    assert "Stack: 2000" in edited_text
    assert bot.delete_message.await_count == 2
    await orchestrator.request_manager.close()

