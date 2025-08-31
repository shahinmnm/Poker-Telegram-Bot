#!/usr/bin/env python3

from telegram import (
    Message,
    ParseMode,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Bot,
    InputMediaPhoto,
)
from threading import Timer
from io import BytesIO
from typing import List, Optional
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
)

class PokerBotViewer:
    def __init__(self, bot: Bot):
        self._bot = bot
        self._desk_generator = DeskImageGenerator()

    def send_message_return_id(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
    ) -> Optional[MessageId]:
        """Sends a message and returns its ID, or None if not applicable."""
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                parse_mode=ParseMode.MARKDOWN,
                text=text,
                reply_markup=reply_markup,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                return message.message_id
        except Exception as e:
            print(f"Error sending message and returning ID: {e}")
        return None

    def send_message(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
    ) -> Optional[MessageId]:
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                parse_mode=ParseMode.MARKDOWN,
                text=text,
                reply_markup=reply_markup,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                return message.message_id
        except Exception as e:
            print(f"Error sending message: {e}")
        return None

    def send_photo(self, chat_id: ChatId) -> None:
        try:
            self._bot.send_photo(
                chat_id=chat_id,
                photo=open("./assets/poker_hand.jpg", 'rb'),
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
            )
        except Exception as e:
            print(f"Error sending photo: {e}")

    def send_dice_reply(
        self, chat_id: ChatId, message_id: MessageId, emoji='🎲'
    ) -> Optional[Message]:
        try:
            return self._bot.send_dice(
                reply_to_message_id=message_id,
                chat_id=chat_id,
                disable_notification=True,
                emoji=emoji,
            )
        except Exception as e:
            print(f"Error sending dice reply: {e}")
            return None

    def send_message_reply(
        self, chat_id: ChatId, message_id: MessageId, text: str
    ) -> None:
        try:
            self._bot.send_message(
                reply_to_message_id=message_id,
                chat_id=chat_id,
                parse_mode=ParseMode.MARKDOWN,
                text=text,
                disable_notification=True,
            )
        except Exception as e:
            print(f"Error sending message reply: {e}")

    def send_desk_cards_img(
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
            messages = self._bot.send_media_group(
                chat_id=chat_id,
                media=[
                    InputMediaPhoto(
                        media=bio,
                        caption=caption,
                    ),
                ],
                disable_notification=disable_notification,
            )
            if messages and isinstance(messages, list) and len(messages) > 0:
                return messages[0]
        except Exception as e:
            print(f"Error sending desk cards image: {e}")
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

    def show_reopen_keyboard(self, chat_id: ChatId, player_mention: Mention) -> None:
        """Hides cards and shows a keyboard with a 'Show Cards' button."""
        show_cards_button_text = "🃏 نمایش کارت‌ها"
        show_table_button_text = "👁️ نمایش میز"
        reopen_keyboard = ReplyKeyboardMarkup(
            keyboard=[[show_cards_button_text, show_table_button_text]],
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False
        )
        self.send_message(
            chat_id=chat_id,
            text=f"کارت‌های {player_mention} پنهان شد. برای مشاهده دوباره از دکمه‌ها استفاده کن.",
            reply_markup=reopen_keyboard,
        )

    def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: str,
    ) -> Optional[MessageId]:
        markup = self._get_cards_markup(cards)
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                text="کارت‌های شما " + mention_markdown,
                reply_markup=markup,
                reply_to_message_id=ready_message_id,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
            )
            if isinstance(message, Message):
                return message.message_id
        except Exception as e:
            print(f"Error sending cards: {e}")
        return None

    @staticmethod
    def define_check_call_action(game: Game, player: Player) -> PlayerAction:
        if player.round_rate >= game.max_round_rate:
            return PlayerAction.CHECK
        return PlayerAction.CALL

    def send_turn_actions(
            self,
            chat_id: ChatId,
            game: Game,
            player: Player,
            money: Money,
    ) -> Optional[MessageId]:
        """ارسال فوری پیام نوبت بازیکن با ظاهر زیباتر و بدون تأخیر صف."""
        # نمایش کارت‌های میز
        if not game.cards_table:
            cards_table = "🚫 کارتی روی میز نیست"
        else:
            cards_table = " ".join(game.cards_table)
    
        # محاسبه مقدار CALL یا CHECK
        call_amount = game.max_round_rate - player.round_rate
        call_check_action = self.define_check_call_action(game, player)
        if call_check_action == PlayerAction.CALL:
            call_check_text = f"{call_check_action.value} ({call_amount}$)"
        else:
            call_check_text = call_check_action.value
    
        # متن پیام با فرمت Markdown
        text = (
            f"🎯 **نوبت بازی {player.mention_markdown}**\n\n"
            f"🃏 **کارت‌های روی میز:** {cards_table}\n"
            f"💰 **پات فعلی:** `{game.pot}$`\n"
            f"💵 **موجودی شما:** `{money}$`\n"
            f"🎲 **بِت فعلی شما در این دور:** `{player.round_rate}$`\n"
            f"📈 **حداکثر شرط در این دور:** `{game.max_round_rate}$`\n\n"
            f"لطفاً حرکت خود را انتخاب کنید:"
        )
    
        # ساخت کیبورد
        markup = self._get_turns_markup(call_check_text, call_check_action)
    
        try:
            # ارسال فوری با bypass صف
            message = self._bot.send_message_sync(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=False,  # اطلاع‌رسانی به بازیکن
            )
            if isinstance(message, Message):
                return message.message_id
        except Exception as e:
            print(f"Error sending turn actions: {e}")
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


    from telegram.error import BadRequest, Unauthorized  # اضافه کردن بالای فایل
    
    def remove_markup(self, chat_id: ChatId, message_id: MessageId) -> None:
        """حذف دکمه‌های اینلاین از یک پیام و فیلتر کردن ارورهای رایج."""
        if not message_id:
            return
        try:
            self._bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message is not modified" in err:
                print(f"[INFO] Markup already removed or message not found (ID={message_id}).")
            else:
                print(f"[WARNING] BadRequest removing markup (ID={message_id}): {e}")
        except Unauthorized as e:
            print(f"[INFO] Cannot edit markup, bot unauthorized in chat {chat_id}: {e}")
        except Exception as e:
            print(f"[ERROR] Unexpected error removing markup (ID={message_id}): {e}")
    
    def remove_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        """حذف پیام از چت و فیلتر کردن ارورهای بی‌خطر."""
        if not message_id:
            return
        try:
            self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            err = str(e).lower()
            if "message to delete not found" in err or "message can't be deleted" in err:
                print(f"[INFO] Message already deleted or too old (ID={message_id}).")
            else:
                print(f"[WARNING] BadRequest deleting message (ID={message_id}): {e}")
        except Unauthorized as e:
            print(f"[INFO] Cannot delete message, bot unauthorized in chat {chat_id}: {e}")
        except Exception as e:
            print(f"[ERROR] Unexpected error deleting message (ID={message_id}): {e}")
            
    def remove_message_delayed(self, chat_id: ChatId, message_id: MessageId, delay: float = 3.0) -> None:
        """حذف پیام با تأخیر برحسب ثانیه."""
        if not message_id:
            return

        def _remove():
            try:
                self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                print(f"Could not delete message {message_id} in chat {chat_id}: {e}")

        Timer(delay, _remove).start()
