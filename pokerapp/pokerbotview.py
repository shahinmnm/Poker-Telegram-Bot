#!/usr/bin/env python3

from telegram import (
    Message,
    ParseMode,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Bot,
    InputMediaPhoto,
)
from io import BytesIO

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
    ) -> None:
        self._bot.send_message(
            chat_id=chat_id,
            parse_mode=ParseMode.MARKDOWN,
            text=text,
            reply_markup=reply_markup,
            disable_notification=True,
            disable_web_page_preview=True,
        )

    def send_photo(self, chat_id: ChatId) -> None:
        # TODO: photo to args.
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
        im_cards = self._desk_generator.generate_desk(cards)
        bio = BytesIO()
        bio.name = 'desk.png'
        im_cards.save(bio, 'PNG')
        bio.seek(0)
        
        # در کد اصلی از send_media_group استفاده شده که یک لیست برمی‌گرداند
        # ما فقط به message_id اولین آیتم نیاز داریم
        message = self._bot.send_photo(
            chat_id=chat_id,
            photo=bio,
            caption=caption,
            disable_notification=disable_notification,
        )


    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
        # ساخت یک کیبورد جذاب‌تر
        # هر کارت را به همراه یک ایموجی نمایش می‌دهیم
        card_buttons = [f"🃏 {card}" for card in cards]
        keyboard = [
            card_buttons,
            ["👁️ نمایش میز"],  # دکمه‌ای برای نمایش مجدد وضعیت میز
            ["🙈 پنهان کردن"],   # دکمه‌ای برای بستن کیبورد
        ]
        return ReplyKeyboardMarkup(
            keyboard=keyboard,
            selective=True,       # این کیبورد فقط برای بازیکن مورد نظر فعال می‌شود
            resize_keyboard=True, # اندازه دکمه‌ها برای نمایش بهتر تنظیم می‌شود
            one_time_keyboard=True # بعد از یک بار کلیک، کیبورد بسته می‌شود (اختیاری)
        )

    @ staticmethod
    def _get_turns_markup(
        check_call_action: PlayerAction
    ) -> InlineKeyboardMarkup:
        keyboard = [[
            InlineKeyboardButton(
                text=PlayerAction.FOLD.value,
                callback_data=PlayerAction.FOLD.value,
            ),
            InlineKeyboardButton(
                text=PlayerAction.ALL_IN.value,
                callback_data=PlayerAction.ALL_IN.value,
            ),
            InlineKeyboardButton(
                text=check_call_action.value,
                callback_data=check_call_action.value,
            ),
        ], [
            InlineKeyboardButton(
                text=str(PlayerAction.SMALL.value) + "$",
                callback_data=str(PlayerAction.SMALL.value)
            ),
            InlineKeyboardButton(
                text=str(PlayerAction.NORMAL.value) + "$",
                callback_data=str(PlayerAction.NORMAL.value)
            ),
            InlineKeyboardButton(
                text=str(PlayerAction.BIG.value) + "$",
                callback_data=str(PlayerAction.BIG.value)
            ),
        ]]

        return InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )

    def send_cards(
            self,
            group_chat_id: ChatId, # حالا ID گروه را می‌گیریم
            cards: Cards,
            mention_markdown: Mention,
            player_id: int, # به ID بازیکن برای selective نیاز داریم
    ) -> None:
        """
        Sends a message to the group, mentioning the player and showing them a custom keyboard with their cards.
        """
        markup = PokerBotViewer._get_cards_markup(cards)
        text = f"کارت‌های شما نمایش داده شد {mention_markdown}!\n" \
               f"کارت‌هایتان در منوی پایین صفحه (کیبورد) قابل مشاهده است. این منو فقط برای شما نمایش داده می‌شود."

        # ارسال پیام در گروه، اما کیبورد فقط برای بازیکن هدف نمایش داده می‌شود
        self._bot.send_message(
            chat_id=group_chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
            # selective=True در ReplyKeyboardMarkup مدیریت می‌شود
        )
    def hide_cards_keyboard(self, chat_id: ChatId, player_mention: Mention) -> None:
        """
        Removes the custom reply keyboard for a player.
        """
        self._bot.send_message(
            chat_id=chat_id,
            text=f"منوی کارت‌ها برای {player_mention} بسته شد.",
            reply_markup=ReplyKeyboardRemove(selective=True),
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True
        )

    @ staticmethod
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
    ) -> None:
        if len(game.cards_table) == 0:
            cards_table = "🚫 کارتی نیست."
        else:
            cards_table = " ".join(game.cards_table)
        text = (
            "🔄 نوبت  {}\n" +
            "{}\n" +
            "پول: *{}$*\n" +
            "📊 حداکثر نرخ دور: *{}$*"
        ).format(
            player.mention_markdown,
            cards_table,
            money,
            game.max_round_rate,
        )
        check_call_action = PokerBotViewer.define_check_call_action(
            game, player
        )
        markup = PokerBotViewer._get_turns_markup(check_call_action)
        self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )

    def remove_markup(
        self,
        chat_id: ChatId,
        message_id: MessageId,
    ) -> None:
        self._bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
        )

    def remove_message(
        self,
        chat_id: ChatId,
        message_id: MessageId,
    ) -> None:
        self._bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
