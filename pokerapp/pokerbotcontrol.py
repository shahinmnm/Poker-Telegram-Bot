#!/usr/bin/env python3

from telegram import Update
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
    Updater,
    MessageHandler,
    Filters,
)
import traceback  # <--- برای لاگ دقیق خطا اضافه شد

from pokerapp.entities import PlayerAction, UserException, Game
from pokerapp.pokerbotmodel import PokerBotModel

KEY_CHAT_DATA_GAME = "game" 

class PokerBotCotroller:
    def __init__(self, model: PokerBotModel, updater: Updater, mdm=None):
        self._mdm = mdm
        self._view = model._view
        self._model = model
        self._view = model._view

        SHOW_CARDS_TEXT = "🃏 نمایش کارت‌ها"
        HIDE_CARDS_TEXT = "🙈 پنهان کردن کارت‌ها"
        SHOW_TABLE_TEXT = "👁️ نمایش میز"

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

        updater.dispatcher.add_handler(
            MessageHandler(
                Filters.text([SHOW_CARDS_TEXT, HIDE_CARDS_TEXT, SHOW_TABLE_TEXT]) & (~Filters.command),
                self._handle_text_buttons
            )
        )

        updater.dispatcher.add_handler(
            CallbackQueryHandler(self.middleware_user_turn)
        )
    def attach_mdm(self, mdm):
        self._mdm = mdm

    def middleware_user_turn(self, update: Update, context: CallbackContext) -> None:

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        game: Game = context.chat_data.get(KEY_CHAT_DATA_GAME)

        print(f"\nDEBUG: Callback received from user {user_id} in chat {chat_id}.")

        if not game or game.state not in self._model.ACTIVE_GAME_STATES:
            print("DEBUG: Game not active or finished. Ignoring callback.")
            query = update.callback_query
            if query:
                query.answer(text="بازی در جریان نیست.", show_alert=False)
            return

        current_player = self._model._current_turn_player(game)
        if not current_player:
            print("WARNING: No current player found in active game. Ignoring callback.")
            return

        if current_player.user_id != user_id:
            print(f"DEBUG: Not user's turn. Current turn: {current_player.user_id}, Requester: {user_id}.")
            query = update.callback_query
            if query:
                query.answer(text="☝️ نوبت شما نیست!", show_alert=True)
            return

        print("DEBUG: User's turn confirmed. Proceeding to _handle_button_clicked.")
        self._handle_button_clicked(update, context)


    def _handle_text_buttons(self, update: Update, context: CallbackContext) -> None:
        """Handles clicks on custom reply keyboard buttons."""
        text = update.message.text

        SHOW_CARDS_TEXT = "🃏 نمایش کارت‌ها"
        HIDE_CARDS_TEXT = "🙈 پنهان کردن کارت‌ها"
        SHOW_TABLE_TEXT = "👁️ نمایش میز"

        if text == HIDE_CARDS_TEXT:
            self._model.hide_cards(update, context)
        elif text == SHOW_CARDS_TEXT:
            self._model.send_cards_to_user(update, context)
        elif text == SHOW_TABLE_TEXT:
            self._model.show_table(update, context)

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
        chat_id = update.effective_chat.id
        game: Game = context.chat_data.get(KEY_CHAT_DATA_GAME)

        try:
            query_data = update.callback_query.data # <--- دریافت دیتا از کوئری

            if query_data == PlayerAction.CHECK.value or query_data == PlayerAction.CALL.value:
                self._model.player_action_call_check(update, context, game) # <--- نام صحیح جدید
            elif query_data == PlayerAction.FOLD.value:
                self._model.player_action_fold(update, context, game) # <--- نام صحیح جدید
            elif query_data == str(PlayerAction.SMALL.value):
                self._model.player_action_raise_bet(update, context, game, PlayerAction.SMALL.value) # <--- نام صحیح جدید
            elif query_data == str(PlayerAction.NORMAL.value):
                self._model.player_action_raise_bet(update, context, game, PlayerAction.NORMAL.value) # <--- نام صحیح جدید
            elif query_data == str(PlayerAction.BIG.value):
                self._model.player_action_raise_bet(update, context, game, PlayerAction.BIG.value) # <--- نام صحیح جدید
            elif query_data == PlayerAction.ALL_IN.value:
                self._model.player_action_all_in(update, context, game) # <--- نام صحیح جدید
            else:
                print(f"WARNING: Unknown callback query data: {query_data}")

        except UserException as ex:
            print(f"INFO: Handled UserException: {ex}")
            self._view.send_message(chat_id=chat_id, text=str(ex))
        except Exception:
            print(f"FATAL ERROR: Unexpected exception in player_action.")
            traceback.print_exc() # چاپ کامل خطا
            self._view.send_message(chat_id, "یک خطای بحرانی در پردازش حرکت رخ داد. بازی ریست می‌شود.")
            if game:
                game.reset() # ریست کردن بازی برای جلوگیری از قفل شدن
