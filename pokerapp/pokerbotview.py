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
)
from dataclasses import dataclass
import asyncio
import hashlib
import logging
import json
import time
from cachetools import LRUCache
from pokerapp.config import DEFAULT_RATE_LIMIT_PER_MINUTE, DEFAULT_RATE_LIMIT_PER_SECOND
from pokerapp.winnerdetermination import HAND_NAMES_TRANSLATIONS
from pokerapp.desk import DeskImageGenerator
from pokerapp.cards import Cards, Card
from pokerapp.entities import (
    Game,
    Player,
    PlayerAction,
    MessageId,
    ChatId,
    Mention,
    Money,
    PlayerState,
)
from pokerapp.telegram_validation import TelegramPayloadValidator
from pokerapp.utils.messaging_service import MessagingService


logger = logging.getLogger(__name__)

_CARD_SPACER = "     "


@dataclass(slots=True)
class _CacheRecord:
    value: bool
    timestamp: float


class _TimedLRUCache(LRUCache):
    """LRU cache with a simple time-to-live eviction policy."""

    def __init__(self, *, maxsize: int, ttl: float) -> None:
        super().__init__(maxsize=maxsize)
        self._ttl = ttl

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        try:
            record = LRUCache.__getitem__(self, key)
        except KeyError:
            return False
        if time.monotonic() - record.timestamp <= self._ttl:
            return True
        LRUCache.__delitem__(self, key)
        return False

    def __getitem__(self, key: object) -> bool:  # type: ignore[override]
        record = LRUCache.__getitem__(self, key)
        if time.monotonic() - record.timestamp > self._ttl:
            LRUCache.__delitem__(self, key)
            raise KeyError(key)
        return record.value

    def get(self, key: object, default: Optional[bool] = None) -> Optional[bool]:
        try:
            return self.__getitem__(key)
        except KeyError:
            return default

    def __setitem__(self, key: object, value: bool) -> None:  # type: ignore[override]
        record = _CacheRecord(value=value, timestamp=time.monotonic())
        LRUCache.__setitem__(self, key, record)

    def popitem(self, last: bool = True) -> Tuple[object, bool]:  # type: ignore[override]
        key, record = LRUCache.popitem(self, last=last)
        return key, record.value


class PokerBotViewer:
    _ZERO_WIDTH_SPACE = "\u2063"

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
        rate_limiter_delay: Optional[float] = None,
        update_debounce: float = 0.3,
    ):
        # ``update_debounce`` is kept for compatibility with previous
        # initialisers but no longer used after the messaging rewrite.
        _ = update_debounce

        self._bot = bot
        self._desk_generator = DeskImageGenerator()
        self._admin_chat_id = admin_chat_id
        self._validator = TelegramPayloadValidator(
            logger_=logger.getChild("validation")
        )
        self._messenger = MessagingService(
            bot, logger_=logger.getChild("messaging_service")
        )
        # Legacy rate-limit attributes are retained for backwards compatibility
        # with configuration code but do not influence runtime behaviour.
        self._legacy_rate_limit_per_minute = rate_limit_per_minute
        self._legacy_rate_limit_per_second = rate_limit_per_second
        self._legacy_rate_limiter_delay = rate_limiter_delay

        self._message_update_cache: _TimedLRUCache = _TimedLRUCache(
            maxsize=500,
            ttl=3.0,
        )
        self._message_update_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._message_update_guard = asyncio.Lock()

    def _payload_hash(
        self,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
    ) -> str:
        markup_hash = self._serialize_markup(reply_markup) or ""
        payload = f"{text}|{markup_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_int(value: Optional[int | str]) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

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

        payload_hash = self._payload_hash(normalized_text, reply_markup)
        normalized_chat = self._safe_int(chat_id)
        normalized_message = (
            self._safe_int(message_id) if message_id is not None else 0
        )
        cache_key: Tuple[int, int, str] = (
            normalized_chat,
            normalized_message,
            payload_hash,
        )
        if message_id is not None and self._message_update_cache.get(cache_key):
            return message_id

        lock = await self._acquire_message_lock(chat_id, message_id)
        async with lock:
            if message_id is not None and self._message_update_cache.get(cache_key):
                return message_id

            try:
                if message_id is None:
                    result = await self._messenger.send_message(
                        chat_id=chat_id,
                        text=normalized_text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                        disable_notification=disable_notification,
                    )
                    new_message_id: Optional[MessageId] = getattr(
                        result, "message_id", None
                    )
                else:
                    result = await self._messenger.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=normalized_text,
                        reply_markup=reply_markup,
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
            cache_key = (
                normalized_chat,
                normalized_new_message,
                payload_hash,
            )
            self._message_update_cache[cache_key] = True
            return new_message_id

    @staticmethod
    def _render_cards(cards: Sequence[Card]) -> str:
        if not cards:
            return "—"
        return _CARD_SPACER.join(str(card) for card in cards)

    @classmethod
    def _format_card_line(cls, label: str, cards: Sequence[Card]) -> str:
        return f"{label}: {cls._render_cards(cards)}"

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
        return await self._update_message(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=None,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
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
        try:
            message = await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                return message.message_id
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


    async def send_message(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        parse_mode: str = ParseMode.MARKDOWN,  # <--- پارامتر جدید اضافه شد
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
        try:
            message = await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                return message.message_id
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
        async def _send():
            with open("./assets/poker_hand.jpg", "rb") as f:
                return await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                )
        try:
            # Execute immediately; aiogram/PTB async stack manages throughput.
            await _send()
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
        try:
            await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=None,
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
    ) -> Optional[MessageId]:
        """Edit a message using the central ``MessagingService``."""
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
        try:
            result = await self._messenger.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=normalized_text,
                reply_markup=reply_markup,
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
        try:
            await self._messenger.delete_message(chat_id=chat_id, message_id=message_id)
        except (BadRequest, Forbidden) as e:
            error_message = getattr(e, "message", None) or str(e) or ""
            normalized_message = error_message.lower()
            ignorable_messages = (
                "message to delete not found",
                "message can't be deleted",
                "message cant be deleted",
            )
            if any(msg in normalized_message for msg in ignorable_messages):
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
        except Exception as e:
            logger.error(
                "Error deleting message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
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
            await self._bot.send_photo(
                chat_id=chat_id,
                photo=bio,
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
            message = await self._bot.send_photo(
                chat_id=chat_id,
                photo=bio,
                caption=normalized_caption,
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
    def _derive_stage_from_table(table_cards: Cards, stage: Optional[str] = None) -> str:
        """Infer the current poker street from the number of table cards.

        When an explicit ``stage`` value is provided it takes precedence. This
        helper guarantees a consistent stage string (``preflop``, ``flop``,
        ``turn`` or ``river``) so that all keyboards use the same labelling.
        """

        if stage:
            return stage

        cards_count = len(table_cards)
        if cards_count >= 5:
            return "river"
        if cards_count == 4:
            return "turn"
        if cards_count >= 3:
            return "flop"
        return "preflop"

    @staticmethod
    def _get_table_markup(
        table_cards: Cards, stage: Optional[str] = None
    ) -> ReplyKeyboardMarkup:
        """Creates a keyboard displaying table cards and stage buttons."""

        resolved_stage = PokerBotViewer._derive_stage_from_table(table_cards, stage)
        cards_row = [str(card) for card in table_cards] if table_cards else ["❔"]

        stage_map = {
            "preflop": "پری فلاپ",
            "flop": "فلاپ",
            "turn": "ترن",
            "river": "ریور",
        }

        stages = [
            stage_map["preflop"],
            stage_map["flop"],
            stage_map["turn"],
            stage_map["river"],
        ]

        stages = [
            (f"✅ {label}" if label == stage_map.get(resolved_stage, label) else label)
            for label in stages
        ]

        return ReplyKeyboardMarkup(
            keyboard=[cards_row, stages],
            selective=False,
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    @staticmethod
    def _get_hand_and_board_markup(
        hand: Cards, table_cards: Cards, stage: Optional[str] = None
    ) -> ReplyKeyboardMarkup:
        """Combine player's hand, table cards and stage buttons in one keyboard.

        این کیبورد در پیام نامرئی گروه برای هر بازیکن استفاده می‌شود تا او هم‌زمان
        دست و کارت‌های میز را در منوی کیبوردی ببیند بدون آن‌که پیام قابل مشاهده‌ای
        در گروه باقی بماند. ردیف سوم مراحل بازی را برای ناوبری سریع نگه می‌دارد.
        """

        resolved_stage = PokerBotViewer._derive_stage_from_table(table_cards, stage)
        hand_row = [str(c) for c in hand]
        table_row = [str(c) for c in table_cards] if table_cards else ["❔"]

        stage_map = {
            "preflop": "پری فلاپ",
            "flop": "فلاپ",
            "turn": "ترن",
            "river": "ریور",
        }
        stages = [
            stage_map["preflop"],
            stage_map["flop"],
            stage_map["turn"],
            stage_map["river"],
        ]
        stage_row = []
        for s in stages:
            label = f"🔁 {s}"
            if stage_map.get(resolved_stage) == s:
                label = f"✅ {s}"
            stage_row.append(label)

        return ReplyKeyboardMarkup(
            keyboard=[hand_row, table_row, stage_row],
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    async def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: str | None = None,
            table_cards: Cards | None = None,
            hide_hand_text: bool = False,
            stage: str = "",
            message_id: MessageId | None = None,
            reply_to_ready_message: bool = True,
    ) -> Optional[MessageId]:
        resolved_stage = PokerBotViewer._derive_stage_from_table(table_cards or [], stage)
        markup = self._get_hand_and_board_markup(
            cards, table_cards or [], resolved_stage
        )
        if hide_hand_text:
            hidden_mention = PokerBotViewer._build_hidden_mention(mention_markdown)
            if hidden_mention:
                hidden_text = hidden_mention + PokerBotViewer._ZERO_WIDTH_SPACE
            else:
                hidden_text = PokerBotViewer._ZERO_WIDTH_SPACE

            context_hidden = self._build_context(
                "send_cards_hidden", chat_id=chat_id, message_id=message_id
            )
            normalized_hidden_text = self._validator.normalize_text(
                hidden_text,
                parse_mode=ParseMode.MARKDOWN,
                context=context_hidden,
            )
            if normalized_hidden_text is None:
                logger.warning(
                    "Skipping hidden cards update due to invalid text",
                    extra={"context": context_hidden},
                )
                return None

            if message_id:
                edited_id = await self.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=normalized_hidden_text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
                if edited_id:
                    return edited_id

            try:
                reply_kwargs = {}
                if reply_to_ready_message and ready_message_id and not message_id:
                    reply_kwargs["reply_to_message_id"] = ready_message_id
                message = await self._messenger.send_message(
                    chat_id=chat_id,
                    text=normalized_hidden_text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                    **reply_kwargs,
                )

                new_message_id: Optional[MessageId] = (
                    getattr(message, "message_id", None) if message else None
                )

                if message_id and new_message_id and new_message_id != message_id:
                    try:
                        await self.delete_message(chat_id, message_id)
                    except Exception:
                        logger.debug(
                            "Failed to delete previous hidden cards message",
                            extra={
                                "chat_id": chat_id,
                                "request_params": {"message_id": message_id},
                            },
                        )

                if new_message_id:
                    return new_message_id
            except Exception as e:
                logger.error(
                    "Error sending hidden cards keyboard",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                    },
                )
            return None

        message_body = (
            f"{self._format_card_line('🃏 دست', cards)}\n"
            f"{self._format_card_line('🃎 میز', table_cards or [])}"
        )
        message_text = f"{mention_markdown}\n{message_body}"

        context_visible = self._build_context(
            "send_cards", chat_id=chat_id, message_id=message_id
        )
        normalized_message_text = self._validator.normalize_text(
            message_text,
            parse_mode=ParseMode.MARKDOWN,
            context=context_visible,
        )
        if normalized_message_text is None:
            logger.warning(
                "Skipping visible cards message due to invalid text",
                extra={"context": context_visible},
            )
            return None

        if message_id:
            updated_id = await self.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=normalized_message_text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
            )
            if updated_id:
                return updated_id

        try:
            reply_kwargs = {}
            if reply_to_ready_message and ready_message_id and not message_id:
                reply_kwargs["reply_to_message_id"] = ready_message_id
            message = await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_message_text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                **reply_kwargs,
            )
            new_message_id: Optional[MessageId] = getattr(message, "message_id", None)

            if message_id and new_message_id and new_message_id != message_id:
                try:
                    await self.delete_message(chat_id, message_id)
                except Exception:
                    logger.debug(
                        "Failed to delete previous cards message",
                        extra={
                            "chat_id": chat_id,
                            "request_params": {"message_id": message_id},
                        },
                    )

            if new_message_id:
                return new_message_id
        except Exception as e:
            logger.error(
                "Error sending cards",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
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
    ) -> Optional[MessageId]:
        """Send or edit the persistent turn message for the active player."""

        call_amount = max(game.max_round_rate - player.round_rate, 0)
        call_action = self.define_check_call_action(game, player)
        call_text = (
            f"{call_action.value} ({call_amount}$)"
            if call_action == PlayerAction.CALL and call_amount > 0
            else call_action.value
        )
        keyboard = self._build_turn_keyboard(call_text, call_action)

        seat_number = (player.seat_index or 0) + 1
        board_line = self._format_card_line("🃏 Board", game.cards_table)

        info_lines = [
            f"🎯 **نوبت بازی {player.mention_markdown} (صندلی {seat_number})**",
            "",
            board_line,
            f"💰 **پات فعلی:** `{game.pot}$`",
            f"💵 **موجودی شما:** `{money}$`",
            f"🎲 **بِت فعلی شما:** `{player.round_rate}$`",
            f"📈 **حداکثر شرط این دور:** `{game.max_round_rate}$`",
            "",
            "⬇️ حرکت خود را انتخاب کنید:",
        ]

        history = list(
            (recent_actions if recent_actions is not None else game.last_actions)[-3:]
        )
        if history:
            info_lines.append("")
            info_lines.append("🎬 **اکشن‌های اخیر:**")
            info_lines.extend(f"• {action}" for action in history)

        text = "\n".join(info_lines)

        return await self._update_message(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            disable_notification=message_id is not None,
        )

    @staticmethod
    def _build_turn_keyboard(
        call_text: str, call_action: PlayerAction
    ) -> InlineKeyboardMarkup:
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
        return InlineKeyboardMarkup(inline_keyboard=keyboard)


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
        board_cards = self._render_cards(game.cards_table)
        final_message += f"🃏 *کارت‌های روی میز:* {board_cards}\n\n"

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
