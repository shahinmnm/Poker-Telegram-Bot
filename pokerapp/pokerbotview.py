#!/usr/bin/env python3

from telegram import (
    Message,
    ParseMode,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,  # <<<< جدید
    Bot,
    InputMediaPhoto,
)
from io import BytesIO
from typing import List, Optional # <<<< Optional را اضافه کنید
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
    ) -> Optional[MessageId]: # <<<< نوع بازگشتی را به Optional[MessageId] تغییر دهید
        """Sends a message and returns its ID, or None if not applicable."""
        message = self._bot.send_message(
            chat_id=chat_id,
            parse_mode=ParseMode.MARKDOWN,
            text=text,
            reply_markup=reply_markup,
            disable_notification=True,
            disable_web_page_preview=True,
        )
        # <<<< شروع بلوک اصلاح شده >>>>
        # بررسی می‌کنیم که آیا message یک شی Message معتبر است یا خیر
        if isinstance(message, Message):
            return message.message_id
        # در غیر این صورت (مثلا وقتی ReplyKeyboardRemove استفاده شده)، None برمی‌گردانیم
        return None
        # <<<< پایان بلوک اصلاح شده >>>>
    
    def send_message(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
    ) -> None:
        self.send_message_return_id(chat_id, text, reply_markup)

    def send_photo(self, chat_id: ChatId) -> None:
        self._bot.send_photo(
            chat_id=chat_id,
            photo=open("./assets/poker_hand.jpg", 'rb'),
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )

    def send_dice_reply(
        self, chat_id: ChatId, message_id: MessageId, emoji='🎲'
    ) -> Message:
        return self._bot.send_dice(
            reply_to_message_id=message_id,
            chat_id=chat_id,
            disable_notification=True,
            emoji=emoji,
        )

    def send_message_reply(
        self, chat_id: ChatId, message_id: MessageId, text: str
    ) -> None:
        self._bot.send_message(
            reply_to_message_id=message_id,
            chat_id=chat_id,
            parse_mode=ParseMode.MARKDOWN,
            text=text,
            disable_notification=True,
        )
    
    def send_desk_cards_img(
        self,
        chat_id: ChatId,
        cards: Cards,
        caption: str = "",
        disable_notification: bool = True,
    ) -> MessageId:
        """Sends desk cards image and returns message id."""
        im_cards = self._desk_generator.generate_desk(cards)
        bio = BytesIO()
        bio.name = 'desk.png'
        im_cards.save(bio, 'PNG')
        bio.seek(0)
        message = self._bot.send_photo(
            chat_id=chat_id,
            photo=bio,
            caption=caption,
            disable_notification=disable_notification,
        )
        return message.message_id

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
        self._bot.send_message(
            chat_id=chat_id,
            text=f"کارت‌های {player_mention} پنهان شد. برای مشاهده دوباره از دکمه‌ها استفاده کن.",
            reply_markup=reopen_keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )

    # <<<< شروع متد جدید: پاکسازی پیام ها >>>>
    def remove_game_messages(self, chat_id: ChatId, message_ids: List[MessageId]) -> None:
        """Deletes a list of messages from the chat."""
        for msg_id in message_ids:
            try:
                self._bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                # پیام ممکن است قبلاً حذف شده باشد یا خیلی قدیمی باشد
                pass
    # <<<< پایان متد جدید >>>>

    @staticmethod
    def _get_turns_markup(check_call_action: PlayerAction) -> InlineKeyboardMarkup:
        keyboard = [[
            InlineKeyboardButton(text=PlayerAction.FOLD.value, callback_data=PlayerAction.FOLD.value),
            InlineKeyboardButton(text=PlayerAction.ALL_IN.value, callback_data=PlayerAction.ALL_IN.value),
            InlineKeyboardButton(text=check_call_action.value, callback_data=check_call_action.value),
        ], [
            InlineKeyboardButton(text=str(PlayerAction.SMALL.value) + "$", callback_data=str(PlayerAction.SMALL.value)),
            InlineKeyboardButton(text=str(PlayerAction.NORMAL.value) + "$", callback_data=str(PlayerAction.NORMAL.value)),
            InlineKeyboardButton(text=str(PlayerAction.BIG.value) + "$", callback_data=str(PlayerAction.BIG.value)),
        ]]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: str,
    ) -> None: # <<<< تغییر نوع بازگشتی از MessageId به None
        markup = PokerBotViewer._get_cards_markup(cards)
        self._bot.send_message( # <<<< حذف 'message ='
            chat_id=chat_id,
            text="Showing cards to " + mention_markdown,
            reply_markup=markup,
            reply_to_message_id=ready_message_id,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )
    @staticmethod
    def define_check_call_action(game: Game, player: Player) -> PlayerAction:
        if player.round_rate == game.max_round_rate:
            return PlayerAction.CHECK
        return PlayerAction.CALL

    def send_turn_actions(
        self, chat_id: ChatId, game: Game, player: Player, money: Money
    ) -> MessageId:
        """Sends the turn actions to the player and returns the message ID."""
        if not game.cards_table:
            cards_table = "🚫 کارتی روی میز نیست"
        else:
            cards_table = " ".join(game.cards_table)
        text = (
            "🔄 نوبت {}\n" +
            "{}\n" +
            "پول: *{}$*\n" +
            "📊 حداکثر نرخ دور: *{}$*"
        ).format(player.mention_markdown, cards_table, money, game.max_round_rate)
        
        check_call_action = PokerBotViewer.define_check_call_action(game, player)
        markup = PokerBotViewer._get_turns_markup(check_call_action)
        message = self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )
        return message.message_id

    def remove_markup(self, chat_id: ChatId, message_id: MessageId) -> None:
        self._bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)

    def remove_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        self._bot.delete_message(chat_id=chat_id, message_id=message_id)
