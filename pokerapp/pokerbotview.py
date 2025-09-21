#!/usr/bin/env python3

from telegram import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Bot,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from io import BytesIO
from typing import (
    Optional,
    Dict,
    Any,
    Tuple,
    List,
    Sequence,
    Set,
    Callable,
)
from dataclasses import dataclass
import asyncio
import datetime
import hashlib
import inspect
import logging
import json
from cachetools import FIFOCache, LRUCache
from pokerapp.config import DEFAULT_RATE_LIMIT_PER_MINUTE, DEFAULT_RATE_LIMIT_PER_SECOND
from pokerapp.winnerdetermination import HAND_NAMES_TRANSLATIONS
from pokerapp.desk import DeskImageGenerator
from pokerapp.cards import Cards, Card
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    PlayerAction,
    MessageId,
    ChatId,
    Mention,
    Money,
    PlayerState,
)
from pokerapp.telegram_validation import TelegramPayloadValidator
from pokerapp.utils.debug_trace import trace_telegram_api_call
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics


logger = logging.getLogger(__name__)
debug_trace_logger = logging.getLogger("pokerbot.debug_trace")

_CARD_SPACER = "     "


def build_player_cards_keyboard(
    hole_cards: Sequence[str],
    community_cards: Sequence[str],
    current_stage: str,
) -> ReplyKeyboardMarkup:
    """Builds a personalized ReplyKeyboardMarkup for a player."""

    # Row 1: The player's unique hole cards.
    row1 = list(hole_cards)

    # Row 2: The shared community cards on the board.
    row2 = list(community_cards)

    # Row 3: Game stages, with the current stage highlighted by a '✅' emoji.
    stages_persian = ["پری فلاپ", "فلاپ", "ترن", "ریور"]
    stage_map = {
        "ROUND_PRE_FLOP": "پری فلاپ",
        "ROUND_FLOP": "فلاپ",
        "ROUND_TURN": "ترن",
        "ROUND_RIVER": "ریور",
    }

    current_stage_label = stage_map.get(current_stage.upper(), "")

    row3 = [
        f"✅ {label}" if label == current_stage_label else label
        for label in stages_persian
    ]

    # Construct and return the final keyboard object.
    return ReplyKeyboardMarkup(
        keyboard=[row1, row2, row3],
        resize_keyboard=True,      # Makes the keyboard fit the content.
        one_time_keyboard=False,   # The keyboard persists until replaced.
        selective=True,            # CRITICAL: Shows the keyboard ONLY to the @-mentioned user.
    )


@dataclass(slots=True)
class TurnMessageUpdate:
    message_id: Optional[MessageId]
    call_label: str
    call_action: PlayerAction
    board_line: str


@dataclass(slots=True)
class AnchorUpdateRequest:
    player: Player
    seat_number: int
    role_label: str
    board_cards: Sequence[Card]
    active: bool
    message_id: Optional[MessageId]
    player_cards: Sequence[Card]
    game_state: GameState


class PokerBotViewer:
    _ZERO_WIDTH_SPACE = "\u2063"
    _INVISIBLE_CHARS = {
        "\u200b",  # zero width space
        "\u200c",  # zero width non-joiner
        "\u200d",  # zero width joiner
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark
        "\u2060",  # word joiner
        "\u2061",
        "\u2062",
        "\u2063",
    }
    _ACTIVE_ANCHOR_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }
    _SUIT_EMOJI = {
        "♠": "♠\ufe0f",
        "♥": "♥\ufe0f",
        "♦": "♦\ufe0f",
        "♣": "♣\ufe0f",
    }

    @classmethod
    def _has_visible_text(cls, text: str) -> bool:
        if not text:
            return False
        for char in text:
            if char.isspace():
                continue
            if char in cls._INVISIBLE_CHARS:
                continue
            return True
        return False

    @staticmethod
    def _log_skip_empty(chat_id: ChatId, message_id: Optional[MessageId]) -> None:
        logger.info(
            "SKIP SEND: empty or invisible content for %s, msg %s",
            chat_id,
            message_id,
        )

    @staticmethod
    def _is_private_chat(chat_id: ChatId) -> bool:
        if isinstance(chat_id, str):
            return chat_id.startswith("private:")
        return False

    @staticmethod
    def _build_hidden_mention(mention_markdown: Optional[Mention]) -> str:
        """Return an invisible Markdown mention for ``mention_markdown``."""

        if not mention_markdown:
            return ""

        try:
            label_end = mention_markdown.index("](")
            link_end = mention_markdown.index(")", label_end + 2)
            link = mention_markdown[label_end + 2 : link_end].strip()
            if link:
                return f"[{PokerBotViewer._ZERO_WIDTH_SPACE}]({link})"
        except ValueError:
            pass

        return mention_markdown

    @staticmethod
    def _build_context(method: str, **values: Any) -> Dict[str, Any]:
        context: Dict[str, Any] = {"method": method}
        for key, value in values.items():
            if value is not None:
                context[key] = value
        return context

    @staticmethod
    def _serialize_markup(
        markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
    ) -> Optional[str]:
        if markup is None:
            return None
        serializer = getattr(markup, "to_dict", None)
        if callable(serializer):
            try:
                return json.dumps(serializer(), sort_keys=True, ensure_ascii=False)
            except TypeError:
                pass
        try:
            return json.dumps(markup, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return repr(markup)

    def __init__(
        self,
        bot: Bot,
        admin_chat_id: Optional[int] = None,
        *,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        rate_limit_per_second: Optional[int] = DEFAULT_RATE_LIMIT_PER_SECOND,
        rate_limiter_delay: Optional[float] = 0.05,
        update_debounce: float = 0.25,
        request_metrics: Optional[RequestMetrics] = None,
    ):
        # ``update_debounce`` historically controlled how quickly message edits
        # were flushed to Telegram.  The messaging rewrite in mid-2023 stopped
        # consuming the parameter, but we now wire it back in so call sites can
        # opt into even tighter coalescing windows when needed.
        self._message_update_debounce_delay = max(0.0, float(update_debounce))

        self._bot = bot
        self._desk_generator = DeskImageGenerator()
        self._admin_chat_id = admin_chat_id
        self._validator = TelegramPayloadValidator(
            logger_=logger.getChild("validation")
        )
        self._request_metrics = request_metrics or RequestMetrics(
            logger_=logger.getChild("request_metrics")
        )
        # Legacy rate-limit attributes are retained for backwards compatibility
        # with configuration code but do not influence runtime behaviour.
        self._legacy_rate_limit_per_minute = rate_limit_per_minute
        self._legacy_rate_limit_per_second = rate_limit_per_second
        self._legacy_rate_limiter_delay = rate_limiter_delay

        self._message_update_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._message_update_guard = asyncio.Lock()
        self._message_payload_hashes: LRUCache[Tuple[int, int], str] = LRUCache(
            maxsize=1024
        )
        self._message_payload_cache_lock = asyncio.Lock()
        self._last_callback_updates: Dict[Tuple[int, int, str, int], str] = {}
        self._last_callback_edit: Dict[Tuple[int, str], str] = {}
        self._last_message_hash: Dict[int, str] = {}
        self._last_message_hash_lock = asyncio.Lock()
        self._deleted_messages: Set[int] = set()
        self._deleted_messages_lock = asyncio.Lock()
        self._turn_message_cache: LRUCache[Tuple[int, int], str] = LRUCache(
            maxsize=256
        )
        self._turn_message_cache_lock = asyncio.Lock()
        self._inline_keyboard_cache: FIFOCache[str, InlineKeyboardMarkup] = FIFOCache(
            maxsize=32
        )
        self._inline_keyboard_cache_lock = asyncio.Lock()
        self._stage_payload_hashes: LRUCache[Tuple[int, int, str], str] = LRUCache(
            maxsize=4096
        )
        self._stage_payload_hash_lock = asyncio.Lock()
        self._prestart_countdown_tasks: Dict[int, asyncio.Task[None]] = {}
        self._prestart_countdown_lock = asyncio.Lock()
        self._pending_updates: Dict[
            Tuple[ChatId, Optional[MessageId]], Dict[str, Any]
        ] = {}
        self._pending_update_tasks: Dict[
            Tuple[ChatId, Optional[MessageId]], asyncio.Task[Optional[MessageId]]
        ] = {}
        self._pending_updates_lock = asyncio.Lock()

        self._messenger = MessagingService(
            bot,
            logger_=logger.getChild("messaging_service"),
            request_metrics=self._request_metrics,
            deleted_messages=self._deleted_messages,
            deleted_messages_lock=self._deleted_messages_lock,
            last_message_hash=self._last_message_hash,
            last_message_hash_lock=self._last_message_hash_lock,
        )

    async def _cancel_prestart_countdown(self, chat_id: ChatId) -> None:
        normalized_chat = self._safe_int(chat_id)
        async with self._prestart_countdown_lock:
            task = self._prestart_countdown_tasks.pop(normalized_chat, None)
            if task is not None:
                task.cancel()
                logger.info(
                    "[Countdown] Cancelled prestart countdown for chat %s",
                    normalized_chat,
                )

    def _create_countdown_task(
        self,
        normalized_chat: int,
        anchor_message_id: Optional[int],
        end_time: float,
        payload_fn: Callable[[int], Tuple[str, Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup]]],
    ) -> asyncio.Task[None]:
        """
        Return an asyncio.Task that runs a per-second countdown until end_time.

        payload_fn(seconds_left:int) -> (text:str, reply_markup|None)

        The task uses schedule_message_update if available, otherwise falls back to _update_message.
        It cleans up self._prestart_countdown_tasks entry when finished or cancelled.
        """

        async def _run() -> None:
            current_message_id = anchor_message_id
            try:
                loop = asyncio.get_event_loop()
                while True:
                    now = loop.time()
                    seconds_left = max(0, int(round(end_time - now)))
                    try:
                        text, reply_markup = payload_fn(seconds_left)
                    except Exception:
                        logger.exception("[Countdown] payload_fn failed")
                        text, reply_markup = "", None
                    try:
                        schedule = getattr(self, "schedule_message_update", None)
                        if callable(schedule):
                            result = await schedule(
                                chat_id=normalized_chat,
                                message_id=current_message_id,
                                text=text,
                                reply_markup=reply_markup,
                                request_category=RequestCategory.ANCHOR,
                            )
                        else:
                            result = await self._update_message(
                                chat_id=normalized_chat,
                                message_id=current_message_id,
                                text=text,
                                reply_markup=reply_markup,
                                request_category=RequestCategory.ANCHOR,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("[Countdown] failed to send countdown tick")
                    else:
                        if isinstance(result, int):
                            current_message_id = result
                        elif hasattr(result, "message_id"):
                            maybe_id = getattr(result, "message_id", None)
                            if isinstance(maybe_id, int):
                                current_message_id = maybe_id
                        elif result is not None:
                            try:
                                current_message_id = int(result)
                            except (TypeError, ValueError):
                                pass
                    if seconds_left <= 0:
                        break
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                logger.debug(
                    "[Countdown] Task cancelled for chat %s", normalized_chat
                )
                raise
            finally:
                async with self._prestart_countdown_lock:
                    existing = self._prestart_countdown_tasks.get(normalized_chat)
                    if existing is asyncio.current_task():
                        self._prestart_countdown_tasks.pop(normalized_chat, None)
                logger.info(
                    "[Countdown] Prestart countdown finished for chat %s",
                    normalized_chat,
                )

        return asyncio.create_task(_run())

    async def start_prestart_countdown(
        self,
        chat_id: ChatId,
        anchor_message_id: Optional[MessageId],
        seconds: int,
        payload_fn: Callable[[int], Tuple[str, Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup]]],
    ) -> None:
        """
        Start or replace a per-chat prestart countdown.

        payload_fn(seconds_left:int) -> (text, reply_markup)
        """

        normalized_chat = self._safe_int(chat_id)
        end_time = asyncio.get_event_loop().time() + max(0, int(seconds))
        async with self._prestart_countdown_lock:
            old = self._prestart_countdown_tasks.get(normalized_chat)
            if old is not None:
                old.cancel()
            task = self._create_countdown_task(
                normalized_chat, anchor_message_id, end_time, payload_fn
            )
            self._prestart_countdown_tasks[normalized_chat] = task
        logger.info(
            "[Countdown] Started prestart countdown for chat %s seconds=%s anchor=%s",
            normalized_chat,
            seconds,
            anchor_message_id,
        )

    def _payload_hash(
        self,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
    ) -> str:
        markup_hash = self._serialize_markup(reply_markup) or ""
        payload = f"{text}|{markup_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def payload_signature(
        self,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
    ) -> str:
        """Public helper exposing the stable payload hash for callers."""

        return self._payload_hash(text, reply_markup)

    @staticmethod
    def _safe_int(value: Optional[int | str]) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    async def schedule_message_update(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[MessageId],
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        parse_mode: str = ParseMode.MARKDOWN,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        key = (chat_id, message_id)
        loop = asyncio.get_running_loop()

        async with self._pending_updates_lock:
            pending_entry = self._pending_updates.get(key)
            future_obj = pending_entry.get("future") if pending_entry else None
            future: asyncio.Future[Optional[MessageId]]
            if isinstance(future_obj, asyncio.Future) and not future_obj.done():
                future = future_obj
            else:
                future = loop.create_future()

            if not future.done():
                payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "reply_markup": reply_markup,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": disable_web_page_preview,
                    "disable_notification": disable_notification,
                    "request_category": request_category,
                }
                self._pending_updates[key] = {"payload": payload, "future": future}

            pending_task = self._pending_update_tasks.get(key)
            if pending_task is not None:
                pending_task.cancel()

            task = asyncio.create_task(
                self._execute_debounced_update(key, self._message_update_debounce_delay)
            )
            self._pending_update_tasks[key] = task

        return await future

    async def _execute_debounced_update(
        self, key: Tuple[ChatId, Optional[MessageId]], delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)

            async with self._pending_updates_lock:
                active_task = self._pending_update_tasks.get(key)
                current_task = asyncio.current_task()
                if active_task is not current_task:
                    return

                entry = self._pending_updates.pop(key, None)
                self._pending_update_tasks.pop(key, None)

            if not entry:
                return

            payload = entry.get("payload", {})
            future = entry.get("future")

            try:
                result = await self._update_message(**payload)
            except Exception as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                logger.exception(
                    "Debounced update failed",
                    extra={
                        "chat_id": payload.get("chat_id"),
                        "message_id": payload.get("message_id"),
                        "request_category": payload.get("request_category"),
                    },
                )
            else:
                if future is not None and not future.done():
                    future.set_result(result)
        except asyncio.CancelledError:
            return

    async def _acquire_message_lock(
        self, chat_id: ChatId, message_id: Optional[MessageId]
    ) -> asyncio.Lock:
        normalized_chat = self._safe_int(chat_id)
        normalized_message = (
            self._safe_int(message_id) if message_id is not None else -1
        )
        key = (normalized_chat, normalized_message)
        async with self._message_update_guard:
            lock = self._message_update_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._message_update_locks[key] = lock
        return lock

    @staticmethod
    def _normalize_stage_name(stage: Optional[str]) -> str:
        return stage if stage else "<unknown>"

    async def should_skip_stage_update(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[MessageId],
        stage: str,
        payload_hash: str,
    ) -> bool:
        """Return ``True`` if the same stage payload was already broadcast."""

        if message_id is None:
            return False

        normalized_chat = self._safe_int(chat_id)
        normalized_message = self._safe_int(message_id)
        stage_key = (normalized_chat, normalized_message, stage)
        cached = await self._get_stage_payload_hash(stage_key)
        return cached == payload_hash

    def _should_skip_callback_update(
        self,
        key: Tuple[int, int, str, int],
        callback_id: str,
    ) -> bool:
        stored_token = self._last_callback_updates.get(key)
        return stored_token == callback_id

    def _store_callback_update_token(
        self,
        key: Tuple[int, int, str, int],
        callback_id: str,
    ) -> None:
        self._last_callback_updates[key] = callback_id

    async def _clear_callback_tokens_for_message(
        self,
        normalized_chat: int,
        normalized_message: int,
    ) -> None:
        to_remove = [
            key
            for key in self._last_callback_updates
            if key[0] == normalized_chat and key[1] == normalized_message
        ]
        for key in to_remove:
            self._last_callback_updates.pop(key, None)

        callback_throttle_keys = [
            key
            for key in self._last_callback_edit
            if key[0] == normalized_message
        ]
        for key in callback_throttle_keys:
            self._last_callback_edit.pop(key, None)

        await self._pop_last_text_hash(normalized_message)
        await self._clear_stage_payloads_for_message(
            normalized_chat, normalized_message
        )

    async def _get_payload_hash(
        self, key: Tuple[int, int]
    ) -> Optional[str]:
        async with self._message_payload_cache_lock:
            return self._message_payload_hashes.get(key)

    async def _set_payload_hash(self, key: Tuple[int, int], value: str) -> None:
        async with self._message_payload_cache_lock:
            self._message_payload_hashes[key] = value
            logger.debug(
                "Payload cache size %s/%s",
                self._message_payload_hashes.currsize,
                self._message_payload_hashes.maxsize,
            )

    async def _pop_payload_hash(self, key: Tuple[int, int]) -> None:
        async with self._message_payload_cache_lock:
            self._message_payload_hashes.pop(key, None)

    async def _get_last_text_hash(self, message_id: int) -> Optional[str]:
        async with self._last_message_hash_lock:
            return self._last_message_hash.get(message_id)

    async def _set_last_text_hash(self, message_id: int, value: str) -> None:
        async with self._last_message_hash_lock:
            self._last_message_hash[message_id] = value
            logger.debug(
                "Text hash cache size %s",
                len(self._last_message_hash),
            )

    async def _pop_last_text_hash(self, message_id: int) -> None:
        async with self._last_message_hash_lock:
            self._last_message_hash.pop(message_id, None)

    async def _is_message_deleted(self, message_id: int) -> bool:
        async with self._deleted_messages_lock:
            return message_id in self._deleted_messages

    async def _mark_message_deleted(self, message_id: int) -> None:
        async with self._deleted_messages_lock:
            self._deleted_messages.add(message_id)

    async def _unmark_message_deleted(self, message_id: int) -> None:
        async with self._deleted_messages_lock:
            self._deleted_messages.discard(message_id)

    async def _get_turn_cache_hash(
        self, key: Tuple[int, int]
    ) -> Optional[str]:
        async with self._turn_message_cache_lock:
            return self._turn_message_cache.get(key)

    async def _set_turn_cache_hash(self, key: Tuple[int, int], value: str) -> None:
        async with self._turn_message_cache_lock:
            self._turn_message_cache[key] = value
            logger.debug(
                "Turn cache size %s/%s",
                self._turn_message_cache.currsize,
                self._turn_message_cache.maxsize,
            )

    async def _pop_turn_cache_hash(self, key: Tuple[int, int]) -> None:
        async with self._turn_message_cache_lock:
            self._turn_message_cache.pop(key, None)

    async def _get_stage_payload_hash(
        self, key: Tuple[int, int, str]
    ) -> Optional[str]:
        async with self._stage_payload_hash_lock:
            return self._stage_payload_hashes.get(key)

    async def _set_stage_payload_hash(
        self, key: Tuple[int, int, str], value: str
    ) -> None:
        async with self._stage_payload_hash_lock:
            self._stage_payload_hashes[key] = value
            logger.debug(
                "Stage payload cache size %s/%s",
                self._stage_payload_hashes.currsize,
                self._stage_payload_hashes.maxsize,
            )

    async def _clear_stage_payloads_for_message(
        self, normalized_chat: int, normalized_message: int
    ) -> None:
        async with self._stage_payload_hash_lock:
            keys_to_remove = [
                key
                for key in self._stage_payload_hashes
                if key[0] == normalized_chat and key[1] == normalized_message
            ]
            for key in keys_to_remove:
                self._stage_payload_hashes.pop(key, None)

    def _detect_callback_context(
        self,
    ) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        try:
            stack = inspect.stack(context=0)
        except Exception:
            return None, None, None

        callback_id: Optional[str] = None
        stage_name: Optional[str] = None
        user_id: Optional[int] = None
        try:
            for frame_info in stack:
                locals_ = frame_info.frame.f_locals

                callback = locals_.get("callback_query")
                if callback is None:
                    update = locals_.get("update")
                    if update is not None:
                        callback = getattr(update, "callback_query", None)

                if callback is not None:
                    if callback_id is None:
                        raw_id = getattr(callback, "id", None) or getattr(
                            callback, "query_id", None
                        )
                        if raw_id is None:
                            data = getattr(callback, "data", None)
                            if data is not None:
                                raw_id = f"data:{data}"
                        if raw_id is None:
                            from_user = getattr(callback, "from_user", None)
                            if from_user is not None:
                                user_identifier = getattr(from_user, "id", None)
                                if user_identifier is not None:
                                    raw_id = f"user:{user_identifier}"
                        if raw_id is not None:
                            callback_id = str(raw_id)

                    if user_id is None:
                        from_user = getattr(callback, "from_user", None)
                        if from_user is not None:
                            raw_user_id = getattr(from_user, "id", None)
                            if raw_user_id is not None:
                                try:
                                    user_id = int(raw_user_id)
                                except (TypeError, ValueError):
                                    user_id = None

                if stage_name is None:
                    game = locals_.get("game")
                    if game is not None:
                        state = getattr(game, "state", None)
                        if state is not None:
                            name = getattr(state, "name", None)
                            if isinstance(name, str):
                                stage_name = name
                            else:
                                stage_name = str(state)

                if callback_id is not None and stage_name is not None:
                    break

            return callback_id, stage_name, user_id
        except Exception:
            return None, None, None
        finally:
            del stack

    async def _update_message(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[MessageId],
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        parse_mode: str = ParseMode.MARKDOWN,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        context = self._build_context(
            "update_message", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=parse_mode,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping update_message due to invalid text",
                extra={"context": context},
            )
            return message_id

        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, message_id)
            return message_id

        if (
            request_category == RequestCategory.ANCHOR
            and reply_markup is not None
            and not isinstance(reply_markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup))
        ):
            logger.warning(
                "Discarding unsupported reply markup for anchor update",
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "markup_type": type(reply_markup).__name__,
                },
            )
            reply_markup = None

        payload_hash = self._payload_hash(normalized_text, reply_markup)
        message_text_hash = hashlib.md5(normalized_text.encode("utf-8")).hexdigest()
        normalized_chat = self._safe_int(chat_id)
        normalized_existing_message = (
            self._safe_int(message_id) if message_id is not None else None
        )
        message_key: Optional[Tuple[int, int]] = (
            (normalized_chat, normalized_existing_message)
            if normalized_existing_message is not None
            else None
        )
        callback_id: Optional[str] = None
        callback_stage_name = self._normalize_stage_name(request_category.value)
        callback_user_id: Optional[int] = None
        callback_token_key: Optional[Tuple[int, int, str, int]] = None
        callback_throttle_key: Optional[Tuple[int, str]] = None
        if normalized_existing_message is not None:
            callback_id, detected_stage, detected_user_id = (
                self._detect_callback_context()
            )
            if callback_id is not None:
                callback_stage_name = self._normalize_stage_name(detected_stage)
                callback_user_id = self._safe_int(detected_user_id)
                callback_token_key = (
                    normalized_chat,
                    normalized_existing_message,
                    callback_stage_name,
                    callback_user_id,
                )
                callback_throttle_key = (
                    normalized_existing_message,
                    callback_stage_name,
                )
                last_callback_token = self._last_callback_edit.get(
                    callback_throttle_key
                )
                if last_callback_token == callback_id:
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} due to callback throttling"
                    )
                    return message_id
                if self._should_skip_callback_update(
                    callback_token_key, callback_id
                ):
                    logger.debug(
                        "Skipping update_message due to callback throttling",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "callback_id": callback_id,
                            "game_stage": callback_stage_name,
                            "callback_user_id": callback_user_id,
                            "trigger": "callback_query",
                        },
                    )
                    return message_id
        stage_key: Optional[Tuple[int, int, str]] = (
            (normalized_chat, normalized_existing_message, callback_stage_name)
            if normalized_existing_message is not None
            else None
        )
        previous_payload_hash: Optional[str] = None
        if normalized_existing_message is not None:
            if await self._is_message_deleted(normalized_existing_message):
                debug_trace_logger.info(
                    f"Skipping editMessageText for message_id={message_id} because it was deleted"
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id
            previous_text_hash = await self._get_last_text_hash(
                normalized_existing_message
            )
            if previous_text_hash == message_text_hash:
                anchor_markup_only = False
                if (
                    request_category == RequestCategory.ANCHOR
                    and message_key is not None
                    and isinstance(reply_markup, InlineKeyboardMarkup)
                ):
                    previous_payload_hash = await self._get_payload_hash(message_key)
                    anchor_markup_only = previous_payload_hash != payload_hash
                if anchor_markup_only:
                    logger.debug(
                        "Anchor text unchanged; will refresh inline markup",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                        },
                    )
                else:
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} due to no content change"
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
        turn_cache_key = message_key if request_category == RequestCategory.TURN else None

        if stage_key is not None:
            cached_stage_hash = await self._get_stage_payload_hash(stage_key)
            if cached_stage_hash == payload_hash:
                logger.debug(
                    "Skipping update_message due to stage throttle",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                        "game_stage": callback_stage_name,
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

        if message_key is not None:
            if previous_payload_hash is None:
                previous_payload_hash = await self._get_payload_hash(message_key)
            if previous_payload_hash == payload_hash:
                logger.debug(
                    "Skipping update_message due to identical payload",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                        "callback_id": callback_id,
                        "game_stage": callback_stage_name,
                        "callback_user_id": callback_user_id,
                        "trigger": "callback_query"
                        if callback_id is not None
                        else "automatic",
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

        if turn_cache_key is not None:
            cached_turn_hash = await self._get_turn_cache_hash(turn_cache_key)
            if cached_turn_hash == payload_hash:
                logger.debug(
                    "Skipping turn update due to LRU cache hit",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

        lock = await self._acquire_message_lock(chat_id, message_id)
        async with lock:
            if normalized_existing_message is not None:
                if await self._is_message_deleted(normalized_existing_message):
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} because it was deleted"
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
                previous_text_hash = await self._get_last_text_hash(
                    normalized_existing_message
                )
                if previous_text_hash == message_text_hash:
                    anchor_markup_only = False
                    if (
                        request_category == RequestCategory.ANCHOR
                        and message_key is not None
                        and isinstance(reply_markup, InlineKeyboardMarkup)
                    ):
                        if previous_payload_hash is None:
                            previous_payload_hash = await self._get_payload_hash(
                                message_key
                            )
                        anchor_markup_only = previous_payload_hash != payload_hash
                    if anchor_markup_only:
                        if reply_markup is not None and not isinstance(
                            reply_markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)
                        ):
                            logger.debug(
                                "Ignoring unsupported reply markup during anchor refresh",
                                extra={
                                    "chat_id": chat_id,
                                    "message_id": message_id,
                                    "markup_type": type(reply_markup).__name__,
                                },
                            )
                            reply_markup = None
                        callback_token_registered = False
                        if (
                            callback_id is not None
                            and callback_throttle_key is not None
                        ):
                            self._last_callback_edit[
                                callback_throttle_key
                            ] = callback_id
                            callback_token_registered = True
                        try:
                            updated = await self.edit_message_reply_markup(
                                chat_id=chat_id,
                                message_id=message_id,
                                reply_markup=reply_markup,
                            )
                        except Exception:
                            if callback_token_registered:
                                self._last_callback_edit.pop(
                                    callback_throttle_key, None
                                )
                            raise
                        if not updated and callback_token_registered:
                            self._last_callback_edit.pop(
                                callback_throttle_key, None
                            )
                        if updated:
                            await self._set_last_text_hash(
                                normalized_existing_message, message_text_hash
                            )
                            if message_key is not None:
                                await self._set_payload_hash(
                                    message_key, payload_hash
                                )
                            if stage_key is not None:
                                await self._set_stage_payload_hash(
                                    stage_key, payload_hash
                                )
                            if turn_cache_key is not None:
                                await self._set_turn_cache_hash(
                                    message_key, payload_hash
                                )
                            await self._unmark_message_deleted(
                                normalized_existing_message
                            )
                            if (
                                callback_id is not None
                                and callback_token_key is not None
                            ):
                                self._store_callback_update_token(
                                    callback_token_key, callback_id
                                )
                            if (
                                callback_id is not None
                                and callback_throttle_key is not None
                            ):
                                self._last_callback_edit[
                                    callback_throttle_key
                                ] = callback_id
                            return message_id
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} due to no content change"
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
            if message_key is not None:
                if previous_payload_hash is None:
                    previous_payload_hash = await self._get_payload_hash(
                        message_key
                    )
                if previous_payload_hash == payload_hash:
                    logger.debug(
                        "Skipping update_message inside lock due to identical payload",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "callback_id": callback_id,
                            "game_stage": callback_stage_name,
                            "callback_user_id": callback_user_id,
                            "trigger": "callback_query"
                            if callback_id is not None
                            else "automatic",
                        },
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
            if stage_key is not None:
                cached_stage_hash = await self._get_stage_payload_hash(stage_key)
                if cached_stage_hash == payload_hash:
                    logger.debug(
                        "Skipping update_message inside lock due to stage throttle",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "game_stage": callback_stage_name,
                        },
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
            if (
                callback_token_key is not None
                and callback_id is not None
                and callback_throttle_key is not None
            ):
                last_callback_token = self._last_callback_edit.get(
                    callback_throttle_key
                )
                if last_callback_token == callback_id:
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} due to callback throttling"
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
            if (
                callback_token_key is not None
                and callback_id is not None
                and self._should_skip_callback_update(callback_token_key, callback_id)
            ):
                logger.debug(
                    "Skipping update_message inside lock due to callback throttling",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                        "callback_id": callback_id,
                        "game_stage": callback_stage_name,
                        "callback_user_id": callback_user_id,
                        "trigger": "callback_query",
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

            callback_token_registered = False
            try:
                if message_id is None:
                    result = await self._messenger.send_message(
                        chat_id=chat_id,
                        text=normalized_text,
                        reply_markup=reply_markup,
                        request_category=request_category,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                        disable_notification=disable_notification,
                    )
                    new_message_id: Optional[MessageId] = getattr(
                        result, "message_id", None
                    )
                else:
                    if (
                        callback_id is not None
                        and callback_throttle_key is not None
                    ):
                        self._last_callback_edit[
                            callback_throttle_key
                        ] = callback_id
                        callback_token_registered = True
                    result = await self._messenger.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=normalized_text,
                        reply_markup=reply_markup,
                        request_category=request_category,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    if hasattr(result, "message_id"):
                        new_message_id = result.message_id  # type: ignore[assignment]
                    elif isinstance(result, int):
                        new_message_id = result
                    else:
                        new_message_id = message_id
            except (BadRequest, Forbidden, RetryAfter, TelegramError) as exc:
                if callback_token_registered and callback_throttle_key is not None:
                    self._last_callback_edit.pop(callback_throttle_key, None)
                logger.error(
                    "Failed to update message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return message_id

            if new_message_id is None:
                return message_id

            normalized_new_message = self._safe_int(new_message_id)
            new_message_key = (normalized_chat, normalized_new_message)
            await self._set_payload_hash(new_message_key, payload_hash)
            await self._set_last_text_hash(normalized_new_message, message_text_hash)
            await self._unmark_message_deleted(normalized_new_message)
            if turn_cache_key is not None:
                await self._set_turn_cache_hash(new_message_key, payload_hash)
            new_stage_key = (
                normalized_chat,
                normalized_new_message,
                callback_stage_name,
            )
            await self._set_stage_payload_hash(new_stage_key, payload_hash)
            if callback_id is not None:
                new_callback_token_key = (
                    normalized_chat,
                    normalized_new_message,
                    callback_stage_name,
                    callback_user_id if callback_user_id is not None else 0,
                )
                self._store_callback_update_token(
                    new_callback_token_key,
                    callback_id,
                )
                self._last_callback_edit[
                    (normalized_new_message, callback_stage_name)
                ] = callback_id
            if (
                message_key is not None
                and new_message_key != message_key
            ):
                await self._pop_payload_hash(message_key)
                if turn_cache_key is not None:
                    await self._pop_turn_cache_hash(message_key)
                await self._clear_callback_tokens_for_message(
                    message_key[0],
                    message_key[1],
                )
            return new_message_id

    @staticmethod
    def _render_cards(cards: Sequence[Card]) -> str:
        if not cards:
            return "—"
        return _CARD_SPACER.join(str(card) for card in cards)

    @classmethod
    def _format_card_line(cls, label: str, cards: Sequence[Card]) -> str:
        return f"{label}: {cls._render_cards(cards)}"

    @classmethod
    def _build_anchor_text(
        cls,
        *,
        mention_markdown: Mention,
        seat_number: int,
        role_label: str,
    ) -> str:
        lines = [
            f"🎮 {mention_markdown}",
            f"🪑 صندلی: `{seat_number}`",
            f"🎖️ نقش: {role_label}",
        ]
        # کارت‌های بازیکن در کیبورد پاسخ اختصاصی نمایش داده می‌شوند.
        return "\n".join(lines)

    async def update_player_anchor(
        self,
        *,
        chat_id: ChatId,
        player: Player,
        seat_number: int,
        role_label: str,
        board_cards: Sequence[Card],
        player_cards: Sequence[Card],
        game_state: GameState,
        active: bool,
        message_id: Optional[MessageId] = None,
    ) -> Optional[MessageId]:
        text = self._build_anchor_text(
            mention_markdown=player.mention_markdown,
            seat_number=seat_number,
            role_label=role_label,
        )
        player_hole_cards = [str(card) for card in player_cards]
        community_cards = [str(card) for card in board_cards]
        reply_markup = build_player_cards_keyboard(
            hole_cards=player_hole_cards,
            community_cards=community_cards,
            current_stage=game_state.name,
        )
        if active:
            text = f"{text}\n\n🎯 **نوبت بازی این بازیکن است.**"
        return await self.schedule_message_update(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            disable_notification=message_id is not None,
            request_category=RequestCategory.ANCHOR,
        )

    async def send_player_cards_keyboard(
        self,
        *,
        chat_id: ChatId,
        hole_cards: Sequence[str],
        community_cards: Sequence[str],
        current_stage: str,
        message_id: Optional[MessageId] = None,
        text: str = "کارت های شما 👇",
    ) -> Optional[MessageId]:
        """Send or update the per-player cards reply keyboard."""

        markup = build_player_cards_keyboard(
            hole_cards=hole_cards,
            community_cards=community_cards,
            current_stage=current_stage,
        )

        if message_id:
            try:
                edited_id = await self.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    request_category=RequestCategory.GENERAL,
                )
            except BadRequest as exc:
                message = str(exc).lower()
                if "message is not modified" in message:
                    return message_id
                logger.debug(
                    "Falling back to send_message for cards keyboard",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            except TelegramError as exc:
                logger.debug(
                    "Failed to edit cards keyboard message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(exc).__name__,
                    },
                )
            else:
                if edited_id:
                    return edited_id
                return message_id

        return await self.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            request_category=RequestCategory.GENERAL,
        )

    async def announce_player_seats(
        self,
        *,
        chat_id: ChatId,
        players: Sequence[Player],
        dealer_index: int,
        message_id: Optional[MessageId] = None,
    ) -> Optional[MessageId]:
        """Send or update the seat map for the current hand."""

        sorted_players = sorted(
            players,
            key=lambda p: (p.seat_index if p.seat_index is not None else 0),
        )

        lines: List[str] = ["👥 *بازیکنان حاضر در میز*", ""]
        for player in sorted_players:
            seat_no = (player.seat_index or 0) + 1
            dealer_suffix = " — دیلر" if player.seat_index == dealer_index else ""
            lines.append(
                f"`{seat_no:>2}` │ {player.mention_markdown}{dealer_suffix}"
            )

        if not sorted_players:
            lines.append("هنوز بازیکنی در میز حضور ندارد.")

        text = "\n".join(lines)
        return await self.schedule_message_update(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=None,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
            request_category=RequestCategory.STAGE,
        )

    async def notify_admin(self, log_data: Dict[str, Any]) -> None:
        if not self._admin_chat_id:
            return
        context = self._build_context("notify_admin", chat_id=self._admin_chat_id)
        text = self._validator.normalize_text(
            json.dumps(log_data, ensure_ascii=False),
            parse_mode=None,
            context=context,
        )
        if text is None:
            logger.warning(
                "Dropping admin notification due to invalid payload",
                extra={"context": context},
            )
            return
        try:
            await self._messenger.send_message(
                chat_id=self._admin_chat_id,
                text=text,
                request_category=RequestCategory.GENERAL,
            )
        except Exception as e:
            logger.error(
                "Failed to notify admin",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": self._admin_chat_id,
                },
            )

    async def send_message_return_id(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        """Sends a message and returns its ID, or None if not applicable."""
        context = self._build_context("send_message_return_id", chat_id=chat_id)
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping send_message_return_id due to invalid text",
                extra={"context": context},
            )
            return None
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, None)
            return None
        try:
            message = await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                message_id = message.message_id
                await self._unmark_message_deleted(self._safe_int(message_id))
                return message_id
        except Exception as e:
            logger.error(
                "Error sending message and returning ID",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "request_params": {"text": text},
                },
            )
        return None

    async def last_message_edit_at(
        self, chat_id: ChatId, message_id: MessageId
    ) -> Optional[datetime.datetime]:
        """Return the timestamp of the last successful edit for ``message_id``."""

        accessor = getattr(self._messenger, "last_edit_timestamp", None)
        if accessor is None:
            return None
        try:
            result = accessor(chat_id, message_id)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return None
        if isinstance(result, datetime.datetime):
            return result
        return None


    async def send_message(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        parse_mode: str = ParseMode.MARKDOWN,  # <--- پارامتر جدید اضافه شد
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        context = self._build_context("send_message", chat_id=chat_id)
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=parse_mode,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping send_message due to invalid text",
                extra={"context": context},
            )
            return None
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, None)
            return None
        try:
            message = await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=parse_mode,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                message_id = message.message_id
                await self._unmark_message_deleted(self._safe_int(message_id))
                return message_id
        except Exception as e:
            logger.error(
                "Error sending message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "request_params": {"text": text},
                },
            )
        return None

    async def send_photo(self, chat_id: ChatId) -> None:
        try:
            with open("./assets/poker_hand.jpg", "rb") as f:
                await self._messenger.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    request_category=RequestCategory.MEDIA,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                )
        except Exception as e:
            logger.error(
                "Error sending photo",
                extra={"error_type": type(e).__name__, "chat_id": chat_id},
            )

    async def send_dice_reply(
        self, chat_id: ChatId, message_id: MessageId, emoji='🎲'
    ) -> Optional[Message]:
        context = self._build_context(
            "send_dice_reply", chat_id=chat_id, message_id=message_id
        )
        try:
            return await self._bot.send_dice(
                reply_to_message_id=message_id,
                chat_id=chat_id,
                disable_notification=True,
                emoji=emoji,
            )
        except Exception as e:
            logger.error(
                "Error sending dice reply",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "request_params": {"emoji": emoji},
                },
            )
            return None

    @property
    def request_metrics(self) -> RequestMetrics:
        """Expose the shared request metrics helper for orchestration code."""

        return self._request_metrics

    async def send_message_reply(
        self, chat_id: ChatId, message_id: MessageId, text: str
    ) -> None:
        context = self._build_context(
            "send_message_reply", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping send_message_reply due to invalid text",
                extra={"context": context},
            )
            return
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, message_id)
            return
        try:
            await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=None,
                request_category=RequestCategory.GENERAL,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                reply_to_message_id=message_id,
            )
        except Exception as e:
            logger.error(
                "Error sending message reply",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "request_params": {"text": text},
                },
            )

    async def edit_message_text(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        parse_mode: str = ParseMode.MARKDOWN,
        disable_web_page_preview: bool = False,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        """Edit a message using the central ``MessagingService``.

        Args:
            request_category: Request category used for metrics and rate limiting.
        """
        context = self._build_context(
            "edit_message_text", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=parse_mode,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping edit_message_text due to invalid text",
                extra={"context": context},
            )
            return None
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, message_id)
            return None
        try:
            result = await self._messenger.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=normalized_text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return result
        except Exception as exc:
            logger.error(
                "Failed to edit message text",
                extra={
                    "error_type": type(exc).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "context": context,
                },
            )
            return None

    async def delete_message(
        self, chat_id: ChatId, message_id: MessageId
    ) -> None:
        """Delete a message while keeping the cache in sync."""
        normalized_chat = self._safe_int(chat_id)
        normalized_message = self._safe_int(message_id)
        lock = await self._acquire_message_lock(chat_id, message_id)
        async with lock:
            try:
                await self._messenger.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    request_category=RequestCategory.DELETE,
                )
                await self._mark_message_deleted(normalized_message)
            except (BadRequest, Forbidden) as e:
                error_message = getattr(e, "message", None) or str(e) or ""
                normalized_error = error_message.lower()
                ignorable_messages = (
                    "message to delete not found",
                    "message can't be deleted",
                    "message cant be deleted",
                )
                if any(msg in normalized_error for msg in ignorable_messages):
                    logger.debug(
                        "Ignoring delete_message error",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": type(e).__name__,
                            "error_message": error_message,
                        },
                    )
                    return
                logger.warning(
                    "Failed to delete message",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_message": error_message,
                    },
                )
                return
            except Exception as e:
                logger.error(
                    "Error deleting message (%s)",
                    type(e).__name__,
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
                return
            finally:
                await self._clear_callback_tokens_for_message(
                    normalized_chat, normalized_message
                )

    async def send_single_card(
        self,
        chat_id: ChatId,
        card: Card,
        disable_notification: bool = True,
    ) -> None:
        """Send a single card image to the specified chat."""
        try:
            im_card = self._desk_generator._load_card_image(card)
            bio = BytesIO()
            bio.name = "card.png"
            im_card.save(bio, "PNG")
            bio.seek(0)
            await self._messenger.send_photo(
                chat_id=chat_id,
                photo=bio,
                request_category=RequestCategory.MEDIA,
                disable_notification=disable_notification,
            )
        except Exception as e:
            logger.error(
                "Error sending single card",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "card": card,
                },
            )

    async def send_desk_cards_img(
        self,
        chat_id: ChatId,
        cards: Cards,
        caption: str = "",
        disable_notification: bool = True,
        parse_mode: str = ParseMode.MARKDOWN,
        reply_markup: Optional[ReplyKeyboardMarkup] = None,
    ) -> Optional[Message]:
        """Sends desk cards image and returns the message object."""
        context = self._build_context("send_desk_cards_img", chat_id=chat_id)
        normalized_caption = self._validator.normalize_caption(
            caption,
            parse_mode=parse_mode,
            context=context,
        )
        if caption and normalized_caption is None:
            logger.warning(
                "Skipping send_desk_cards_img due to invalid caption",
                extra={"context": context},
            )
            return None
        try:
            desk_bytes = self._desk_generator.render_cached_png(cards)
            bio = BytesIO(desk_bytes)
            bio.name = "desk.png"
            bio.seek(0)
            message = await self._messenger.send_photo(
                chat_id=chat_id,
                photo=bio,
                caption=normalized_caption,
                request_category=RequestCategory.MEDIA,
                parse_mode=parse_mode,
                disable_notification=disable_notification,
                reply_markup=reply_markup,
            )
            if isinstance(message, Message):
                return message
        except Exception as e:
            logger.error(
                "Error sending desk cards image",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                },
            )
        return None

    async def edit_desk_cards_img(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        cards: Cards,
        caption: str = "",
        parse_mode: str = ParseMode.MARKDOWN,
        reply_markup: Optional[ReplyKeyboardMarkup] = None,
    ) -> Optional[Message]:
        """Edit an existing desk image or send a new one on failure.

        Returns the newly sent :class:`telegram.Message` when a new photo is
        sent instead of editing, otherwise ``None``.
        """
        try:
            desk_bytes = self._desk_generator.render_cached_png(cards)
            bio = BytesIO(desk_bytes)
            bio.name = "desk.png"
            bio.seek(0)
            context = self._build_context(
                "edit_desk_cards_img", chat_id=chat_id, message_id=message_id
            )
            normalized_caption = self._validator.normalize_caption(
                caption,
                parse_mode=parse_mode,
                context=context,
            )
            if caption and normalized_caption is None:
                logger.warning(
                    "Skipping edit_desk_cards_img due to invalid caption",
                    extra={"context": context},
                )
                return None
            media = InputMediaPhoto(
                media=bio, caption=normalized_caption, parse_mode=parse_mode
            )
            await self._bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=reply_markup,
            )
            return None
        except BadRequest:
            # If editing fails (e.g. original message no longer exists or is
            # invalid), fall back to sending a new photo so the board can still
            # be updated.
            try:
                msg = await self.send_desk_cards_img(
                    chat_id=chat_id,
                    cards=cards,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                return msg
            except Exception as e:
                logger.error(
                    "Error sending new desk cards image after edit failure",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
        except Exception as e:
            logger.error(
                "Error editing desk cards image",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        return None

    @staticmethod
    def define_check_call_action(game: Game, player: Player) -> PlayerAction:
        if player.round_rate >= game.max_round_rate:
            return PlayerAction.CHECK
        return PlayerAction.CALL

    async def update_turn_message(
        self,
        *,
        chat_id: ChatId,
        game: Game,
        player: Player,
        money: Money,
        message_id: Optional[MessageId] = None,
        recent_actions: Optional[List[str]] = None,
    ) -> TurnMessageUpdate:
        """Send or edit the persistent turn message for the active player."""

        call_amount = max(game.max_round_rate - player.round_rate, 0)
        call_action = self.define_check_call_action(game, player)
        call_text = (
            f"{call_action.value} ({call_amount}$)"
            if call_action == PlayerAction.CALL and call_amount > 0
            else call_action.value
        )

        seat_number = (player.seat_index or 0) + 1
        board_line = self._format_card_line("🃏 Board", game.cards_table)

        stage_labels = {
            GameState.ROUND_PRE_FLOP: "Pre-Flop",
            GameState.ROUND_FLOP: "Flop",
            GameState.ROUND_TURN: "Turn",
            GameState.ROUND_RIVER: "River",
        }
        stage_name = stage_labels.get(game.state, "Pre-Flop")

        info_lines = [
            f"🎯 **نوبت:** {player.mention_markdown} (صندلی {seat_number})",
            f"🎰 **مرحله بازی:** {stage_name}",
            "",
            board_line,
            f"💰 **پات فعلی:** `{game.pot}$`",
            f"💵 **موجودی شما:** `{money}$`",
            f"🎲 **بِت فعلی شما:** `{player.round_rate}$`",
            f"📈 **حداکثر شرط این دور:** `{game.max_round_rate}$`",
            "",
            "⬇️ **از دکمه‌های زیر برای اقدام استفاده کنید.**",
        ]

        history = list(
            (recent_actions if recent_actions is not None else game.last_actions)[-5:]
        )
        if history:
            info_lines.append("")
            info_lines.append("🎬 **اکشن‌های اخیر:**")
            info_lines.extend(f"• {action}" for action in history)

        text = "\n".join(info_lines)

        reply_markup = await self._build_turn_keyboard(call_text, call_action)

        new_message_id = await self.schedule_message_update(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            disable_notification=message_id is not None,
            request_category=RequestCategory.TURN,
        )

        return TurnMessageUpdate(
            message_id=new_message_id,
            call_label=call_text,
            call_action=call_action,
            board_line=board_line,
        )

    async def _build_turn_keyboard(
        self, call_text: str, call_action: PlayerAction
    ) -> InlineKeyboardMarkup:
        """Return a cached inline keyboard for the active player's actions."""

        cache_key = f"{call_action.value}|{call_text}"
        async with self._inline_keyboard_cache_lock:
            cached = self._inline_keyboard_cache.get(cache_key)
            if cached is not None:
                logger.debug(
                    "Inline keyboard cache hit %s (size %s/%s)",
                    cache_key,
                    self._inline_keyboard_cache.currsize,
                    self._inline_keyboard_cache.maxsize,
                )
                return cached

            keyboard = [
                [
                    InlineKeyboardButton(
                        text=PlayerAction.FOLD.value,
                        callback_data=PlayerAction.FOLD.value,
                    ),
                    InlineKeyboardButton(
                        text=PlayerAction.ALL_IN.value,
                        callback_data=PlayerAction.ALL_IN.value,
                    ),
                    InlineKeyboardButton(
                        text=call_text,
                        callback_data=call_action.value,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=str(PlayerAction.SMALL.value),
                        callback_data=str(PlayerAction.SMALL.value),
                    ),
                    InlineKeyboardButton(
                        text=str(PlayerAction.NORMAL.value),
                        callback_data=str(PlayerAction.NORMAL.value),
                    ),
                    InlineKeyboardButton(
                        text=str(PlayerAction.BIG.value),
                        callback_data=str(PlayerAction.BIG.value),
                    ),
                ],
            ]
            markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
            self._inline_keyboard_cache[cache_key] = markup
            logger.debug(
                "Turn keyboard cache size %s/%s",
                self._inline_keyboard_cache.currsize,
                self._inline_keyboard_cache.maxsize,
            )
            return markup


    async def edit_message_reply_markup(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
    ) -> bool:
        """Update a message's inline keyboard while handling common failures."""
        if not message_id:
            return False
        try:
            return await self._messenger.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
                request_category=RequestCategory.INLINE,
            )
        except BadRequest as e:
            err = str(e).lower()
            if "message is not modified" in err:
                logger.info(
                    "Reply markup already up to date",
                    extra={"chat_id": chat_id, "message_id": message_id},
                )
                return True
            if "message to edit not found" not in err:
                logger.warning(
                    "BadRequest editing reply markup",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
        except Forbidden as e:
            logger.info(
                "Cannot edit reply markup, bot unauthorized",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        except Exception as e:
            logger.error(
                "Unexpected error editing reply markup",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        return False

    async def remove_markup(self, chat_id: ChatId, message_id: MessageId) -> None:
        """حذف دکمه‌های اینلاین از یک پیام و فیلتر کردن ارورهای رایج."""
        if not message_id:
            return
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
            )
        except BadRequest as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message is not modified" in err:
                logger.info(
                    "Markup already removed or message not found",
                    extra={"message_id": message_id},
                )
            else:
                logger.warning(
                    "BadRequest removing markup",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
        except Forbidden as e:
            logger.info(
                "Cannot edit markup, bot unauthorized",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        except Exception as e:
            logger.error(
                "Unexpected error removing markup",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
    
    async def remove_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        """Stub for backward compatibility; message deletion is disabled."""
        logger.debug(
            "remove_message called for chat_id %s message_id %s but deletion is disabled",
            chat_id,
            message_id,
        )

    async def remove_message_delayed(
        self, chat_id: ChatId, message_id: MessageId, delay: float = 3.0
    ) -> None:
        """Stub for backward compatibility; message deletion is disabled."""
        logger.debug(
            "remove_message_delayed called for chat_id %s message_id %s but deletion is disabled",
            chat_id,
            message_id,
        )
        
    async def send_showdown_results(self, chat_id: ChatId, game: Game, winners_by_pot: list) -> None:
        """
        پیام نهایی نتایج بازی را با فرمت زیبا ساخته و ارسال می‌کند.
        این نسخه برای مدیریت ساختار داده جدید Side Pot (لیست دیکشنری‌ها) به‌روز شده است.
        """
        final_message = "🏆 *نتایج نهایی و نمایش کارت‌ها*\n\n"

        if not winners_by_pot:
            final_message += "خطایی در تعیین برنده رخ داد. پات تقسیم نشد."
        else:
            # نام‌گذاری پات‌ها برای نمایش بهتر (اصلی، فرعی ۱، فرعی ۲ و...)
            pot_names = ["*پات اصلی*", "*پات فرعی ۱*", "*پات فرعی ۲*", "*پات فرعی ۳*"]
            
            # FIX: حلقه برای پردازش صحیح "لیست دیکشنری‌ها" اصلاح شد
            for i, pot_data in enumerate(winners_by_pot):
                pot_amount = pot_data.get("amount", 0)
                winners_info = pot_data.get("winners", [])

                if pot_amount == 0 or not winners_info:
                    continue
                
                # انتخاب نام پات بر اساس ترتیب آن
                pot_name = pot_names[i] if i < len(pot_names) else f"*پات فرعی {i}*"
                final_message += f"💰 {pot_name}: {pot_amount}$\n"
                
                win_amount_per_player = pot_amount // len(winners_info)

                for winner in winners_info:
                    player = winner.get("player")
                    if not player: continue # اطمینان از وجود بازیکن

                    hand_type = winner.get('hand_type')
                    hand_cards = winner.get('hand_cards', [])
                    
                    hand_name_data = HAND_NAMES_TRANSLATIONS.get(hand_type, {})
                    hand_display_name = f"{hand_name_data.get('emoji', '🃏')} {hand_name_data.get('fa', 'دست نامشخص')}"

                    final_message += (
                        f"  - {player.mention_markdown} با دست {hand_display_name} "
                        f"برنده *{win_amount_per_player}$* شد.\n"
                    )
                    final_message += (
                        f"    کارت‌ها: {self._render_cards(hand_cards)}\n"
                    )
                
                final_message += "\n" # یک خط فاصله برای جداسازی پات‌ها

        final_message += "⎯" * 20 + "\n"
        board_line = self._format_card_line("🃏 Board", game.cards_table)
        final_message += f"{board_line}\n\n"

        final_message += "🤚 *کارت‌های سایر بازیکنان:*\n"
        all_players_in_hand = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN, PlayerState.FOLD))

        # FIX: استخراج صحیح ID برندگان از ساختار داده جدید
        winner_user_ids = set()
        for pot_data in winners_by_pot:
            for winner_info in pot_data.get("winners", []):
                if "player" in winner_info:
                    winner_user_ids.add(winner_info["player"].user_id)

        for p in all_players_in_hand:
            if p.user_id not in winner_user_ids:
                card_display = (
                    self._render_cards(p.cards)
                    if p.cards
                    else 'کارت‌ها نمایش داده نشد'
                )
                state_info = " (فولد)" if p.state == PlayerState.FOLD else ""
                final_message += f"  - {p.mention_markdown}{state_info}: {card_display}\n"

        # ارسال تصویر نهایی میز همراه با کپشن نتایج. اگر طول پیام از
        # محدودیت کپشن تلگرام بیشتر شد، ادامه پیام در یک پیام متنی جداگانه
        # ارسال می‌شود تا تعداد پیام‌ها حداقل باقی بماند.
        caption_limit = 1024
        caption = final_message[:caption_limit]
        await self.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=caption,
        )
        if len(final_message) > caption_limit:
            await self.send_message(
                chat_id=chat_id,
                text=final_message[caption_limit:],
                parse_mode="Markdown",
            )

    async def send_new_hand_ready_message(self, chat_id: ChatId) -> None:
        """پیام آمادگی برای دست جدید را ارسال می‌کند."""
        message = (
            "♻️ دست به پایان رسید. بازیکنان باقی‌مانده برای دست بعد حفظ شدند.\n"
            "برای شروع دست جدید، /start را بزنید یا بازیکنان جدید می‌توانند با دکمهٔ «نشستن سر میز» وارد شوند."
        )
        context = self._build_context(
            "send_new_hand_ready_message", chat_id=chat_id
        )
        normalized_message = self._validator.normalize_text(
            message,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_message is None:
            logger.warning(
                "Skipping new hand ready message due to invalid text",
                extra={"context": context},
            )
            return
        reply_keyboard = ReplyKeyboardMarkup(
            keyboard=[["/start", "نشستن سر میز"], ["/stop"]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=False,
        )
        try:
            await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_message,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                disable_web_page_preview=True,
                reply_markup=reply_keyboard,
            )
        except Exception as e:
            logger.error(
                "Error sending new hand ready message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                },
            )
