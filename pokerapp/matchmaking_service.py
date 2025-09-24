"""Matchmaking and hand lifecycle helpers for the poker engine."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from pokerapp.entities import ChatId, Game, GameState, Player, PlayerState
from pokerapp.player_manager import PlayerManager
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.stats_reporter import StatsReporter
from pokerapp.utils.request_metrics import RequestMetrics
from pokerapp.lock_manager import LockManager


class MatchmakingService:
    """Encapsulate start-hand orchestration and round progression."""

    def __init__(
        self,
        *,
        view: PokerBotViewer,
        round_rate,
        request_metrics: RequestMetrics,
        player_manager: PlayerManager,
        stats_reporter: StatsReporter,
        lock_manager: LockManager,
        send_turn_message: Callable[[Game, Player, ChatId], Awaitable[None]],
        safe_int: Callable[[ChatId], int],
        old_players_key: str,
        logger: logging.Logger,
    ) -> None:
        self._view = view
        self._round_rate = round_rate
        self._request_metrics = request_metrics
        self._player_manager = player_manager
        self._stats_reporter = stats_reporter
        self._lock_manager = lock_manager
        self._send_turn_message = send_turn_message
        self._safe_int = safe_int
        self._old_players_key = old_players_key
        self._logger = logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start_game(
        self,
        *,
        context,
        game: Game,
        chat_id: ChatId,
        build_identity_from_player: Callable[[Player], object],
    ) -> None:
        await self._player_manager.cleanup_ready_prompt(game, chat_id)

        if not self._ensure_dealer_position(game):
            return

        await self._stats_reporter.hand_started(
            game,
            chat_id,
            build_identity_from_player,
        )

        await self._initialize_hand_state(game, chat_id)
        await self._clear_seat_state(game, chat_id)

        await self._deal_hole_cards(game, chat_id)
        if game.state == GameState.INITIAL:
            return

        current_player = await self._post_blinds_and_prepare_players(game, chat_id)

        await self._handle_post_start_notifications(
            context=context,
            game=game,
            chat_id=chat_id,
            current_player=current_player,
        )

    async def progress_stage(
        self,
        *,
        context,
        chat_id: ChatId,
        game: Game,
        finalize_game: Callable[[Any, Game, ChatId], Awaitable[None]],
    ) -> bool:
        game.chat_id = chat_id

        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            await finalize_game(context=context, game=game, chat_id=chat_id)
            game.current_player_index = -1
            return False

        stage_transitions: Dict[GameState, Tuple[GameState, int, str]] = {
            GameState.ROUND_PRE_FLOP: (GameState.ROUND_FLOP, 3, "🃏 فلاپ"),
            GameState.ROUND_FLOP: (GameState.ROUND_TURN, 1, "🃏 ترن"),
            GameState.ROUND_TURN: (GameState.ROUND_RIVER, 1, "🃏 ریور"),
        }

        while True:
            self._round_rate.collect_bets_for_pot(game)
            for player in game.players:
                player.has_acted = False

            transition = stage_transitions.get(game.state)
            if transition:
                next_state, card_count, stage_label = transition
                self._logger.debug(
                    "Advancing game %s to %s",
                    game.id,
                    self._state_token(next_state),
                )
                game.state = next_state
                await self._add_cards_to_table(
                    count=card_count,
                    game=game,
                    chat_id=chat_id,
                    street_name=stage_label,
                    send_message=True,
                )
            elif game.state == GameState.ROUND_RIVER:
                await finalize_game(context=context, game=game, chat_id=chat_id)
                game.current_player_index = -1
                return False
            else:
                self._logger.warning("Unexpected state %s during stage progression", game.state)
                game.current_player_index = -1
                return False

            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if not active_players:
                if game.state == GameState.ROUND_RIVER:
                    await finalize_game(context=context, game=game, chat_id=chat_id)
                    game.current_player_index = -1
                    return False
                continue

            first_player_index = self._round_rate._find_next_active_player_index(  # type: ignore[attr-defined]
                game,
                game.dealer_index,
            )
            game.current_player_index = first_player_index
            if first_player_index == -1:
                if game.state == GameState.ROUND_RIVER:
                    await finalize_game(context=context, game=game, chat_id=chat_id)
                    game.current_player_index = -1
                    return False
                continue

            return True

    async def add_cards_to_table(
        self,
        *,
        count: int,
        game: Game,
        chat_id: ChatId,
        street_name: str,
        send_message: bool,
    ) -> None:
        async with self._lock_manager.guard(self._stage_lock_key(chat_id), timeout=10):
            await self._add_cards_to_table(
                count=count,
                game=game,
                chat_id=chat_id,
                street_name=street_name,
                send_message=send_message,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        for player in game.seated_players():
            if len(game.remain_cards) < 2:
                await self._view.send_message(
                    chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود."
                )
                await self._request_metrics.end_cycle(
                    self._safe_int(chat_id), cycle_token=game.id
                )
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

    def _ensure_dealer_position(self, game: Game) -> bool:
        if not hasattr(game, "dealer_index"):
            game.dealer_index = -1

        new_dealer_index = game.advance_dealer()
        if new_dealer_index == -1:
            new_dealer_index = game.next_occupied_seat(-1)
            game.dealer_index = new_dealer_index

        if game.dealer_index == -1:
            self._logger.warning("Cannot start game without an occupied dealer seat")
            return False
        return True

    async def _initialize_hand_state(self, game: Game, chat_id: ChatId) -> None:
        game.state = GameState.ROUND_PRE_FLOP
        await self._request_metrics.start_cycle(self._safe_int(chat_id), game.id)

    async def _clear_seat_state(self, game: Game, chat_id: ChatId) -> None:
        await self._player_manager.clear_seat_announcement(game, chat_id)
        await self._player_manager.clear_player_anchors(game)

    async def _deal_hole_cards(self, game: Game, chat_id: ChatId) -> None:
        await self._divide_cards(game, chat_id)

    async def _post_blinds_and_prepare_players(
        self, game: Game, chat_id: ChatId
    ) -> Optional[Player]:
        current_player = await self._round_rate.set_blinds(game, chat_id)
        self._player_manager.assign_role_labels(game)
        await self._stats_reporter.invalidate_players(game.players)
        return current_player

    async def _handle_post_start_notifications(
        self,
        *,
        context,
        game: Game,
        chat_id: ChatId,
        current_player: Optional[Player],
    ) -> None:
        game.chat_id = chat_id

        async with self._lock_manager.guard(self._stage_lock_key(chat_id), timeout=10):
            await self._view.send_player_role_anchors(game=game, chat_id=chat_id)

        self._record_game_start_action(game)

        if current_player:
            await self._send_turn_message(game, current_player, chat_id)

        context.chat_data[self._old_players_key] = [p.user_id for p in game.players]

    def _record_game_start_action(self, game: Game) -> None:
        action_str = "بازی شروع شد"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 5:
            game.last_actions.pop(0)

    async def _add_cards_to_table(
        self,
        *,
        count: int,
        game: Game,
        chat_id: ChatId,
        street_name: str,
        send_message: bool,
    ) -> None:
        game.chat_id = chat_id
        should_refresh_anchors = count > 0
        if count > 0:
            for _ in range(count):
                if game.remain_cards:
                    game.cards_table.append(game.remain_cards.pop())

        if should_refresh_anchors:
            await self._view.update_player_anchors_and_keyboards(game)

        if not send_message:
            return

        if game.board_message_id:
            try:
                await self._view.delete_message(chat_id, game.board_message_id)
                if game.board_message_id in game.message_ids_to_delete:
                    game.message_ids_to_delete.remove(game.board_message_id)
            except Exception:  # pragma: no cover - best effort cleanup
                self._logger.debug(
                    "Failed to delete board message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": game.board_message_id,
                    },
                )
            game.board_message_id = None

    def _stage_lock_key(self, chat_id: ChatId) -> str:
        return f"stage:{self._safe_int(chat_id)}"

    @staticmethod
    def _state_token(state: Optional[GameState]) -> str:
        name = getattr(state, "name", None)
        if isinstance(name, str):
            return name
        value = getattr(state, "value", None)
        if isinstance(value, str):
            return value
        return str(state)
