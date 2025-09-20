"""Async messaging and flow control for the aiogram based poker bot.

This module provides a high level orchestration layer that fulfils the
requirements described in the rewrite brief.  The :class:`RequestManager`
serialises every outgoing Telegram Bot API call, deduplicates identical edits
and guards against empty messages.  :class:`PokerMessagingOrchestrator` keeps
track of the current table state, including anchored player messages, the
shared turn message and the seat voting flow used between hands.

The implementation relies on aiogram primitives but is deliberately decoupled
from the wider application so it can be unit tested in isolation.  Existing
model or controller layers can integrate with the orchestrator by invoking the
documented coroutine methods.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from cachetools import LRUCache, TTLCache

from pokerapp.utils.debug_trace import trace_telegram_api_call


logger = logging.getLogger(__name__)


_CARD_SPACER = "     "  # five spaces to visually separate board cards
_DEFAULT_TURN_NOTICE = "دکمه‌های نوبت شما در پیام اختصاصی فعال شده‌اند."
_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u200e",
    "\u200f",
    "\u2060",
    "\u2061",
    "\u2062",
    "\u2063",
}


class GameState(Enum):
    """Enumeration describing the lifecycle of a table."""

    WAITING = auto()
    VOTING = auto()
    IN_HAND = auto()
    SHOWDOWN = auto()


@dataclass(slots=True)
class ActionButton:
    """Simple inline keyboard button descriptor."""

    label: str
    callback_data: str


@dataclass(slots=True)
class PlayerInfo:
    """Minimal description of a player required for messaging."""

    player_id: int
    name: str
    seat_number: int
    roles: Sequence[str] = field(default_factory=tuple)
    buttons: Sequence[ActionButton] = field(default_factory=tuple)


@dataclass(slots=True)
class AnchorMessage:
    """Track anchor message metadata for a player."""

    player: PlayerInfo
    message_id: Optional[int] = None
    base_text: str = ""


@dataclass(slots=True)
class TurnState:
    """All fields displayed in the shared turn message."""

    board_cards: Sequence[str] = field(default_factory=tuple)
    pot: int = 0
    stack: int = 0
    current_bet: int = 0
    max_bet: int = 0
    stage: Optional[str] = None
    turn_indicator: Optional[str] = None
    notice: Optional[str] = None


@dataclass(slots=True)
class _PendingTurnEdit:
    text: str
    reply_markup: Any
    payload_hash: str
    countdown_tick: bool = False


def _has_visible_text(text: Optional[str]) -> bool:
    """Return ``True`` when ``text`` contains visible, non-whitespace characters."""

    if text is None:
        return False
    for char in text:
        if char.isspace():
            continue
        if char in _INVISIBLE_CHARS:
            continue
        return True
    return False


def _serialize_markup(markup: Any) -> str:
    """Return a deterministic string representation of ``markup``."""

    if markup is None:
        return ""
    serializer = getattr(markup, "model_dump", None)
    if callable(serializer):
        try:
            return json.dumps(serializer(), sort_keys=True, ensure_ascii=False)
        except TypeError:
            pass
    serializer = getattr(markup, "to_python", None)
    if callable(serializer):
        try:
            return json.dumps(serializer(), sort_keys=True, ensure_ascii=False)
        except TypeError:
            pass
    serializer = getattr(markup, "to_dict", None)
    if callable(serializer):
        try:
            return json.dumps(serializer(), sort_keys=True, ensure_ascii=False)
        except TypeError:
            pass
    try:
        return json.dumps(markup, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        return repr(markup)


def _content_hash(text: Optional[str], reply_markup: Any) -> str:
    payload = json.dumps(
        {
            "text": text or "",
            "reply_markup": _serialize_markup(reply_markup),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RequestManager:
    """Centralise all Telegram Bot API interactions with caching and locking."""

    def __init__(
        self,
        bot: Bot,
        *,
        cache_ttl: int = 3,
        cache_maxsize: int = 500,
        queue_delay: float = 0.075,
    ) -> None:
        self._bot = bot
        self._cache: TTLCache[tuple[int, int, str], bool] = TTLCache(
            maxsize=cache_maxsize,
            ttl=cache_ttl,
        )
        self._cache_lock = asyncio.Lock()
        self._locks: Dict[tuple[int, int], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._queue: asyncio.Queue[Optional[tuple[asyncio.Future, Any]]] = (
            asyncio.Queue()
        )
        self._queue_delay = max(0.0, queue_delay)
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())

    async def close(self) -> None:
        if self._worker is None:
            return
        await self._queue.join()
        await self._queue.put(None)
        await self._worker
        self._worker = None

    async def send_message(
        self,
        *,
        chat_id: int,
        text: Optional[str],
        reply_markup: Any = None,
        **params: Any,
    ) -> Optional[Message]:
        if not _has_visible_text(text):
            logger.info(
                "SKIP SEND: empty or invisible content for %s, msg %s",
                chat_id,
                None,
            )
            return None

        future: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _execute() -> Optional[Message]:
            lock = await self._acquire_lock(chat_id, 0)
            async with lock:
                try:
                    trace_telegram_api_call(
                        "sendMessage",
                        chat_id=chat_id,
                        message_id=None,
                        text=text,
                        reply_markup=reply_markup,
                    )
                    message: Message = await self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        **params,
                    )
                except TelegramBadRequest as exc:  # pragma: no cover - network error
                    logger.warning(
                        "TelegramBadRequest during send_message: %s", exc
                    )
                    return None

                message_id = getattr(message, "message_id", None)
                if message_id is not None:
                    await self._remember(chat_id, int(message_id), text, reply_markup)
                return message

        await self._queue.put((future, _execute))
        await self.start()
        return await future

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: Optional[str],
        reply_markup: Any = None,
        skip_cache: bool = False,
        **params: Any,
    ) -> Optional[int]:
        if not _has_visible_text(text):
            logger.info(
                "SKIP SEND: empty or invisible content for %s, msg %s",
                chat_id,
                message_id,
            )
            return message_id

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        payload_hash = _content_hash(text, reply_markup)

        async def _execute() -> Optional[int]:
            if not skip_cache:
                cached = await self._is_cached(chat_id, message_id, payload_hash)
                if cached:
                    logger.info(
                        "SKIP EDIT: identical content for %s, msg %s",
                        chat_id,
                        message_id,
                    )
                    return message_id

            lock = await self._acquire_lock(chat_id, message_id)
            async with lock:
                if not skip_cache:
                    cached = await self._is_cached(chat_id, message_id, payload_hash)
                    if cached:
                        logger.info(
                            "SKIP EDIT: identical content for %s, msg %s",
                            chat_id,
                            message_id,
                        )
                        return message_id
                try:
                    trace_telegram_api_call(
                        "editMessageText",
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=reply_markup,
                    )
                    result = await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=reply_markup,
                        **params,
                    )
                except TelegramBadRequest as exc:  # pragma: no cover - network error
                    logger.warning(
                        "TelegramBadRequest during edit_message_text: %s",
                        exc,
                    )
                    return message_id

                await self._remember(chat_id, message_id, text, reply_markup)
                if hasattr(result, "message_id"):
                    return int(result.message_id)
                return message_id

        await self._queue.put((future, _execute))
        await self.start()
        return await future

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: Any = None,
        skip_cache: bool = False,
        **params: Any,
    ) -> bool:
        payload_hash = _content_hash(None, reply_markup)
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _execute() -> bool:
            if not skip_cache:
                cached = await self._is_cached(chat_id, message_id, payload_hash)
                if cached:
                    logger.info(
                        "SKIP EDIT: identical content for %s, msg %s",
                        chat_id,
                        message_id,
                    )
                    return True

            lock = await self._acquire_lock(chat_id, message_id)
            async with lock:
                if not skip_cache:
                    cached = await self._is_cached(chat_id, message_id, payload_hash)
                    if cached:
                        logger.info(
                            "SKIP EDIT: identical content for %s, msg %s",
                            chat_id,
                            message_id,
                        )
                        return True

                try:
                    await self._bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=reply_markup,
                        **params,
                    )
                except TelegramBadRequest as exc:  # pragma: no cover - network error
                    logger.warning(
                        "TelegramBadRequest during edit_message_reply_markup: %s",
                        exc,
                    )
                    return False

                await self._remember(chat_id, message_id, None, reply_markup)
                return True

        await self._queue.put((future, _execute))
        await self.start()
        return await future

    async def delete_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        **params: Any,
    ) -> bool:
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _execute() -> bool:
            lock = await self._acquire_lock(chat_id, message_id)
            async with lock:
                try:
                    trace_telegram_api_call(
                        "deleteMessage",
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                    await self._bot.delete_message(
                        chat_id=chat_id,
                        message_id=message_id,
                        **params,
                    )
                except TelegramBadRequest as exc:  # pragma: no cover - network error
                    logger.warning(
                        "TelegramBadRequest during delete_message: %s",
                        exc,
                    )
                await self._forget(chat_id, message_id)
                return True

        await self._queue.put((future, _execute))
        await self.start()
        return await future

    async def _worker_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            future, action = item
            try:
                if self._queue_delay:
                    await asyncio.sleep(self._queue_delay)
                result = await action()
                if not future.done():
                    future.set_result(result)
            except Exception as exc:  # pragma: no cover - unexpected path
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _remember(
        self,
        chat_id: int,
        message_id: int,
        text: Optional[str],
        reply_markup: Any,
    ) -> None:
        key = (int(chat_id), int(message_id), _content_hash(text, reply_markup))
        async with self._cache_lock:
            self._cache[key] = True

    async def _is_cached(
        self,
        chat_id: int,
        message_id: int,
        payload_hash: str,
    ) -> bool:
        key = (int(chat_id), int(message_id), payload_hash)
        async with self._cache_lock:
            return bool(self._cache.get(key))

    async def _forget(self, chat_id: int, message_id: int) -> None:
        prefix = (int(chat_id), int(message_id))
        async with self._cache_lock:
            keys = [key for key in self._cache.keys() if key[:2] == prefix]
            for key in keys:
                self._cache.pop(key, None)

    async def _acquire_lock(self, chat_id: int, message_id: int) -> asyncio.Lock:
        key = (int(chat_id), int(message_id))
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


class PokerMessagingOrchestrator:
    """Coordinate anchor messages, turn updates and seat voting."""

    def __init__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        max_seats: int = 8,
        queue_delay: float = 0.075,
    ) -> None:
        self.chat_id = chat_id
        self.state = GameState.WAITING
        self.max_seats = max_seats
        self._request_manager = RequestManager(
            bot,
            queue_delay=queue_delay,
        )
        self._anchors: Dict[int, AnchorMessage] = {}
        self._turn_message_id: Optional[int] = None
        self._turn_state = TurnState(notice=_DEFAULT_TURN_NOTICE)
        self._actions: Deque[str] = deque(maxlen=5)
        self._turn_update_cache: LRUCache[tuple[int, int, str], bool] = LRUCache(
            maxsize=256
        )
        self._turn_update_lock = asyncio.Lock()
        self._turn_update_task: Optional[asyncio.Task] = None
        self._turn_update_pending: Optional[_PendingTurnEdit] = None
        self._turn_update_delay = 0.06
        self._voting_message_id: Optional[int] = None
        self._voting_players: List[str] = []
        self._voting_status: Dict[str, str] = {}

    @property
    def request_manager(self) -> RequestManager:
        return self._request_manager

    async def start_voting(self, players: Sequence[str]) -> Optional[int]:
        """Begin the seating vote after a hand concludes."""

        self.state = GameState.VOTING
        self._voting_players = list(players)
        self._voting_status = {name: name for name in players}

        text = self._render_voting_text()
        markup = self._build_voting_markup()
        message = await self._request_manager.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_markup=markup,
            disable_notification=True,
        )
        self._voting_message_id = getattr(message, "message_id", None)
        return self._voting_message_id

    async def vote_continue(self, player_name: str) -> None:
        if self.state != GameState.VOTING:
            return
        if player_name not in self._voting_status:
            self._voting_players.append(player_name)
        self._voting_status[player_name] = f"✔️ {player_name}"
        await self._update_voting_message()

    async def vote_leave(self, player_name: str) -> None:
        if self.state != GameState.VOTING:
            return
        if player_name not in self._voting_status:
            self._voting_players.append(player_name)
        self._voting_status[player_name] = f"❌ {player_name}"
        await self._update_voting_message()

    async def vote_join(self, player_name: str) -> bool:
        if self.state != GameState.VOTING:
            return False
        approved = [
            name
            for name in self._voting_players
            if self._voting_status.get(name, name).startswith("✔️")
        ]
        remaining = self.max_seats - len(approved)
        if remaining <= 0:
            return False
        if player_name not in self._voting_players:
            self._voting_players.append(player_name)
        self._voting_status[player_name] = f"➕ {player_name}"
        await self._update_voting_message()
        return True

    async def end_voting(self) -> List[str]:
        if self.state != GameState.VOTING:
            return []
        approved: List[str] = []
        for name in self._voting_players:
            label = self._voting_status.get(name, name)
            if label.startswith("❌"):
                continue
            approved.append(name)

        if self._voting_message_id is not None:
            await self._request_manager.delete_message(
                chat_id=self.chat_id,
                message_id=self._voting_message_id,
            )
        self._voting_message_id = None
        self.state = GameState.WAITING
        return approved

    async def start_hand(
        self,
        players: Sequence[PlayerInfo],
        *,
        turn_state: Optional[TurnState] = None,
        notice: Optional[str] = None,
    ) -> None:
        self.state = GameState.IN_HAND
        self._anchors.clear()
        await self._request_manager.start()
        self._actions.clear()
        await self._reset_turn_updates()

        for player in players:
            base_text = self._format_anchor_text(player)
            message = await self._request_manager.send_message(
                chat_id=self.chat_id,
                text=base_text,
                reply_markup=self._build_anchor_markup(player.buttons, active=False),
                disable_notification=True,
            )
            message_id = getattr(message, "message_id", None)
            self._anchors[player.player_id] = AnchorMessage(
                player=player,
                message_id=message_id,
                base_text=base_text,
            )

        if turn_state is not None:
            self._turn_state = turn_state
            if not getattr(self._turn_state, "notice", None):
                self._turn_state.notice = _DEFAULT_TURN_NOTICE
        if notice:
            self._turn_state.notice = notice
        elif not getattr(self._turn_state, "notice", None):
            self._turn_state.notice = _DEFAULT_TURN_NOTICE
        text = self._render_turn_text()
        message = await self._request_manager.send_message(
            chat_id=self.chat_id,
            text=text,
            disable_notification=True,
        )
        self._turn_message_id = getattr(message, "message_id", None)

    async def set_player_active(
        self, player_id: int, *, active: bool, buttons: Optional[Sequence[ActionButton]] = None
    ) -> None:
        anchor = self._anchors.get(player_id)
        if not anchor or anchor.message_id is None:
            return
        if buttons is not None:
            anchor.player = PlayerInfo(
                player_id=anchor.player.player_id,
                name=anchor.player.name,
                seat_number=anchor.player.seat_number,
                roles=anchor.player.roles,
                buttons=tuple(buttons),
            )
        markup = self._build_anchor_markup(anchor.player.buttons, active=active)
        await self._request_manager.edit_message_reply_markup(
            chat_id=self.chat_id,
            message_id=anchor.message_id,
            reply_markup=markup,
        )

    async def update_turn_state(self, *, countdown_tick: bool = False, **updates: Any) -> None:
        for key, value in updates.items():
            if hasattr(self._turn_state, key):
                setattr(self._turn_state, key, value)
        await self._refresh_turn_message(countdown_tick=countdown_tick)

    async def record_action(self, description: str) -> None:
        description = description.strip()
        if not description:
            return
        self._actions.append(description)
        await self._refresh_turn_message()

    async def showdown(
        self,
        *,
        summary_lines: Sequence[str],
        chip_counts: Mapping[int, str],
    ) -> None:
        self.state = GameState.SHOWDOWN
        text = self._render_turn_text()
        summary: List[str] = list(summary_lines)
        if chip_counts:
            summary.extend(chip_counts.values())
        if summary:
            text = f"{text}\n\n" + "\n".join(summary)
        if self._turn_message_id is not None:
            await self._schedule_turn_message_edit(text)
        await self._clear_hand_messages()

    async def cancel(self) -> None:
        await self._clear_hand_messages()
        if self._voting_message_id is not None:
            await self._request_manager.delete_message(
                chat_id=self.chat_id,
                message_id=self._voting_message_id,
            )
            self._voting_message_id = None
        await self._request_manager.close()
        self.state = GameState.WAITING

    async def _refresh_turn_message(self, *, countdown_tick: bool = False) -> None:
        if self._turn_message_id is None:
            return
        text = self._render_turn_text()
        await self._schedule_turn_message_edit(text, countdown_tick=countdown_tick)

    async def _schedule_turn_message_edit(
        self,
        text: str,
        reply_markup: Any = None,
        *,
        countdown_tick: bool = False,
    ) -> None:
        if self._turn_message_id is None:
            return
        payload_hash = _content_hash(text, reply_markup)
        key = (self.chat_id, self._turn_message_id, payload_hash)
        if not countdown_tick and key in self._turn_update_cache:
            return
        self._turn_update_pending = _PendingTurnEdit(
            text=text,
            reply_markup=reply_markup,
            payload_hash=payload_hash,
            countdown_tick=countdown_tick,
        )
        delay = 0.0 if countdown_tick else self._turn_update_delay
        if self._turn_update_task is None or self._turn_update_task.done():
            self._turn_update_task = asyncio.create_task(
                self._flush_turn_updates(delay)
            )
        task = self._turn_update_task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _flush_turn_updates(self, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            while True:
                pending = self._turn_update_pending
                if pending is None:
                    break
                self._turn_update_pending = None
                message_id = self._turn_message_id
                if message_id is None:
                    break
                key = (self.chat_id, message_id, pending.payload_hash)
                if not pending.countdown_tick and key in self._turn_update_cache:
                    continue
                async with self._turn_update_lock:
                    message_id = self._turn_message_id
                    if message_id is None:
                        break
                    key = (self.chat_id, message_id, pending.payload_hash)
                    if not pending.countdown_tick and key in self._turn_update_cache:
                        continue
                    await self._request_manager.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=message_id,
                        text=pending.text,
                        reply_markup=pending.reply_markup,
                    )
                    self._turn_update_cache[key] = True
        finally:
            self._turn_update_task = None

    async def _reset_turn_updates(self) -> None:
        self._turn_update_pending = None
        self._turn_update_cache.clear()
        if self._turn_update_task and not self._turn_update_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                self._turn_update_task.cancel()
                await self._turn_update_task
        self._turn_update_task = None

    def _render_turn_text(self) -> str:
        board = (
            _CARD_SPACER.join(self._turn_state.board_cards)
            if self._turn_state.board_cards
            else "—"
        )
        lines: List[str] = []
        indicator = self._turn_state.turn_indicator
        if indicator:
            lines.append(f"🎯 {indicator}")
        stage = self._turn_state.stage or "Pre-Flop"
        lines.append(f"🎰 مرحله بازی: {stage}")
        lines.extend(
            [
                f"🃏 Board: {board}",
                f"💰 Pot: {self._turn_state.pot}",
                f"💵 Stack: {self._turn_state.stack}",
                f"🎲 Bet: {self._turn_state.current_bet}",
                f"📈 Max bet this round: {self._turn_state.max_bet}",
            ]
        )
        if self._turn_state.notice:
            lines.append("")
            lines.append(f"⬇️ {self._turn_state.notice}")
        if self._actions:
            lines.append("")
            lines.append("🎬 اکشن‌های اخیر:")
            for action in list(self._actions)[-5:]:
                lines.append(f"• {action}")
        return "\n".join(lines)

    def _format_anchor_text(self, player: PlayerInfo) -> str:
        roles = "، ".join(player.roles) if player.roles else "بازیکن"
        return "\n".join(
            [
                f"🎮 {player.name}",
                f"🪑 صندلی: {player.seat_number}",
                f"🎖️ نقش: {roles}",
            ]
        )

    def _build_anchor_markup(
        self, buttons: Sequence[ActionButton], *, active: bool
    ) -> Optional[InlineKeyboardMarkup]:
        if not active or not buttons:
            return None
        rows: List[List[InlineKeyboardButton]] = []
        for button in buttons:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=button.label,
                        callback_data=button.callback_data,
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _clear_hand_messages(self) -> None:
        await self._reset_turn_updates()
        for anchor in list(self._anchors.values()):
            if anchor.message_id is not None:
                await self._request_manager.delete_message(
                    chat_id=self.chat_id,
                    message_id=anchor.message_id,
                )
        self._anchors.clear()
        if self._turn_message_id is not None:
            await self._request_manager.delete_message(
                chat_id=self.chat_id,
                message_id=self._turn_message_id,
            )
        self._turn_message_id = None
        self._actions.clear()
        self._turn_state = TurnState(notice=_DEFAULT_TURN_NOTICE)

    def _render_voting_text(self) -> str:
        lines = [
            "🪑 نشست میز شروع شد!",
            "",
            "بازیکنان دست قبل به طور پیشفرض در بازی هستند:",
            "",
        ]
        if not self._voting_players:
            lines.append("(بازیکنی در دست قبل حضور نداشت)")
        else:
            for name in self._voting_players:
                lines.append(self._voting_status.get(name, name))
        lines.extend(
            [
                "",
                "برای ماندن روی دکمه ✅ بزنید.",
                "",
                "برای خارج شدن ❌ بزنید.",
                "",
                "اگر جا هست ➕ برای بازیکن جدید.",
            ]
        )
        return "\n".join(lines)

    def _build_voting_markup(self) -> InlineKeyboardMarkup:
        join_enabled = len(self._voting_players) < self.max_seats
        rows = [
            [InlineKeyboardButton(text="✅ ادامه", callback_data="seat:stay")],
            [InlineKeyboardButton(text="❌ خروج", callback_data="seat:leave")],
        ]
        if join_enabled:
            rows.append(
                [InlineKeyboardButton(text="➕ ورود", callback_data="seat:join")]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➕ ورود (ظرفیت تکمیل)", callback_data="seat:full"
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _update_voting_message(self) -> None:
        if self._voting_message_id is None:
            return
        text = self._render_voting_text()
        await self._request_manager.edit_message_text(
            chat_id=self.chat_id,
            message_id=self._voting_message_id,
            text=text,
        )
        markup = self._build_voting_markup()
        await self._request_manager.edit_message_reply_markup(
            chat_id=self.chat_id,
            message_id=self._voting_message_id,
            reply_markup=markup,
        )


__all__ = [
    "ActionButton",
    "AnchorMessage",
    "GameState",
    "PlayerInfo",
    "PokerMessagingOrchestrator",
    "RequestManager",
    "TurnState",
]

