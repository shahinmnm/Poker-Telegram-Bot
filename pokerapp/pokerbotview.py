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
from typing import Optional, Callable, Awaitable, Dict, Any, List
import asyncio
import logging
import json
import time
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
        delay: float = 0.1,
        max_retries: int = 3,
        error_delay: float = 0.1,
        notify_admin: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        max_per_minute: int = 20,
    ):
        self._delay = delay
        self._lock = asyncio.Lock()
        self._max_retries = max_retries
        self._error_delay = error_delay
        self._notify_admin = notify_admin
        self._max_tokens = max_per_minute
        self._refill_rate = max_per_minute / 60.0  # tokens per second
        self._buckets: Dict[ChatId, Dict[str, float]] = {}

    async def _wait_for_token(self, chat_id: ChatId) -> Dict[str, float]:
        bucket = self._buckets.setdefault(
            chat_id, {"tokens": self._max_tokens, "ts": time.monotonic()}
        )
        while True:
            now = time.monotonic()
            elapsed = now - bucket["ts"]
            bucket["tokens"] = min(
                self._max_tokens, bucket["tokens"] + elapsed * self._refill_rate
            )
            bucket["ts"] = now
            if bucket["tokens"] >= 1:
                return bucket
            wait_time = (1 - bucket["tokens"]) / self._refill_rate
            await asyncio.sleep(wait_time)

    async def send(
        self, func: Callable[..., Awaitable[Any]], *args, chat_id: Optional[ChatId] = None, **kwargs
    ):
        """Execute ``func`` with args, respecting per-chat limits and retrying."""
        async with self._lock:
            bucket = None
            if chat_id is not None:
                bucket = await self._wait_for_token(chat_id)
            attempts = 0
            last_error: Optional[TelegramError] = None
            while True:
                try:
                    result = await func(*args, **kwargs)
                    remaining = None
                    if bucket is not None:
                        bucket["tokens"] -= 1
                        remaining = bucket["tokens"]
                    delay = self._delay
                    if remaining is not None and remaining < 5:
                        delay += (5 - remaining) * 0.5
                    await asyncio.sleep(delay)
                    return result
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except TelegramError as e:
                    if attempts >= self._max_retries:
                        last_error = e
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
                        await self._notify_admin(
                            {
                                "event": "rate_limiter_error",
                                "error": str(e),
                                "error_type": type(e).__name__,
                            }
                        )
                    last_error = e if isinstance(e, TelegramError) else None
                    break
            if last_error:
                if self._notify_admin:
                    await self._notify_admin(
                        {
                            "event": "rate_limiter_failed",
                            "request_params": {"args": str(args), "kwargs": str(kwargs)},
                        }
                    )
                logger.warning(
                    "RateLimitedSender.send failed after retries",
                    extra={"request_params": {"args": args, "kwargs": kwargs}},
                )
                raise last_error
            if self._notify_admin:
                await self._notify_admin(
                    {
                        "event": "rate_limiter_failed",
                        "request_params": {"args": str(args), "kwargs": str(kwargs)},
                    }
                )
            logger.warning(
                "RateLimitedSender.send failed after retries",
                extra={"request_params": {"args": args, "kwargs": kwargs}},
            )
            return None

class PokerBotViewer:
    def __init__(self, bot: Bot, admin_chat_id: Optional[int] = None):
        self._bot = bot
        self._desk_generator = DeskImageGenerator()
        self._admin_chat_id = admin_chat_id
        # 0.1s base delay to allow faster message delivery while avoiding limits
        self._rate_limiter = RateLimitedSender(
            delay=0.1, error_delay=0.1, notify_admin=self.notify_admin
        )

    async def notify_admin(self, log_data: Dict[str, Any]) -> None:
        if not self._admin_chat_id:
            return
        try:
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=self._admin_chat_id,
                    text=json.dumps(log_data, ensure_ascii=False),
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
        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    parse_mode=ParseMode.MARKDOWN,
                    text=text,
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
        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    parse_mode=parse_mode,
                    text=text,
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
        try:
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    reply_to_message_id=message_id,
                    chat_id=chat_id,
                    parse_mode=ParseMode.MARKDOWN,
                    text=text,
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
        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                ),
                chat_id=chat_id,
            )
            if isinstance(message, Message):
                return message.message_id
            return message_id
        except Exception as e:
            logger.error(
                "Error editing message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "request_params": {"text": text},
                },
            )
        return None

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
                    caption=caption,
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
            media = InputMediaPhoto(media=bio, caption=caption, parse_mode=parse_mode)
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
    @staticmethod
    def _get_table_markup(table_cards: Cards, stage: str) -> ReplyKeyboardMarkup:
        """Creates a keyboard displaying table cards and stage buttons."""
        cards_row = [str(card) for card in table_cards] if table_cards else ["❔"]
        stages = ["فلاپ", "ترن", "ریور", "👁️ نمایش میز"]
        stage_map = {"flop": "فلاپ", "turn": "ترن", "river": "ریور"}
        if stage in stage_map:
            stages = [
                f"✅ {stage_map[stage]}" if s == stage_map[stage] else s for s in stages
            ]
        return ReplyKeyboardMarkup(
            keyboard=[cards_row, stages],
            selective=False,
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    @staticmethod
    def _get_hand_and_board_markup(
        hand: Cards, table_cards: Cards, stage: str
    ) -> ReplyKeyboardMarkup:
        """Combine player's hand, table cards and stage buttons in one keyboard.

        این کیبورد در پیام خصوصی بازیکن استفاده می‌شود تا او هم‌زمان دست و کارت‌های
        میز را مشاهده کند. دکمهٔ پنهان‌سازی کارت‌ها حذف شده است تا فضا برای نمایش
        بهتر کارت‌ها فراهم شود.
        """

        hand_row = [str(c) for c in hand]
        table_row = [str(c) for c in table_cards] if table_cards else ["❔"]

        stages = ["فلاپ", "ترن", "ریور"]
        stage_map = {"flop": "فلاپ", "turn": "ترن", "river": "ریور"}
        stage_row = []
        for s in stages:
            label = f"🔁 {s}"
            if stage_map.get(stage) == s:
                label = f"✅ {s}"
            stage_row.append(label)

        return ReplyKeyboardMarkup(
            keyboard=[hand_row, table_row, stage_row],
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    async def show_reopen_keyboard(self, chat_id: ChatId) -> None:
        """Hides cards and sends a private keyboard to reopen them."""
        show_cards_button_text = "🃏 نمایش کارت‌ها"
        show_table_button_text = "👁️ نمایش میز"
        reopen_keyboard = ReplyKeyboardMarkup(
            keyboard=[[show_cards_button_text, show_table_button_text]],
            selective=False,
            resize_keyboard=True,
            one_time_keyboard=False
        )
        await self.send_message(
            chat_id=chat_id,
            text="کارت‌ها پنهان شد. برای مشاهده دوباره از دکمه‌ها استفاده کن.",
            reply_markup=reopen_keyboard,
        )

    async def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: str | None = None,
            table_cards: Cards | None = None,
            stage: str = "",
            message_id: MessageId | None = None,
            reply_to_ready_message: bool = True,
    ) -> Optional[MessageId]:
        markup = self._get_hand_and_board_markup(cards, table_cards or [], stage)
        hand_text = " ".join(str(card) for card in cards)
        table_values = list(table_cards or [])
        table_text = " ".join(str(card) for card in table_values) if table_values else "❔"
        message_text = (
            f"{mention_markdown}\n"
            f"🃏 دست: {hand_text}\n"
            f"🃎 میز: {table_text}"
        )
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

        try:
            async def _send() -> Message:
                reply_kwargs = {}
                if reply_to_ready_message and ready_message_id and not message_id:
                    reply_kwargs["reply_to_message_id"] = ready_message_id
                return await self._bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                    disable_notification=True,
                    **reply_kwargs,
                )

            message = await self._rate_limiter.send(_send, chat_id=chat_id)
            if isinstance(message, Message):
                return message.message_id
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

        if message_id:
            await self.edit_turn_actions(chat_id, message_id, text, markup)
            return message_id

        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=False,  # player gets notification
                ),
                chat_id=chat_id,
            )
            if isinstance(message, Message):
                return message.message_id
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
        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                ),
                chat_id=chat_id,
            )
            if isinstance(message, Message):
                return message.message_id
        except Exception as e:
            logger.error(
                "Error editing turn actions",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "request_params": {"message_id": message_id},
                },
            )
        return None

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
        try:
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                    disable_web_page_preview=True,
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
