"""Unified start message management for poker games.

This module centralises all rendering and delivery of the pre-hand message that
lists the players who are ready, shows the countdown when it is active and, once
the hand begins, morphs into a compact turn summary.  The goal is to ensure the
group chat only ever sees a single message that is updated in-place instead of a
mixture of transient countdown and seating prompts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from pokerapp.entities import Game, GameState, Player, MAX_PLAYERS
from pokerapp.utils.locale_utils import to_persian_digits
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestCategory


try:  # pragma: no cover - prometheus_client optional
    from prometheus_client import Counter
except Exception:  # pragma: no cover - dependency optional in tests
    Counter = None  # type: ignore[assignment]


class _NoopCounter:  # pragma: no cover - simple fallback helper
    def inc(self, amount: float = 1.0) -> None:
        return None

    def labels(self, **_: object) -> "_NoopCounter":
        return self


if Counter is not None:  # pragma: no branch - initialise metrics when available
    _EDIT_SUCCESS_COUNTER = Counter(
        "poker_game_start_view_edit_success",
        "Successful updates to the game start anchor message.",
    )
    _EDIT_FAILURE_COUNTER = Counter(
        "poker_game_start_view_edit_failures",
        "Failed attempts to update the game start anchor message.",
        ("reason",),
    )
    _RATE_LIMIT_COUNTER = Counter(
        "poker_game_start_view_rate_limited",
        "Occurrences of rate limiting whilst updating the game start message.",
    )
else:  # pragma: no cover - prometheus_client missing in environment
    _EDIT_SUCCESS_COUNTER = _NoopCounter()
    _EDIT_FAILURE_COUNTER = _NoopCounter()
    _RATE_LIMIT_COUNTER = _NoopCounter()


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayerRosterEntry:
    """Immutable representation of a lobby player for countdown messages."""

    user_id: int
    seat_index: Optional[int]
    username: str
    chips: int = 0


@dataclass(frozen=True)
class CountdownSnapshot:
    """Snapshot of countdown state used for rendering updates."""

    chat_id: int
    remaining_seconds: int
    total_seconds: int
    player_count: int
    pot_size: int
    player_roster: tuple[PlayerRosterEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StageSnapshot:
    """Game stage information used when the countdown finishes."""

    stage: GameState
    current_player: Optional[Player]
    recent_actions: Sequence[str] = field(default_factory=tuple)


class GameStartView:
    """Render and deliver the unified pre-hand message."""

    _MIN_UPDATE_INTERVAL = 1.0
    _MAX_TIMEOUT_RETRIES = 3
    _INITIAL_TIMEOUT_BACKOFF = 1.0
    _MAX_TIMEOUT_BACKOFF = 8.0

    def __init__(
        self,
        messenger: MessagingService,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._messenger = messenger
        self._logger = logger or _LOGGER.getChild("manager")
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._last_update: dict[int, float] = {}
        self._anchor_message_id: dict[int, Optional[int]] = {}

    async def update_message(
        self,
        *,
        game: Game,
        countdown: Optional[CountdownSnapshot] = None,
        stage: Optional[StageSnapshot] = None,
        allow_create: bool = True,
    ) -> Optional[int]:
        """Send or edit the single start message for ``game``."""

        chat_id = self._safe_int(getattr(game, "chat_id", None))
        if chat_id == 0:
            self._logger.debug("Skipping start message update; chat_id missing")
            return None

        lock = await self._get_lock(chat_id)
        async with lock:
            await self._respect_rate_limit(chat_id)

            message_id = getattr(game, "ready_message_main_id", None)
            if message_id is None:
                cached_anchor = self._anchor_message_id.get(chat_id)
                if cached_anchor is not None:
                    message_id = cached_anchor
            text, markup = self._render_text(game, countdown=countdown, stage=stage)
            if text is None:
                return message_id

            context = {
                "operation": "game_start_view.update",
                "stage": getattr(game.state, "name", str(game.state)),
            }

            parse_mode = ParseMode.MARKDOWN_V2
            if message_id is None and not allow_create:
                self._logger.debug(
                    "Skipping message creation; allow_create disabled",
                    extra={"chat_id": chat_id},
                )
                return None

            timeout_attempts = 0
            backoff = self._INITIAL_TIMEOUT_BACKOFF
            while True:
                operation = "send" if message_id is None else "edit"
                try:
                    if message_id is None:
                        result = await self._messenger.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode=parse_mode,
                            reply_markup=markup,
                            disable_web_page_preview=True,
                            disable_notification=True,
                            request_category=RequestCategory.START_GAME,
                            context=context,
                        )
                        message_id = self._resolve_message_id(result)
                    else:
                        edit_result = await self._messenger.edit_message_text(
                            chat_id=chat_id,
                            message_id=int(message_id),
                            text=text,
                            reply_markup=markup,
                            parse_mode=parse_mode,
                            request_category=RequestCategory.START_GAME,
                            context=context,
                            current_game_id=getattr(game, "id", None),
                        )
                        resolved = self._resolve_message_id(edit_result)
                        if resolved is not None:
                            message_id = resolved

                    if message_id is not None:
                        try:
                            normalized_id = int(message_id)
                        except (TypeError, ValueError):
                            normalized_id = message_id
                    else:
                        normalized_id = None

                    game.ready_message_main_id = normalized_id
                    game.ready_message_main_text = text
                    game.ready_message_game_id = getattr(game, "id", None)
                    game.ready_message_stage = game.state
                    self._anchor_message_id[chat_id] = (
                        normalized_id if isinstance(normalized_id, int) else None
                    )
                    self._last_update[chat_id] = time.monotonic()
                    _EDIT_SUCCESS_COUNTER.inc()
                    return message_id

                except RetryAfter as exc:
                    _RATE_LIMIT_COUNTER.inc()
                    delay = getattr(exc, "retry_after", None)
                    if delay is None:
                        delay = getattr(exc, "value", 1)  # type: ignore[attr-defined]
                    try:
                        delay_float = max(0.0, float(delay))
                    except (TypeError, ValueError):
                        delay_float = 1.0
                    self._logger.warning(
                        "Rate limited while updating start message; retrying",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": "retry_after",
                            "operation": operation,
                            "retry_after": delay_float,
                        },
                    )
                    await asyncio.sleep(delay_float)
                    continue

                except TimedOut as exc:
                    timeout_attempts += 1
                    if timeout_attempts >= self._MAX_TIMEOUT_RETRIES:
                        _EDIT_FAILURE_COUNTER.labels(reason="timed_out").inc()
                        self._logger.error(
                            "Timed out updating start message; giving up",
                            extra={
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "error_type": "timed_out",
                                "operation": operation,
                                "attempts": timeout_attempts,
                            },
                            exc_info=True,
                        )
                        self._mark_message_stale(game, chat_id)
                        return None

                    delay = min(backoff, self._MAX_TIMEOUT_BACKOFF)
                    backoff = min(backoff * 2, self._MAX_TIMEOUT_BACKOFF)
                    self._logger.warning(
                        "Timed out updating start message; retrying",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": "timed_out",
                            "operation": operation,
                            "attempts": timeout_attempts,
                            "retry_delay": delay,
                        },
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)
                    continue

                except BadRequest as exc:
                    reason = self._classify_bad_request(exc)
                    _EDIT_FAILURE_COUNTER.labels(reason=reason).inc()
                    error_message = str(exc)
                    if reason == "message_not_modified":
                        self._logger.debug(
                            "Start message unchanged during edit",
                            extra={
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "error_type": reason,
                                "operation": operation,
                            },
                        )
                        return message_id

                    self._logger.warning(
                        "Telegram rejected message update",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": reason,
                            "operation": operation,
                            "error": error_message,
                        },
                    )
                    self._mark_message_stale(game, chat_id)
                    return None

                except NetworkError as exc:
                    _EDIT_FAILURE_COUNTER.labels(reason="network_error").inc()
                    self._logger.warning(
                        "Network error updating start message",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": "network_error",
                            "operation": operation,
                            "error": str(exc),
                        },
                        exc_info=True,
                    )
                    if allow_create or message_id is None:
                        # Only clear the cached anchor when we can create a
                        # replacement message. For transient network failures
                        # we must keep the existing message ID so callers that
                        # disallow creation (like the countdown manager) can
                        # retry editing once the connection recovers.
                        self._mark_message_stale(game, chat_id)
                    return None

                except TelegramError as exc:
                    _EDIT_FAILURE_COUNTER.labels(reason="telegram_error").inc()
                    self._logger.warning(
                        "Telegram API error during message update",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": "telegram_error",
                            "operation": operation,
                            "error": str(exc),
                        },
                        exc_info=True,
                    )
                    if allow_create or message_id is None:
                        # See comment above: retain the existing message ID
                        # when callers expect to edit in-place after transient
                        # Telegram API failures.
                        self._mark_message_stale(game, chat_id)
                    return None

                except Exception as exc:  # pragma: no cover - defensive coding
                    _EDIT_FAILURE_COUNTER.labels(reason="unexpected").inc()
                    self._logger.error(
                        "Unexpected error during message update",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": "unexpected",
                            "operation": operation,
                            "error": str(exc),
                        },
                        exc_info=True,
                    )
                    self._mark_message_stale(game, chat_id)
                    return None
            return message_id

    async def _get_lock(self, chat_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[chat_id] = lock
            return lock

    async def _respect_rate_limit(self, chat_id: int) -> None:
        last = self._last_update.get(chat_id)
        if last is None:
            return
        elapsed = time.monotonic() - last
        if elapsed < self._MIN_UPDATE_INTERVAL:
            await asyncio.sleep(self._MIN_UPDATE_INTERVAL - elapsed)

    @staticmethod
    def _resolve_message_id(result: object) -> Optional[int]:
        if result is None:
            return None
        if hasattr(result, "message_id"):
            try:
                return int(getattr(result, "message_id"))
            except (TypeError, ValueError):
                return None
        try:
            return int(result)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _classify_bad_request(exc: BadRequest) -> str:
        message = str(getattr(exc, "message", "") or str(exc)).lower()
        if "message is not modified" in message:
            return "message_not_modified"
        if "message to edit not found" in message:
            return "message_to_edit_not_found"
        return "bad_request"

    def _mark_message_stale(self, game: Game, chat_id: int) -> None:
        self._anchor_message_id[chat_id] = None
        if hasattr(game, "ready_message_main_id"):
            game.ready_message_main_id = None
        if hasattr(game, "ready_message_main_text"):
            game.ready_message_main_text = None
        if hasattr(game, "ready_message_game_id"):
            game.ready_message_game_id = None
        if hasattr(game, "ready_message_stage"):
            game.ready_message_stage = None

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        """Safely coerce value to int with fallback."""

        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _render_text(
        self,
        game: Game,
        *,
        countdown: Optional[CountdownSnapshot],
        stage: Optional[StageSnapshot],
    ) -> tuple[Optional[str], Optional[object]]:
        if stage is not None:
            return self._render_stage_text(game, stage), None

        snapshot = countdown or self._build_idle_snapshot(game)
        return self._render_waiting_text(snapshot)

    def _build_idle_snapshot(self, game: Game) -> CountdownSnapshot:
        ready_users = getattr(game, "ready_users", set()) or set()
        ready_players = [
            player
            for player in getattr(game, "players", [])
            if player and getattr(player, "user_id", None) in ready_users
        ]
        raw_roster: list[dict[str, Any]] = []
        for player in ready_players:
            if player is None:
                continue

            user_id = getattr(player, "user_id", None)
            chips_value = getattr(player, "money", None)
            if chips_value is None:
                wallet = getattr(player, "wallet", None)
                chips_value = getattr(wallet, "cached_value", None)
                if chips_value is None:
                    chips_value = getattr(wallet, "value", None)
                if chips_value is None:
                    chips_value = getattr(wallet, "_value", None)

            raw_roster.append(
                {
                    "user_id": user_id,
                    "username": (
                        getattr(player, "display_name", None)
                        or getattr(player, "full_name", None)
                        or getattr(player, "username", None)
                        or getattr(player, "mention_markdown", None)
                        or getattr(player, "mention", None)
                        or str(user_id)
                    ),
                    "chips": chips_value,
                    "seat_index": getattr(player, "seat_index", None),
                }
            )

        normalized_roster = self._normalize_roster(raw_roster)
        roster = tuple(
            PlayerRosterEntry(
                user_id=entry["user_id"],
                seat_index=None if entry["seat_index"] < 0 else entry["seat_index"],
                username=entry["username"],
                chips=entry["chips"],
            )
            for entry in normalized_roster
        )
        return CountdownSnapshot(
            chat_id=self._safe_int(getattr(game, "chat_id", None)),
            remaining_seconds=0,
            total_seconds=max(
                1,
                self._safe_int(getattr(game, "countdown_total", None), 30),
            ),
            player_count=len(roster),
            pot_size=self._safe_int(getattr(game, "pot", None)),
            player_roster=roster,
        )

    def _render_waiting_text(
        self, snapshot: CountdownSnapshot
    ) -> tuple[str, Optional[InlineKeyboardMarkup]]:
        remaining = max(0, int(snapshot.remaining_seconds))
        total = max(1, int(snapshot.total_seconds))
        progress_bar = self._render_progress_bar(remaining, total)

        remaining_fa = self._escape_markdown_v2(to_persian_digits(remaining))
        ready_fa = self._escape_markdown_v2(to_persian_digits(snapshot.player_count))
        max_players_fa = self._escape_markdown_v2(to_persian_digits(MAX_PLAYERS))
        pot_fa = self._escape_markdown_v2(to_persian_digits(snapshot.pot_size))

        header = "🚀 بازی در آستانه شروع"
        if remaining > 0:
            header = f"🚀 شروع بازی ({remaining_fa}s)"
        header = self._escape_markdown_v2(header)

        countdown_lines = [
            header,
            "",
            f"{progress_bar} {remaining_fa} ثانیه مانده",
            "",
        ]

        countdown_lines.extend(self._build_player_list_section(snapshot))

        countdown_lines.extend(
            [
                "",
                f"📊 {ready_fa}/{max_players_fa} بازیکن آماده",
                f"💰 پات: {pot_fa} سکه",
                "",
                "⚡ برای پیوستن /join را بزنید\\!",
            ]
        )

        keyboard = [
            [InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game")]
        ]
        if snapshot.player_count >= 2:
            keyboard[0].append(
                InlineKeyboardButton(text="شروع بازی", callback_data="start_game")
            )

        markup = InlineKeyboardMarkup(keyboard)

        return "\n".join(countdown_lines).strip(), markup

    def _render_stage_text(self, game: Game, stage: StageSnapshot) -> Optional[str]:
        player = stage.current_player
        if player is None:
            return "🎮 بازی شروع شد"

        seat_number = (player.seat_index or 0) + 1
        seat_fa = self._escape_markdown_v2(to_persian_digits(seat_number))
        display_name = (
            getattr(player, "display_name", None)
            or getattr(player, "full_name", None)
            or getattr(player, "username", None)
            or str(getattr(player, "user_id", "?"))
        )
        safe_name = self._escape_markdown_v2(str(display_name))
        user_id = self._safe_int(getattr(player, "user_id", None))
        player_name = f"[{safe_name}](tg://user?id={user_id})"

        stage_labels = {
            GameState.ROUND_PRE_FLOP: "پری‌فلاپ",
            GameState.ROUND_FLOP: "فلاپ",
            GameState.ROUND_TURN: "ترن",
            GameState.ROUND_RIVER: "ریور",
        }
        stage_name = stage_labels.get(stage.stage, "پری‌فلاپ")

        board_cards = getattr(game, "cards_table", []) or []
        if not board_cards:
            board_text = "—"
        else:
            board_text = " ".join(
                self._escape_markdown_v2(str(card)) for card in board_cards
            )

        pot = self._escape_markdown_v2(
            to_persian_digits(self._safe_int(getattr(game, "pot", None)))
        )
        stack_value = getattr(player, "money", None)
        if stack_value is None:
            player_stack = getattr(player, "wallet", None)
            stack_value = getattr(player_stack, "cached_value", None)
            if stack_value is None:
                stack_value = getattr(player_stack, "_value", None)
        stack_fa = self._escape_markdown_v2(
            to_persian_digits(self._safe_int(stack_value))
        )

        round_rate = self._escape_markdown_v2(
            to_persian_digits(self._safe_int(getattr(player, "round_rate", None)))
        )
        max_round = self._escape_markdown_v2(
            to_persian_digits(self._safe_int(getattr(game, "max_round_rate", None)))
        )

        lines = [
            f"🎯 نوبت: {player_name} (صندلی {seat_fa})",
            f"🎰 مرحله بازی: {self._escape_markdown_v2(stage_name)}",
            "",
            f"🃏 کارت‌های میز: {board_text}",
            f"💰 پات فعلی: {pot}$",
            f"💵 موجودی شما: {stack_fa}$",
            f"🎲 شرط فعلی شما: {round_rate}$",
            f"📈 حداکثر شرط این دور: {max_round}$",
        ]

        actions = [action for action in stage.recent_actions if action]
        if actions:
            lines.append("")
            lines.append("🎬 اکشن‌های اخیر:")
            for action in actions:
                escaped = self._escape_markdown_v2(str(action))
                lines.append(f"• {escaped}")

        return "\n".join(lines)

    def _render_progress_bar(self, remaining: int, total: int) -> str:
        """Render a text-based progress bar.

        Uses Unicode block chars (█ and ░) which are safe for MarkdownV2.
        """

        width = 20
        total = max(total, 1)
        remaining = max(0, min(remaining, total))
        ratio = remaining / total
        filled = max(0, min(width, int(round(ratio * width))))
        empty = max(0, width - filled)
        return ("█" * filled) + ("░" * empty)

    def _build_player_list_section(self, snapshot: CountdownSnapshot) -> list[str]:
        roster = snapshot.player_roster
        lines = ["👥 *لیست بازیکنان آماده*"]
        if not roster:
            lines.append("هیچ بازیکنی آماده نیست")
            return lines

        for index, entry in enumerate(roster, start=1):
            index_fa = self._escape_markdown_v2(to_persian_digits(index))
            if entry.seat_index is None:
                seat_label = "صندلی نامشخص"
            else:
                seat_label = f"صندلی {to_persian_digits(entry.seat_index + 1)}"
            seat_label = self._escape_markdown_v2(seat_label)
            link = f"[{entry.username}](tg://user?id={entry.user_id})"
            lines.append(f"{index_fa}\\. \\({seat_label}\\) {link} 🟢")

        return lines

    def _normalize_roster(self, roster: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sanitise raw roster entries into a Markdown-safe structure.

        Parameters
        ----------
        roster:
            Iterable of dictionaries describing players. Each dictionary may
            contain arbitrary keys such as ``user_id``, ``username``, ``chips``
            or ``seat_index`` gathered from different sources (database rows,
            entity objects, etc.). The method tolerates missing keys and
            gracefully skips malformed entries.

        Returns
        -------
        list of dict
            A list of dictionaries sorted by ``seat_index``. Every dictionary
            is guaranteed to include the keys ``user_id`` (int), ``username``
            (MarkdownV2 escaped string), ``chips`` (int) and ``seat_index``
            (int; ``-1`` represents unknown seats).
        """

        if not roster:
            return []

        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()

        for raw_entry in roster:
            if not isinstance(raw_entry, dict):
                continue

            user_id_value = raw_entry.get("user_id")
            user_id = self._safe_int(user_id_value, default=0)
            if user_id <= 0 or user_id in seen:
                continue

            seat_index_value = raw_entry.get("seat_index")
            seat_index = self._safe_int(seat_index_value, default=-1)

            raw_username = (
                raw_entry.get("username")
                or raw_entry.get("display_name")
                or raw_entry.get("full_name")
                or raw_entry.get("mention")
                or raw_entry.get("mention_markdown")
                or f"Player {user_id}"
            )
            username = self._escape_markdown_v2(str(raw_username))

            chips_source: Any = raw_entry.get("chips")
            if chips_source is None:
                chips_source = raw_entry.get("money")
            if chips_source is None:
                wallet = raw_entry.get("wallet")
                if isinstance(wallet, dict):
                    chips_source = (
                        wallet.get("cached_value")
                        or wallet.get("value")
                        or wallet.get("_value")
                    )
                else:
                    chips_source = getattr(wallet, "cached_value", None)
                    if chips_source is None:
                        chips_source = getattr(wallet, "value", None)
                    if chips_source is None:
                        chips_source = getattr(wallet, "_value", None)

            chips = self._safe_int(chips_source, default=0)

            normalized.append(
                {
                    "user_id": user_id,
                    "username": username,
                    "chips": chips,
                    "seat_index": seat_index,
                }
            )
            seen.add(user_id)

        normalized.sort(key=lambda entry: (entry["seat_index"] < 0, entry["seat_index"]))
        return normalized

    @staticmethod
    def _escape_markdown_v2(text: str) -> str:
        special = "_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{char}" if char in special else char for char in text)

