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
from typing import Optional, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from pokerapp.entities import Game, GameState, Player, MAX_PLAYERS
from pokerapp.utils.locale_utils import to_persian_digits
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestCategory


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayerRosterEntry:
    """Immutable representation of a lobby player."""

    user_id: int
    seat_index: Optional[int]
    display_name: str


@dataclass(slots=True)
class CountdownSnapshot:
    """Snapshot of countdown state used for rendering updates."""

    chat_id: int
    remaining_seconds: int
    total_seconds: int
    player_count: int
    pot_size: int
    player_roster: tuple[PlayerRosterEntry, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class StageSnapshot:
    """Game stage information used when the countdown finishes."""

    stage: GameState
    current_player: Optional[Player]
    recent_actions: Sequence[str] = field(default_factory=tuple)


class GameStartView:
    """Render and deliver the unified pre-hand message."""

    _MIN_UPDATE_INTERVAL = 1.0

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

            except BadRequest as exc:
                self._logger.warning(
                    "Telegram rejected message update (bad request)",
                    extra={
                        "chat_id": chat_id,
                        "error": str(exc),
                        "operation": "send" if message_id is None else "edit",
                        "message_id": message_id,
                    },
                )
                self._clear_cached_message(game)
                return None

            except TelegramError as exc:
                self._logger.warning(
                    "Telegram API error during message update",
                    extra={
                        "chat_id": chat_id,
                        "error": str(exc),
                        "operation": "send" if message_id is None else "edit",
                        "message_id": message_id,
                    },
                )
                self._clear_cached_message(game)
                return None

            except Exception as exc:
                self._logger.error(
                    "Unexpected error during message update",
                    extra={
                        "chat_id": chat_id,
                        "error": str(exc),
                        "operation": "send" if message_id is None else "edit",
                    },
                    exc_info=True,
                )
                self._clear_cached_message(game)
                return None

            if message_id is not None:
                try:
                    normalized_id = int(message_id)
                except (TypeError, ValueError):
                    normalized_id = message_id
                game.ready_message_main_id = normalized_id
                game.ready_message_main_text = text
                game.ready_message_game_id = getattr(game, "id", None)
                game.ready_message_stage = game.state

            self._last_update[chat_id] = time.monotonic()
            return message_id

    @staticmethod
    def _clear_cached_message(game: Game) -> None:
        if hasattr(game, "ready_message_main_id"):
            game.ready_message_main_id = None
        if hasattr(game, "ready_message_main_text"):
            game.ready_message_main_text = None
        if hasattr(game, "ready_message_game_id"):
            game.ready_message_game_id = None
        if hasattr(game, "ready_message_stage"):
            game.ready_message_stage = None

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
    def _safe_int(value: object, default: int = 0) -> int:
        """Safely coerce ``value`` to ``int`` with a fallback."""

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
        roster = self._normalize_roster(ready_players)
        return CountdownSnapshot(
            chat_id=self._safe_int(getattr(game, "chat_id", None), 0),
            remaining_seconds=0,
            total_seconds=max(
                1,
                self._safe_int(getattr(game, "countdown_total", None), 30),
            ),
            player_count=len(roster),
            pot_size=self._safe_int(getattr(game, "pot", None), 0),
            player_roster=roster,
        )

    def _render_waiting_text(
        self, snapshot: CountdownSnapshot
    ) -> tuple[str, Optional[InlineKeyboardMarkup]]:
        remaining = max(0, int(snapshot.remaining_seconds))
        total = max(1, int(snapshot.total_seconds))
        progress_bar = self._render_progress_bar(remaining, total)

        remaining_fa = to_persian_digits(remaining)

        header = "🚀 بازی در آستانه شروع"
        if remaining > 0:
            header = f"🚀 شروع بازی ({remaining_fa}s)"

        countdown_lines = [
            self._escape_markdown_v2(header),
            "",
            f"{progress_bar} {remaining_fa} ثانیه مانده",
            "",
        ]

        countdown_lines.extend(self._build_player_list_section(snapshot))

        ready_fa = to_persian_digits(snapshot.player_count)
        max_players_fa = to_persian_digits(MAX_PLAYERS)
        pot_fa = to_persian_digits(snapshot.pot_size)

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
        seat_fa = to_persian_digits(seat_number)
        display_name = (
            getattr(player, "display_name", None)
            or getattr(player, "full_name", None)
            or getattr(player, "username", None)
            or str(getattr(player, "user_id", "?"))
        )
        safe_name = self._escape_markdown_v2(str(display_name))
        user_id = getattr(player, "user_id", 0)
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            user_id = 0
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
            board_text = " ".join(self._escape_markdown_v2(str(card)) for card in board_cards)

        pot = to_persian_digits(int(getattr(game, "pot", 0)))
        stack_value = getattr(player, "money", None)
        if stack_value is None:
            player_stack = getattr(player, "wallet", None)
            stack_value = getattr(player_stack, "cached_value", None)
            if stack_value is None:
                stack_value = getattr(player_stack, "_value", None)
        if stack_value is None:
            stack_value = 0
        stack_fa = to_persian_digits(int(stack_value or 0))

        round_rate = to_persian_digits(int(getattr(player, "round_rate", 0)))
        max_round = to_persian_digits(int(getattr(game, "max_round_rate", 0)))

        lines = [
            f"🎯 نوبت: {player_name} (صندلی {seat_fa})",
            f"🎰 مرحله بازی: {self._escape_markdown_v2(stage_name)}",
            "",
            f"🃏 کارت‌های میز: {self._escape_markdown_v2(board_text)}",
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
            index_fa = to_persian_digits(index)
            if entry.seat_index is None:
                seat_label = "صندلی نامشخص"
            else:
                seat_label = f"صندلی {to_persian_digits(entry.seat_index + 1)}"
            seat_label = self._escape_markdown_v2(seat_label)
            display_name = self._escape_markdown_v2(entry.display_name)
            link = f"[{display_name}](tg://user?id={entry.user_id})"
            lines.append(f"{index_fa}\\. \\({seat_label}\\) {link} 🟢")

        return lines

    def _normalize_roster(
        self, roster: Optional[Sequence[object]]
    ) -> tuple[PlayerRosterEntry, ...]:
        if not roster:
            return ()

        normalized: list[PlayerRosterEntry] = []
        seen: set[int] = set()

        for item in roster:
            if item is None:
                continue
            user_id = getattr(item, "user_id", None)
            if user_id is None:
                continue
            try:
                user_id_int = int(user_id)
            except (TypeError, ValueError):
                continue
            if user_id_int in seen:
                continue

            seat_index = getattr(item, "seat_index", None)
            if seat_index is not None:
                try:
                    seat_index = int(seat_index)
                except (TypeError, ValueError):
                    seat_index = None

            display_name = (
                getattr(item, "display_name", None)
                or getattr(item, "mention_markdown", None)
                or getattr(item, "full_name", None)
                or getattr(item, "username", None)
                or str(user_id_int)
            )

            normalized.append(
                PlayerRosterEntry(
                    user_id=user_id_int,
                    seat_index=seat_index,
                    display_name=str(display_name),
                )
            )
            seen.add(user_id_int)

        normalized.sort(key=lambda entry: (entry.seat_index is None, entry.seat_index))
        return tuple(normalized)

    @staticmethod
    def _escape_markdown_v2(text: str) -> str:
        special = "_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{char}" if char in special else char for char in text)

