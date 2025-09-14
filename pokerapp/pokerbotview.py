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
from typing import Optional, Callable, Awaitable, Dict, Any
import asyncio
import logging
import json
from pokerapp.winnerdetermination import HAND_NAMES_TRANSLATIONS
from pokerapp.desk import DeskImageGenerator
from pokerapp.cards import Cards
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
    """Serializes Telegram requests and retries on rate limits."""

    def __init__(
        self,
        delay: float = 3.0,
        max_retries: int = 3,
        error_delay: float = 1.0,
        notify_admin: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ):
        self._delay = delay
        self._lock = asyncio.Lock()
        self._max_retries = max_retries
        self._error_delay = error_delay
        self._notify_admin = notify_admin

    async def send(self, func: Callable[..., Awaitable[Any]], *args, **kwargs):
        """Execute ``func`` with args, retrying on ``RetryAfter``."""
        async with self._lock:
            for _ in range(self._max_retries):
                try:
                    result = await func(*args, **kwargs)
                    await asyncio.sleep(self._delay)
                    return result
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except TelegramError:
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
                    break
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
        # 3s base delay to avoid hitting 20 messages/min limit in group chats
        self._rate_limiter = RateLimitedSender(delay=3.0, notify_admin=self.notify_admin)

    async def notify_admin(self, log_data: Dict[str, Any]) -> None:
        if not self._admin_chat_id:
            return
        try:
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=self._admin_chat_id,
                    text=json.dumps(log_data, ensure_ascii=False),
                )
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
                )
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
                )
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
            await self._rate_limiter.send(_send)
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
                )
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
                )
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
                )
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

    async def send_desk_cards_img(
        self,
        chat_id: ChatId,
        cards: Cards,
        caption: str = "",
        disable_notification: bool = True,
    ) -> Optional[Message]:
        """Sends desk cards image and returns the message object."""
        try:
            im_cards = self._desk_generator.generate_desk(cards)
            bio = BytesIO()
            bio.name = 'desk.png'
            im_cards.save(bio, 'PNG')
            bio.seek(0)
            messages = await self._rate_limiter.send(
                lambda: self._bot.send_media_group(
                    chat_id=chat_id,
                    media=[
                        InputMediaPhoto(
                            media=bio,
                            caption=caption,
                        ),
                    ],
                    disable_notification=disable_notification,
                )
            )
            if messages and isinstance(messages, list) and len(messages) > 0:
                return messages[0]
        except Exception as e:
            logger.error(
                "Error sending desk cards image",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                },
            )
        return None

    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
        """Creates the keyboard for showing player cards and actions."""
        hide_cards_button_text = "🙈 پنهان کردن کارت‌ها"
        show_table_button_text = "👁️ نمایش میز"
        return ReplyKeyboardMarkup(
            keyboard=[
                cards,
                [hide_cards_button_text, show_table_button_text]
            ],
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    async def show_reopen_keyboard(self, chat_id: ChatId, player_mention: Mention) -> None:
        """Hides cards and shows a keyboard with a 'Show Cards' button."""
        show_cards_button_text = "🃏 نمایش کارت‌ها"
        show_table_button_text = "👁️ نمایش میز"
        reopen_keyboard = ReplyKeyboardMarkup(
            keyboard=[[show_cards_button_text, show_table_button_text]],
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False
        )
        await self.send_message(
            chat_id=chat_id,
            text=f"کارت‌های {player_mention} پنهان شد. برای مشاهده دوباره از دکمه‌ها استفاده کن.",
            reply_markup=reopen_keyboard,
        )

    async def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: str,
    ) -> Optional[MessageId]:
        markup = self._get_cards_markup(cards)
        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text="کارت‌های شما " + mention_markdown,
                    reply_markup=markup,
                    reply_to_message_id=ready_message_id,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                )
            )
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
    ) -> Optional[MessageId]:
        """ارسال پیام نوبت بازیکن با فرمت فارسی/ایموجی و استفاده از delay جدید 0.5s."""
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

        # کیبورد اینلاین
        markup = self._get_turns_markup(call_check_text, call_check_action)

        try:
            message = await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=False,  # player gets notification
                )
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
                )
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

        await self.send_message(chat_id=chat_id, text=final_message, parse_mode="Markdown")

    async def send_new_hand_ready_message(self, chat_id: ChatId) -> None:
        """پیام آمادگی برای دست جدید را ارسال می‌کند."""
        message = (
            "♻️ دست به پایان رسید. بازیکنان باقی‌مانده برای دست بعد حفظ شدند.\n"
            "برای شروع دست جدید، /start را بزنید یا بازیکنان جدید می‌توانند با /ready اعلام آمادگی کنند."
        )
        try:
            await self._rate_limiter.send(
                lambda: self._bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                    disable_web_page_preview=True,
                )
            )
        except Exception as e:
            logger.error(
                "Error sending new hand ready message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                },
            )
