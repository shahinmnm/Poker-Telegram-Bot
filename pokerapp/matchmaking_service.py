"""Matchmaking and hand lifecycle helpers for the poker engine."""

from __future__ import annotations

import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from pokerapp.cards import get_cards
from pokerapp.entities import ChatId, Game, GameState, Money, Player, PlayerState, Wallet
from pokerapp.config import Config, get_game_constants
from pokerapp.player_manager import PlayerManager
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.stats_reporter import StatsReporter
from pokerapp.utils.request_metrics import RequestMetrics
from pokerapp.lock_manager import LockManager


_CONSTANTS = get_game_constants()
_REDIS_KEYS = _CONSTANTS.redis_keys
if isinstance(_REDIS_KEYS, dict):
    _ENGINE_KEYS = _REDIS_KEYS.get("engine", {})
    if not isinstance(_ENGINE_KEYS, dict):
        _ENGINE_KEYS = {}
else:
    _ENGINE_KEYS = {}

_STAGE_LOCK_PREFIX = _ENGINE_KEYS.get("stage_lock_prefix", "stage:")

_LOCKS_SECTION = _CONSTANTS.section("locks")
_CATEGORY_TIMEOUTS = {}
if isinstance(_LOCKS_SECTION, dict):
    candidate_timeouts = _LOCKS_SECTION.get("category_timeouts_seconds")
    if isinstance(candidate_timeouts, dict):
        _CATEGORY_TIMEOUTS = candidate_timeouts


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


_STAGE_LOCK_TIMEOUT_SECONDS = _positive_float(
    _CATEGORY_TIMEOUTS.get("engine_stage"), 25.0
)


class _DebugWallet(Wallet):
    async def add_daily(self, amount: Money) -> Money:
        return 0

    async def has_daily_bonus(self) -> bool:
        return False

    async def inc(self, amount: Money = 0) -> Money:
        return 0

    async def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        return None

    async def authorized_money(self, game_id: str) -> Money:
        return 0

    async def authorize(self, game_id: str, amount: Money) -> None:
        return None

    async def authorize_all(self, game_id: str) -> Money:
        return 0

    async def value(self) -> Money:
        return 0

    async def approve(self, game_id: str) -> None:
        return None

    async def cancel(self, game_id: str) -> None:
        return None


_DEBUG_WALLET = _DebugWallet()
_DEBUG_DEALER_USER_ID = "debug-dealer"


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
        config: Optional[Config] = None,
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
        self._config = config or Config()
        self._stage_lock_timeout = _STAGE_LOCK_TIMEOUT_SECONDS

    def _log_extra(
        self,
        *,
        stage: str,
        game: Optional[Game] = None,
        chat_id: Optional[ChatId] = None,
        env_config_missing: Optional[Any] = None,
        **extra_fields: Any,
    ) -> Dict[str, Any]:
        resolved_chat_id: Optional[int]
        if chat_id is not None:
            try:
                resolved_chat_id = self._safe_int(chat_id)
            except Exception:  # pragma: no cover - defensive fallback
                resolved_chat_id = chat_id  # type: ignore[assignment]
        elif game is not None and getattr(game, "chat_id", None) is not None:
            resolved_chat_id = getattr(game, "chat_id")  # type: ignore[assignment]
        else:
            resolved_chat_id = None

        dealer_index = -1
        players_ready = 0
        if game is not None:
            dealer_index = getattr(game, "dealer_index", -1)
            if hasattr(game, "seated_players"):
                try:
                    players_ready = len(game.seated_players())
                except Exception:  # pragma: no cover - defensive fallback
                    players_ready = getattr(game, "seated_count", lambda: 0)()

        extra: Dict[str, Any] = {
            "category": "matchmaking",
            "stage": stage,
            "chat_id": resolved_chat_id,
            "game_id": getattr(game, "id", None) if game is not None else None,
            "dealer_index": dealer_index,
            "players_ready": players_ready,
            "env_config_missing": list(env_config_missing or []),
        }

        if self._logger.isEnabledFor(logging.DEBUG) and game is not None:
            snapshot = []
            try:
                players = game.seated_players()
            except Exception:  # pragma: no cover - fallback to attribute access
                players = list(getattr(game, "players", []))
            for player in players:
                snapshot.append(
                    {
                        "user_id": getattr(player, "user_id", None),
                        "seat_index": getattr(player, "seat_index", None),
                        "stack": getattr(player, "stack", None),
                        "total_bet": getattr(player, "total_bet", None),
                        "state": getattr(getattr(player, "state", None), "name", None),
                    }
                )
            extra.update(
                {
                    "debug_stage": getattr(getattr(game, "state", None), "name", None),
                    "debug_pot": getattr(game, "pot", None),
                    "debug_player_snapshot": snapshot,
                }
            )

        extra.update(extra_fields)
        return extra

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
                self._logger.warning(
                    "Unexpected state %s during stage progression",
                    game.state,
                    extra=self._log_extra(
                        stage="progress-stage",
                        game=game,
                        chat_id=chat_id,
                        unexpected_state=getattr(game.state, "name", game.state),
                    ),
                )
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
                # Reinitialize the deck using the same logic as a new hand, which
                # also shuffles the cards to keep gameplay continuity.
                game.remain_cards = get_cards()
                try:
                    resolved_chat_id = self._safe_int(chat_id)
                except Exception:  # pragma: no cover - defensive fallback
                    resolved_chat_id = chat_id
                try:
                    seated_count = len(game.seated_players())
                except Exception:  # pragma: no cover - defensive fallback
                    seated_count = len(getattr(game, "players", []))
                self._logger.debug(
                    "Refilled deck mid-hand for chat %s with %s cards remaining (seated_players=%s)",
                    resolved_chat_id,
                    len(game.remain_cards),
                    seated_count,
                )

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

    def _ensure_dealer_position(self, game: Game) -> bool:
        allow_empty_dealer = bool(getattr(self._config, "ALLOW_EMPTY_DEALER", False))
        if not hasattr(game, "dealer_index"):
            game.dealer_index = -1

        new_dealer_index = game.advance_dealer()
        if new_dealer_index == -1:
            new_dealer_index = game.next_occupied_seat(-1)
            game.dealer_index = new_dealer_index

        if game.dealer_index == -1:
            if allow_empty_dealer:
                fallback_player = self._ensure_debug_dealer(game)
                self._logger.debug(
                    "Using debug dealer fallback: assigned seat %s (created_dummy=%s)",
                    game.dealer_index,
                    bool(fallback_player),
                )
                return True
            self._logger.warning(
                "Cannot start game without an occupied dealer seat",
                extra=self._log_extra(
                    stage="dealer-check",
                    game=game,
                    chat_id=getattr(game, "chat_id", None),
                ),
            )
            return False
        return True

    def _ensure_debug_dealer(self, game: Game) -> Optional[Player]:
        if game.players:
            first_player = game.players[0]
            seat_index = getattr(first_player, "seat_index", None)
            if isinstance(seat_index, int) and seat_index >= 0:
                game.dealer_index = seat_index
            else:
                game.dealer_index = 0
            return None

        existing_dummy: Optional[Player] = getattr(game, "_debug_dummy_dealer", None)
        if isinstance(existing_dummy, Player):
            game.dealer_index = 0
            game.seats[0] = existing_dummy
            existing_dummy.is_dealer = True
            existing_dummy.seat_index = 0
            return existing_dummy

        dummy_player = Player(
            user_id=_DEBUG_DEALER_USER_ID,
            mention_markdown="Debug Dealer",
            wallet=_DEBUG_WALLET,
            ready_message_id="",
            seat_index=0,
        )
        dummy_player.is_dealer = True
        game.seats[0] = dummy_player
        game.dealer_index = 0
        setattr(game, "_debug_dummy_dealer", dummy_player)
        return dummy_player

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

        async with self._trace_lock_guard(
            lock_key=self._stage_lock_key(chat_id),
            chat_id=chat_id,
            game=game,
            stage_label="stage_lock:send_player_role_anchors",
            event_stage_label="send_player_role_anchors",
            timeout=self._stage_lock_timeout,
        ):
            start_ts = asyncio.get_event_loop().time()
            try:
                await asyncio.wait_for(
                    self._view.send_player_role_anchors(game=game, chat_id=chat_id),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - start_ts
                self._logger.error(
                    "[DIAG] send_player_role_anchors TIMEOUT after %.2fs inside Stage Lock",
                    elapsed,
                    extra=self._log_extra(stage="send_player_role_anchors_timeout", game=game, chat_id=chat_id)
                )
                st = "".join(traceback.format_stack())
                self._logger.error("[DIAG] Stacktrace inside Stage Lock:\n%s", st)
                try:
                    await self._lock_manager._record_long_hold_context(
                        lock_key=self._stage_lock_key(chat_id),
                        game=game,
                        elapsed=elapsed,
                        stacktrace=st
                    )
                except Exception:
                    self._logger.exception("[DIAG] Failed to record long-hold context")
                raise

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
        return f"{_STAGE_LOCK_PREFIX}{self._safe_int(chat_id)}"

    @asynccontextmanager
    async def _trace_lock_guard(
        self,
        *,
        lock_key: str,
        chat_id: ChatId,
        stage_label: str,
        event_stage_label: str,
        timeout: float,
        game: Optional[Game] = None,
    ):
        """Serialize stage work with structured tracing metadata."""

        try:
            resolved_chat = self._safe_int(chat_id)
        except Exception:  # pragma: no cover - defensive fallback
            resolved_chat = None

        lock_manager = self._lock_manager
        resolve_level = getattr(lock_manager, "_resolve_level", None)
        if callable(resolve_level):
            lock_level = resolve_level(lock_key, override=None)
        else:  # pragma: no cover - compatibility fallback
            lock_level = None

        context_payload: Optional[Dict[str, Any]]
        build_context = getattr(lock_manager, "_build_context_payload", None)
        if callable(build_context):
            context_payload = build_context(  # type: ignore[misc]
                lock_key,
                lock_level,
                additional={
                    "chat_id": resolved_chat,
                    "game_id": getattr(game, "id", None) if game else None,
                    "stage_label": stage_label,
                    "event_stage_label": event_stage_label,
                },
            )
        else:  # pragma: no cover - compatibility fallback
            context_payload = {
                "chat_id": resolved_chat,
                "game_id": getattr(game, "id", None) if game else None,
                "stage_label": stage_label,
                "event_stage_label": event_stage_label,
                "lock_level": lock_level,
            }

        trace_kwargs = {"timeout": timeout}
        if context_payload is not None:
            trace_kwargs["context"] = context_payload

        guard_method = getattr(lock_manager, "trace_guard", None)
        if not callable(guard_method):  # pragma: no cover - compatibility fallback
            guard_method = getattr(lock_manager, "guard")

        try:
            async with guard_method(lock_key, **trace_kwargs):
                yield
                return
        except TypeError as exc:
            if "unexpected keyword argument 'context'" not in str(exc) or "context" not in trace_kwargs:
                raise

        fallback_kwargs = dict(trace_kwargs)
        fallback_kwargs.pop("context", None)
        async with guard_method(lock_key, **fallback_kwargs):
            yield

    @staticmethod
    def _state_token(state: Optional[GameState]) -> str:
        name = getattr(state, "name", None)
        if isinstance(name, str):
            return name
        value = getattr(state, "value", None)
        if isinstance(value, str):
            return value
        return str(state)
