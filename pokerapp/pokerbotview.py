# pokerbotview.py

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
from io import BytesIO
from typing import List, Tuple, Dict

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

    def send_message(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
    ) -> MessageId:
        message = self._bot.send_message(
            chat_id=chat_id,
            parse_mode=ParseMode.MARKDOWN,
            text=text,
            reply_markup=reply_markup,
            disable_notification=True,
            disable_web_page_preview=True,
        )
        return message.message_id

    def send_photo(self, chat_id: ChatId) -> None:
        self._bot.send_photo(
            chat_id=chat_id,
            photo=open("./assets/poker_hand.jpg", 'rb'),
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )

    def send_dice_reply(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        emoji='🎲',
    ) -> Message:
        return self._bot.send_dice(
            reply_to_message_id=message_id,
            chat_id=chat_id,
            disable_notification=True,
            emoji=emoji,
        )

    def send_message_reply(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
    ) -> MessageId:
        message = self._bot.send_message(
            reply_to_message_id=message_id,
            chat_id=chat_id,
            parse_mode=ParseMode.MARKDOWN,
            text=text,
            disable_notification=True,
        )
        return message.message_id

    def send_desk_cards_img(
        self,
        chat_id: ChatId,
        cards: Cards,
        caption: str = "",
        disable_notification: bool = True,
    ) -> MessageId:
        im_cards = self._desk_generator.generate_desk(cards)
        bio = BytesIO()
        bio.name = 'desk.png'
        im_cards.save(bio, 'PNG')
        bio.seek(0)
        message = self._bot.send_media_group(
            chat_id=chat_id,
            media=[
                InputMediaPhoto(
                    media=bio,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                ),
            ],
            disable_notification=disable_notification,
        )[0]
        return message.message_id

    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
        # دکمه جدید "نمایش میز" به کیبورد اضافه شد
        keyboard = [
            cards,  # ردیف اول: کارت‌های بازیکن
            ["👁️ نمایش میز"] # ردیف دوم: دکمه نمایش میز
        ]
        return ReplyKeyboardMarkup(
            keyboard=keyboard,
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False # برای اینکه کیبورد بعد از یک بار کلیک پنهان نشود
        )

    @staticmethod
    def _get_turns_markup(
        check_call_action: PlayerAction
    ) -> InlineKeyboardMarkup:
        keyboard = [[
            InlineKeyboardButton(
                text=f"棄 Fold {PlayerAction.FOLD.value}",
                callback_data=PlayerAction.FOLD.value,
            ),
            InlineKeyboardButton(
                text=f"🤑 All-in {PlayerAction.ALL_IN.value}",
                callback_data=PlayerAction.ALL_IN.value,
            ),
            InlineKeyboardButton(
                text=f"{'🤝 Check' if check_call_action == PlayerAction.CHECK else '📞 Call'} {check_call_action.value}",
                callback_data=check_call_action.value,
            ),
        ], [
            InlineKeyboardButton(
                text=f"🔼 {PlayerAction.SMALL.value}$",
                callback_data=str(PlayerAction.SMALL.value)
            ),
            InlineKeyboardButton(
                text=f"🔼🔼 {PlayerAction.NORMAL.value}$",
                callback_data=str(PlayerAction.NORMAL.value)
            ),
            InlineKeyboardButton(
                text=f"🔼🔼🔼 {PlayerAction.BIG.value}$",
                callback_data=str(PlayerAction.BIG.value)
            ),
        ]]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: str,
    ) -> None:
        markup = PokerBotViewer._get_cards_markup(cards)
        self._bot.send_message(
            chat_id=chat_id,
            text=f"🃏 نمایش کارت‌ها به {mention_markdown}",
            reply_markup=markup,
            reply_to_message_id=ready_message_id,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )

    @staticmethod
    def define_check_call_action(
        game: Game,
        player: Player,
    ) -> PlayerAction:
        if player.round_rate == game.max_round_rate:
            return PlayerAction.CHECK
        return PlayerAction.CALL

    def send_turn_actions(
            self,
            chat_id: ChatId,
            game: Game,
            player: Player,
            money: Money,
    ) -> MessageId:
        if len(game.cards_table) == 0:
            cards_table_str = "🚫 هنوز کارتی رو نشده"
        else:
            cards_table_str = " ".join(game.cards_table)
        
        text = (
            f"🔄 نوبت {player.mention_markdown}\n\n"
            f"🎲 کارت‌های روی میز: {cards_table_str}\n"
            f"💰 پات فعلی: *{game.pot}$*\n\n"
            f"💵 موجودی شما: *{money}$*\n"
            f"💸 شرط شما در این دور: *{player.round_rate}$*\n"
            f"📈 حداکثر شرط این دور: *{game.max_round_rate}$*"
        )
        
        check_call_action = self.define_check_call_action(game, player)
        markup = self._get_turns_markup(check_call_action)
        
        message = self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=False,
        )
        return message.message_id

    def remove_markup(
        self,
        chat_id: ChatId,
        message_id: MessageId,
    ) -> None:
        try:
            self._bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception as e:
            print(f"Could not remove markup from message {message_id}: {e}")

    def remove_message(
        self,
        chat_id: ChatId,
        message_id: MessageId,
    ) -> None:
        try:
            self._bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception as e:
            print(f"Could not delete message {message_id}: {e}")
