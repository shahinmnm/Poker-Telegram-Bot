#!/usr/bin/env python3

from telegram import Update
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
    Updater,
    MessageHandler,  # <<<< جدید
    Filters,         # <<<< جدید
)

from pokerapp.entities import PlayerAction
from pokerapp.pokerbotmodel import PokerBotModel

class PokerBotCotroller:
    def __init__(self, model: PokerBotModel, updater: Updater):
        self._model = model

        # <<<< شروع بلوک جدید: تعریف متن دکمه ها و MessageHandler >>>>
        # تعریف متون دکمه به عنوان متغیر برای جلوگیری از خطا
        SHOW_CARDS_TEXT = "🃏 نمایش کارت‌ها"
        HIDE_CARDS_TEXT = "🙈 پنهان کردن کارت‌ها"
        SHOW_TABLE_TEXT = "👁️ نمایش میز"
        # <<<< پایان بلوک جدید >>>>

        updater.dispatcher.add_handler(
            CommandHandler('ready', self._handle_ready)
        )
        updater.dispatcher.add_handler(
            CommandHandler('start', self._handle_start)
        )
        updater.dispatcher.add_handler(
            CommandHandler('stop', self._handle_stop)
        )
        updater.dispatcher.add_handler(
            CommandHandler('money', self._handle_money)
        )
        updater.dispatcher.add_handler(
            CommandHandler('ban', self._handle_ban)
        )
        updater.dispatcher.add_handler(
            CommandHandler('cards', self._handle_cards)
        )

        # <<<< شروع بلوک جدید: اضافه کردن MessageHandler برای دکمه های متنی >>>>
        # این Handler به پیام‌های متنی که با محتوای دکمه‌های ما مطابقت دارند، گوش می‌دهد
        updater.dispatcher.add_handler(
            MessageHandler(
                Filters.text([SHOW_CARDS_TEXT, HIDE_CARDS_TEXT, SHOW_TABLE_TEXT]) & (~Filters.command),
                self._handle_text_buttons
            )
        )
        # <<<< پایان بلوک جدید >>>>

        updater.dispatcher.add_handler(
            CallbackQueryHandler(
                self._model.middleware_user_turn(
                    self._handle_button_clicked,
                ),
            )
        )

    # <<<< شروع متد جدید: متد برای پردازش دکمه های متنی >>>>
    def _handle_text_buttons(self, update: Update, context: CallbackContext) -> None:
        """Handles clicks on custom reply keyboard buttons."""
        text = update.message.text
        
        # تعریف مجدد متون برای استفاده در این متد
        SHOW_CARDS_TEXT = "🃏 نمایش کارت‌ها"
        HIDE_CARDS_TEXT = "🙈 پنهان کردن کارت‌ها"
        SHOW_TABLE_TEXT = "👁️ نمایش میز"

        if text == HIDE_CARDS_TEXT:
            self._model.hide_cards(update, context)
        elif text == SHOW_CARDS_TEXT:
            # این همان کاری است که دستور /cards انجام می‌دهد
            self._model.send_cards_to_user(update, context)
        elif text == SHOW_TABLE_TEXT:
            self._model.show_table(update, context)
    # <<<< پایان متد جدید >>>>

    def _handle_ready(self, update: Update, context: CallbackContext) -> None:
        self._model.ready(update, context)

    def _handle_start(self, update: Update, context: CallbackContext) -> None:
        self._model.start(update, context)

    def _handle_stop(self, update: Update, context: CallbackContext) -> None:
        self._model.stop(user_id=update.effective_message.from_user.id)

    def _handle_cards(self, update: Update, context: CallbackContext) -> None:
        self._model.send_cards_to_user(update, context)

    def _handle_ban(self, update: Update, context: CallbackContext) -> None:
        self._model.ban_player(update, context)

    def _handle_money(self, update: Update, context: CallbackContext) -> None:
        self._model.bonus(update, context)

    def _handle_button_clicked(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        query_data = update.callback_query.data
        if query_data == PlayerAction.CHECK.value or query_data == PlayerAction.CALL.value:
            self._model.call_check(update, context)
        elif query_data == PlayerAction.FOLD.value:
            self._model.fold(update, context)
        elif data == str(PlayerAction.SMALL.value):
            # مقدار عددی را مستقیم پاس می‌دهیم
            self._model.raise_rate_bet(update, context, PlayerAction.SMALL.value)
        elif data == str(PlayerAction.NORMAL.value):
            # مقدار عددی را مستقیم پاس می‌دهیم
            self._model.raise_rate_bet(update, context, PlayerAction.NORMAL.value)
        elif data == str(PlayerAction.BIG.value):
            # مقدار عددی را مستقیم پاس می‌دهیم
            self._model.raise_rate_bet(update, context, PlayerAction.BIG.value)
        elif query_data == PlayerAction.ALL_IN.value:
            self._model.all_in(update, context)
