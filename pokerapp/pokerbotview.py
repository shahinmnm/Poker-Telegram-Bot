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
from typing import Optional, Callable, Awaitable, Dict, Any, List, Deque, Tuple
from dataclasses import dataclass, field
from collections import defaultdict, deque
import asyncio
import logging
import json
import time
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


logger = logging.getLogger(__name__)


class RateLimitedSender:
    """Serializes Telegram requests with per-chat rate limiting.

    A simple token-bucket is maintained for each chat to ensure that no more
    than ``max_per_minute`` messages are sent within a rolling minute. When the
    bucket is close to exhaustion, an additional delay is injected between
    messages to reduce the likelihood of hitting Telegram's ``HTTP 429``. The
    queue of pending messages is preserved even when a ``RetryAfter`` error is
    received.
    """

    def __init__(
        self,
        delay: Optional[float] = None,
        *,
        max_retries: int = 3,
        error_delay: float = 0.1,
        notify_admin: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        max_per_minute: int = 20,
        max_per_second: Optional[float] = None,
    ):
        self._global_lock = asyncio.Lock()
        self._max_retries = max_retries
        self._error_delay = error_delay
        self._notify_admin = notify_admin
        self._max_tokens = max_per_minute
        self._refill_rate = max_per_minute / 60.0  # tokens per second
        per_second_limit = max_per_second if max_per_second is not None else self._refill_rate
        if per_second_limit <= 0:
            computed_delay = 0.0
        else:
            computed_delay = 1.0 / per_second_limit
        self._delay = delay if delay is not None else computed_delay
        self._buckets: Dict[ChatId, Dict[str, float]] = {}
        self._locks: Dict[ChatId, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_available_ts: Dict[ChatId, float] = defaultdict(float)

    @dataclass
    class _TokenPermit:
        remaining: float
        wait_before: float

    def _get_lock(self, chat_id: Optional[ChatId]) -> asyncio.Lock:
        if chat_id is None:
            return self._global_lock
        return self._locks[chat_id]

    async def _wait_for_token(self, chat_id: ChatId) -> "RateLimitedSender._TokenPermit":
        lock = self._get_lock(chat_id)
        while True:
            async with lock:
                bucket = self._buckets.setdefault(
                    chat_id, {"tokens": self._max_tokens, "ts": time.monotonic()}
                )
                now = time.monotonic()
                elapsed = now - bucket["ts"]
                bucket["tokens"] = min(
                    self._max_tokens, bucket["tokens"] + elapsed * self._refill_rate
                )
                bucket["ts"] = now
                if bucket["tokens"] >= 1:
                    bucket["tokens"] -= 1
                    remaining = bucket["tokens"]
                    delay = self._delay
                    if remaining < 5:
                        shortage = max(0.0, 5 - remaining)
                        delay += min(shortage * 0.2, 0.9)
                    next_available = self._next_available_ts[chat_id]
                    scheduled_time = max(now, next_available)
                    wait_before = max(0.0, scheduled_time - now)
                    cooldown = delay
                    self._next_available_ts[chat_id] = scheduled_time + cooldown
                    return RateLimitedSender._TokenPermit(
                        remaining=remaining, wait_before=wait_before
                    )
                wait_time = (1 - bucket["tokens"]) / self._refill_rate
            await asyncio.sleep(wait_time)

    async def send(
        self, func: Callable[..., Awaitable[Any]], *args, chat_id: Optional[ChatId] = None, **kwargs
    ):
        """Execute ``func`` with args, respecting per-chat limits and retrying."""
        notifications: List[Dict[str, Any]] = []
        error_to_raise: Optional[TelegramError] = None
        should_notify_failure = False
        should_return_none = False

        attempts = 0
        last_error: Optional[TelegramError] = None
        while True:
            permit: Optional[RateLimitedSender._TokenPermit] = None
            if chat_id is not None:
                permit = await self._wait_for_token(chat_id)
                if permit.wait_before > 0:
                    await asyncio.sleep(permit.wait_before)
            try:
                result = await func(*args, **kwargs)
                return result
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramError as e:
                if attempts >= self._max_retries:
                    last_error = e
                    should_notify_failure = True
                    break
                attempts += 1
                await asyncio.sleep(self._error_delay)
            except Exception as e:
                logger.error(
                    "Unexpected error in RateLimitedSender.send",
                    extra={
                        "error_type": type(e).__name__,
                        "request_params": {"args": args, "kwargs": kwargs},
                    },
                )
                if self._notify_admin:
                    notifications.append(
                        {
                            "event": "rate_limiter_error",
                            "error": str(e),
                            "error_type": type(e).__name__,
                        }
                    )
                last_error = e if isinstance(e, TelegramError) else None
                should_notify_failure = True
                if last_error is None:
                    should_return_none = True
                break
        if should_notify_failure:
            notifications.append(
                {
                    "event": "rate_limiter_failed",
                    "request_params": {"args": str(args), "kwargs": str(kwargs)},
                }
            )
            logger.warning(
                "RateLimitedSender.send failed after retries",
                extra={"request_params": {"args": args, "kwargs": kwargs}},
            )
            if last_error is not None:
                error_to_raise = last_error
            else:
                should_return_none = True

        if self._notify_admin:
            for payload in notifications:
                await self._notify_admin(payload)

        if error_to_raise is not None:
            raise error_to_raise

        if should_return_none:
            return None


class ChatUpdateQueue:
    """Debounces per-chat Telegram edits to avoid redundant updates."""

    @dataclass
    class _LastPayload:
        text: Optional[str] = None
        markup_hash: Optional[str] = None
        parse_mode: Optional[str] = None

    @dataclass
    class _PendingUpdate:
        chat_id: ChatId
        message_id: MessageId
        text: str
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup]
        parse_mode: Optional[str]
        context: str
        ready: asyncio.Event = field(default_factory=asyncio.Event)
        future: asyncio.Future = field(
            default_factory=lambda: asyncio.get_running_loop().create_future()
        )
        timer_task: Optional[asyncio.Task] = None
        markup_hash: Optional[str] = None
        disable_web_page_preview: bool = False

    @dataclass
    class _ChatState:
        pending: Dict[MessageId, "ChatUpdateQueue._PendingUpdate"] = field(
            default_factory=dict
        )
        order: Deque[MessageId] = field(default_factory=deque)
        new_item: asyncio.Event = field(default_factory=asyncio.Event)
        worker: Optional[asyncio.Task] = None

    def __init__(
        self,
        bot: Bot,
        rate_limiter: RateLimitedSender,
        *,
        debounce_window: float = 0.3,
    ) -> None:
        self._bot = bot
        self._rate_limiter = rate_limiter
        self._debounce = debounce_window
        self._chat_states: Dict[ChatId, ChatUpdateQueue._ChatState] = {}
        self._last_payloads: Dict[
            Tuple[ChatId, MessageId], ChatUpdateQueue._LastPayload
        ] = {}

    def _serialize_markup(
        self, markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup]
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

    def _ensure_state(self, chat_id: ChatId) -> "ChatUpdateQueue._ChatState":
        state = self._chat_states.get(chat_id)
        if state is None:
            state = ChatUpdateQueue._ChatState()
            self._chat_states[chat_id] = state
        if state.worker is None or state.worker.done():
            state.worker = asyncio.create_task(self._chat_worker(chat_id, state))
        return state

    async def _chat_worker(
        self, chat_id: ChatId, state: "ChatUpdateQueue._ChatState"
    ) -> None:
        try:
            while True:
                if not state.order:
                    state.new_item.clear()
                    if not state.order:
                        await state.new_item.wait()
                        continue
                message_id = state.order[0]
                pending = state.pending.get(message_id)
                if pending is None:
                    state.order.popleft()
                    continue
                await pending.ready.wait()
                result = await self._execute_pending(pending)
                state.pending.pop(message_id, None)
                state.order.popleft()
                if result is not None:
                    self._last_payloads[(chat_id, result)] = ChatUpdateQueue._LastPayload(
                        text=pending.text,
                        markup_hash=pending.markup_hash,
                        parse_mode=pending.parse_mode,
                    )
                if pending.timer_task and not pending.timer_task.done():
                    pending.timer_task.cancel()
                if not pending.future.done():
                    pending.future.set_result(result)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "Unexpected error in ChatUpdateQueue worker", extra={"chat_id": chat_id}
            )

    async def _execute_pending(
        self, pending: "ChatUpdateQueue._PendingUpdate"
    ) -> Optional[MessageId]:
        try:
            response = await self._rate_limiter.send(
                lambda: self._bot.edit_message_text(
                    chat_id=pending.chat_id,
                    message_id=pending.message_id,
                    text=pending.text,
                    reply_markup=pending.reply_markup,
                    parse_mode=pending.parse_mode,
                    disable_web_page_preview=pending.disable_web_page_preview,
                ),
                chat_id=pending.chat_id,
            )
            if isinstance(response, Message):
                return response.message_id
            if response is True:
                return pending.message_id
            if isinstance(response, int):
                return response
        except BadRequest as e:
            err = str(e).lower()
            if "message is not modified" in err:
                return pending.message_id
            logger.debug(
                "ChatUpdateQueue edit rejected",
                extra={
                    "chat_id": pending.chat_id,
                    "message_id": pending.message_id,
                    "context": pending.context,
                    "error_type": type(e).__name__,
                },
            )
        except Exception as e:
            logger.error(
                "ChatUpdateQueue failed to edit message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": pending.chat_id,
                    "message_id": pending.message_id,
                    "context": pending.context,
                },
            )
        return None

    def record_payload(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        *,
        text: Optional[str],
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
        parse_mode: Optional[str],
    ) -> None:
        markup_hash = self._serialize_markup(reply_markup)
        self._last_payloads[(chat_id, message_id)] = ChatUpdateQueue._LastPayload(
            text=text,
            markup_hash=markup_hash,
            parse_mode=parse_mode,
        )

    async def enqueue_text_edit(
        self,
        *,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
        parse_mode: Optional[str],
        context: str,
        force: bool = False,
        disable_web_page_preview: bool = False,
    ) -> Optional[MessageId]:
        markup_hash = self._serialize_markup(reply_markup)
        last_payload = self._last_payloads.get((chat_id, message_id))
        if (
            last_payload
            and not force
            and last_payload.text == text
            and last_payload.markup_hash == markup_hash
            and last_payload.parse_mode == parse_mode
        ):
            return message_id

        state = self._ensure_state(chat_id)
        pending = state.pending.get(message_id)
        if pending is None:
            pending = ChatUpdateQueue._PendingUpdate(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                context=context,
                markup_hash=markup_hash,
                disable_web_page_preview=disable_web_page_preview,
            )
            state.pending[message_id] = pending
            state.order.append(message_id)
            state.new_item.set()
        else:
            pending.text = text
            pending.reply_markup = reply_markup
            pending.parse_mode = parse_mode
            pending.context = context
            pending.markup_hash = markup_hash
            pending.disable_web_page_preview = disable_web_page_preview
            pending.ready.clear()
        if pending.timer_task and not pending.timer_task.done():
            pending.timer_task.cancel()
        pending.timer_task = asyncio.create_task(
            self._schedule_ready(chat_id, message_id, pending)
        )
        return await asyncio.shield(pending.future)

    async def _schedule_ready(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        pending: "ChatUpdateQueue._PendingUpdate",
    ) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        pending.ready.set()
        state = self._chat_states.get(chat_id)
        if state:
            state.new_item.set()


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
        self._bot = bot
        self._desk_generator = DeskImageGenerator()
        self._admin_chat_id = admin_chat_id
        self._validator = TelegramPayloadValidator(
            logger_=logger.getChild("validation")
        )
        self._rate_limiter = RateLimitedSender(
            delay=rate_limiter_delay,
            error_delay=0.1,
            notify_admin=self.notify_admin,
            max_per_minute=rate_limit_per_minute,
            max_per_second=rate_limit_per_second,
        )
        self._update_queue = ChatUpdateQueue(
            bot,
            self._rate_limiter,
            debounce_window=update_debounce,
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
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=self._admin_chat_id,
                    text=text,
                ),
                chat_id=self._admin_chat_id,
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
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    parse_mode=ParseMode.MARKDOWN,
                    text=normalized_text,
                    reply_markup=reply_markup,
                    disable_notification=True,
                    disable_web_page_preview=True,
                ),
                chat_id=chat_id,
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
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    parse_mode=parse_mode,
                    text=normalized_text,
                    reply_markup=reply_markup,
                    disable_notification=True,
                    disable_web_page_preview=True,
                ),
                chat_id=chat_id,
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

    async def _enqueue_text_edit(
        self,
        *,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
        parse_mode: Optional[str],
        context: str,
        force: bool = False,
        disable_web_page_preview: bool = False,
    ) -> Optional[MessageId]:
        return await self._update_queue.enqueue_text_edit(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            context=context,
            force=force,
            disable_web_page_preview=disable_web_page_preview,
        )

    def remember_text_payload(
        self,
        *,
        chat_id: ChatId,
        message_id: MessageId,
        text: Optional[str],
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
        parse_mode: Optional[str],
    ) -> None:
        if not message_id:
            return
        self._update_queue.record_payload(
            chat_id,
            message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

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
            await self._rate_limiter.send(_send, chat_id=chat_id)
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
            return await self._rate_limiter.send(
                lambda: self._bot.send_dice(
                    reply_to_message_id=message_id,
                    chat_id=chat_id,
                    disable_notification=True,
                    emoji=emoji,
                ),
                chat_id=chat_id,
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
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    reply_to_message_id=message_id,
                    chat_id=chat_id,
                    parse_mode=ParseMode.MARKDOWN,
                    text=normalized_text,
                    disable_notification=True,
                ),
                chat_id=chat_id,
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
    ) -> Optional[MessageId]:
        """Edit a message's text using the rate limiter."""
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
        return await self._enqueue_text_edit(
            chat_id=chat_id,
            message_id=message_id,
            text=normalized_text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            context="edit_message_text",
        )

    async def delete_message(
        self, chat_id: ChatId, message_id: MessageId
    ) -> None:
        """Delete a message with rate limiting and basic error handling."""
        try:
            await self._rate_limiter.send(
                lambda: self._bot.delete_message(
                    chat_id=chat_id, message_id=message_id
                ),
                chat_id=chat_id,
            )
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
            await self._rate_limiter.send(
                lambda: self._bot.send_photo(
                    chat_id=chat_id,
                    photo=bio,
                    disable_notification=disable_notification,
                ),
                chat_id=chat_id,
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
            im_cards = self._desk_generator.generate_desk(cards)
            bio = BytesIO()
            bio.name = "desk.png"
            im_cards.save(bio, "PNG")
            bio.seek(0)
            message = await self._rate_limiter.send(
                lambda: self._bot.send_photo(
                    chat_id=chat_id,
                    photo=bio,
                    caption=normalized_caption,
                    parse_mode=parse_mode,
                    disable_notification=disable_notification,
                    reply_markup=reply_markup,
                ),
                chat_id=chat_id,
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
            im_cards = self._desk_generator.generate_desk(cards)
            bio = BytesIO()
            bio.name = "desk.png"
            im_cards.save(bio, "PNG")
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
            await self._rate_limiter.send(
                lambda: self._bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    media=media,
                    reply_markup=reply_markup,
                ),
                chat_id=chat_id,
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
        hand_text = " ".join(str(card) for card in cards)
        table_values = list(table_cards or [])
        table_text = " ".join(str(card) for card in table_values) if table_values else "❔"
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
                edited_id = await self._enqueue_text_edit(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=normalized_hidden_text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    context="send_cards_hidden_edit",
                    disable_web_page_preview=True,
                )
                if edited_id:
                    return edited_id

            try:
                async def _send_keyboard() -> Message:
                    reply_kwargs = {}
                    if reply_to_ready_message and ready_message_id:
                        reply_kwargs["reply_to_message_id"] = ready_message_id
                    return await self._bot.send_message(
                        chat_id=chat_id,
                        text=normalized_hidden_text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=markup,
                        disable_notification=True,
                        **reply_kwargs,
                    )

                message = await self._rate_limiter.send(
                    _send_keyboard, chat_id=chat_id
                )

                message_id = getattr(message, "message_id", None) if message else None
                if message_id:
                    return message_id
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
            f"🃏 دست: {hand_text}\n"
            f"🃎 میز: {table_text}"
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
            async def _send() -> Message:
                reply_kwargs = {}
                if reply_to_ready_message and ready_message_id and not message_id:
                    reply_kwargs["reply_to_message_id"] = ready_message_id
                return await self._bot.send_message(
                    chat_id=chat_id,
                    text=normalized_message_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                    disable_notification=True,
                    **reply_kwargs,
                )

            message = await self._rate_limiter.send(_send, chat_id=chat_id)
            new_message_id: Optional[MessageId] = getattr(message, "message_id", None)

            if message_id and new_message_id and new_message_id != message_id:
                try:
                    await self._rate_limiter.send(
                        lambda: self._bot.delete_message(
                            chat_id=chat_id,
                            message_id=message_id,
                        ),
                        chat_id=chat_id,
                    )
                except BadRequest:
                    # پیام ممکن است قبلاً حذف شده باشد؛ صرفاً ادامه می‌دهیم.
                    pass
                except Exception as e:
                    logger.error(
                        "Error deleting previous cards message",
                        extra={
                            "error_type": type(e).__name__,
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

    async def send_turn_actions(
        self,
        chat_id: ChatId,
        game: Game,
        player: Player,
        money: Money,
        message_id: Optional[MessageId] = None,
        recent_actions: Optional[List[str]] = None,
    ) -> Optional[MessageId]:
        """ارسال یا ویرایش پیام نوبت بازیکن با نمایش اکشن‌های اخیر."""

        # نمایش کارت‌های میز
        if not game.cards_table:
            cards_table = "🚫 کارتی روی میز نیست"
        else:
            cards_table = " ".join(game.cards_table)

        # محاسبه CALL یا CHECK
        call_amount = game.max_round_rate - player.round_rate
        call_check_action = self.define_check_call_action(game, player)
        if call_check_action == PlayerAction.CALL:
            call_check_text = f"{call_check_action.value} ({call_amount}$)"
        else:
            call_check_text = call_check_action.value

        # متن پیام با Markdown
        text = (
            f"🎯 **نوبت بازی {player.mention_markdown} (صندلی {player.seat_index+1})**\n\n"
            f"🃏 **کارت‌های روی میز:** {cards_table}\n"
            f"💰 **پات فعلی:** `{game.pot}$`\n"
            f"💵 **موجودی شما:** `{money}$`\n"
            f"🎲 **بِت فعلی شما:** `{player.round_rate}$`\n"
            f"📈 **حداکثر شرط این دور:** `{game.max_round_rate}$`\n\n"
            f"⬇️ حرکت خود را انتخاب کنید:"
        )
        if recent_actions:
            text += "\n\n🎬 **اکشن‌های اخیر:**\n" + "\n".join(recent_actions)

        # کیبورد اینلاین
        markup = self._get_turns_markup(call_check_text, call_check_action)

        context = self._build_context(
            "send_turn_actions", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping turn actions message due to invalid text",
                extra={"context": context},
            )
            return None

        if message_id:
            edited_id = await self.edit_turn_actions(
                chat_id, message_id, normalized_text, markup
            )
            if edited_id:
                return edited_id
            # پیام اصلی دیگر قابل ویرایش نیست، پیام جدید ارسال می‌کنیم

        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text=normalized_text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=False,  # player gets notification
                ),
                chat_id=chat_id,
            )
            if isinstance(message, Message):
                new_message_id = message.message_id
                if message_id:
                    try:
                        await self._rate_limiter.send(
                            lambda: self._bot.delete_message(
                                chat_id=chat_id,
                                message_id=message_id,
                            ),
                            chat_id=chat_id,
                        )
                    except BadRequest:
                        pass
                    except Exception as e:
                        logger.debug(
                            "Failed to delete stale turn message",
                            extra={
                                "error_type": type(e).__name__,
                                "chat_id": chat_id,
                                "message_id": message_id,
                            },
                        )
                return new_message_id
        except Exception as e:
            logger.error(
                "Error sending turn actions",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "request_params": {"player": player.user_id},
                },
            )
        return None

    async def edit_turn_actions(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> Optional[MessageId]:
        """ویرایش پیام موجود با متن و کیبورد جدید."""
        context = self._build_context(
            "edit_turn_actions", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping edit_turn_actions due to invalid text",
                extra={"context": context},
            )
            return None
        return await self._enqueue_text_edit(
            chat_id=chat_id,
            message_id=message_id,
            text=normalized_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            context="edit_turn_actions",
            disable_web_page_preview=True,
        )

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
            result = await self._rate_limiter.send(
                lambda: self._bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                ),
                chat_id=chat_id,
            )
            if isinstance(result, Message):
                return True
            if result is True or result is None:
                return True
            return bool(result)
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

    @staticmethod
    def _get_turns_markup(check_call_text: str, check_call_action: PlayerAction) -> InlineKeyboardMarkup:
        keyboard = [[
            InlineKeyboardButton(text=PlayerAction.FOLD.value, callback_data=PlayerAction.FOLD.value),
            InlineKeyboardButton(text=PlayerAction.ALL_IN.value, callback_data=PlayerAction.ALL_IN.value),
            InlineKeyboardButton(text=check_call_text, callback_data=check_call_action.value),
        ], [
            InlineKeyboardButton(text=str(PlayerAction.SMALL.value), callback_data=str(PlayerAction.SMALL.value)),
            InlineKeyboardButton(text=str(PlayerAction.NORMAL.value), callback_data=str(PlayerAction.NORMAL.value)),
            InlineKeyboardButton(text=str(PlayerAction.BIG.value), callback_data=str(PlayerAction.BIG.value)),
        ]]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)


    async def remove_markup(self, chat_id: ChatId, message_id: MessageId) -> None:
        """حذف دکمه‌های اینلاین از یک پیام و فیلتر کردن ارورهای رایج."""
        if not message_id:
            return
        try:
            await self._rate_limiter.send(
                lambda: self._bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                ),
                chat_id=chat_id,
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
                    final_message += f"    کارت‌ها: {' '.join(map(str, hand_cards))}\n"
                
                final_message += "\n" # یک خط فاصله برای جداسازی پات‌ها

        final_message += "⎯" * 20 + "\n"
        final_message += f"🃏 *کارت‌های روی میز:* {' '.join(map(str, game.cards_table)) if game.cards_table else '🚫'}\n\n"

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
                card_display = ' '.join(map(str, p.cards)) if p.cards else 'کارت‌ها نمایش داده نشد'
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
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text=normalized_message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                    disable_web_page_preview=True,
                    reply_markup=reply_keyboard,
                ),
                chat_id=chat_id,
            )
        except Exception as e:
            logger.error(
                "Error sending new hand ready message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                },
            )
