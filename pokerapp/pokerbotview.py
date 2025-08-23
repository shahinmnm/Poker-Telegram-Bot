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
        # TODO: آیا می‌خواهیم مسیر عکس را به‌عنوان پارامتر دریافت کنیم؟
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
        return self._bot.send_media_group(
            chat_id=chat_id,
            media=[
                InputMediaPhoto(
                    media=bio,
                    caption=caption,
                ),
            ],
            disable_notification=disable_notification,
        )[0]
    @staticmethod
    def _card_display(card):
        """
        Map a Card object to a Persian suit+rank string, e.g. '♠️A' or '♦️9'.
        """
        suit_symbols = {'S': '♠️', 'H': '♥️', 'D': '♦️', 'C': '♣️'}
        rank_names = {
            14: 'A', 13: 'K', 12: 'Q', 11: 'J',
            10: '10', 9: '9', 8: '8', 7: '7',
            6: '6', 5: '5', 4: '4', 3: '3', 2: '2',
        }
        suit = suit_symbols.get(card.suit, card.suit)
        rank = rank_names.get(card.value, str(card.value))
        return f"{suit}{rank}"

    def send_dynamic_card_keyboard(self, chat_id, player):
        """
        In the group chat, mention @player and show a two‐button keyboard
        of their private cards.  selective=True ensures only the mentioned
        player sees these buttons.
        """
        cards_display = [self._card_display(c) for c in player.cards]
        markup = ReplyKeyboardMarkup(
            keyboard=[cards_display],
            selective=True,
            resize_keyboard=True,
        )
        self._bot.send_message(
            chat_id=chat_id,
            text=f"{player.mention_markdown} کارت‌های شما:",
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )
    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[cards],
            selective=True,
            resize_keyboard=True,
        )

    @staticmethod
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
        chat_id: ChatId,
        cards: Cards,
        mention_markdown: Mention,
        ready_message_id: str,
    ) -> None:
        markup = PokerBotViewer._get_cards_markup(cards)
        self._bot.send_message(
            chat_id=chat_id,
            text=f"🃏 ارسال کارت‌ها برای {mention_markdown}",
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
    ) -> None:
        if len(game.cards_table) == 0:
            cards_table = "❓ کارت روی میز وجود ندارد"
        else:
            cards_table = " ".join(game.cards_table)

        text = (
            "🎲 نوبت برای {}\n"
            "💠 کارت‌های روی میز: {}\n"
            "💰 موجودی: *{}$*\n"
            "🔼 بیشترین شرط: *{}$*"
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
