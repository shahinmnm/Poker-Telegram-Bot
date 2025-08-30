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

KEY_CHAT_DATA_GAME = "game" # <--- این متغیر برای دسترسی به بازی اضافه شد

class PokerBotCotroller:
    def __init__(self, model: PokerBotModel, updater: Updater):
        self._model = model
        self._view = model._view # <--- دسترسی به view برای ارسال پیام خطا

        # تعریف متون دکمه به عنوان متغیر برای جلوگیری از خطا
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

        # ==================== شروع بلوک اصلاح شده اصلی ====================
        # middleware_user_turn برای بررسی نوبت بازیکن مستقیما اینجا پیاده‌سازی شده
        updater.dispatcher.add_handler(
            CallbackQueryHandler(self.middleware_user_turn)
        )
        # ==================== پایان بلوک اصلاح شده اصلی ====================


    def middleware_user_turn(self, update: Update, context: CallbackContext) -> None:
        """
        این میدل‌ور قبل از اجرای هر دستور دکمه اینلاین، نوبت بازیکن را چک می‌کند
        و لاگ‌های دقیقی برای دیباگ ثبت می‌کند.
        """
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        game: Game = context.chat_data.get(KEY_CHAT_DATA_GAME)

        print(f"\nDEBUG: Callback received from user {user_id} in chat {chat_id}.")

        if not game or game.state not in self._model.ACTIVE_GAME_STATES:
            print("DEBUG: Game not active or finished. Ignoring callback.")
            # می‌توانید یک پیام به کاربر بدهید که بازی فعال نیست
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

        # اگر نوبت کاربر بود، به متد اصلی برای پردازش دکمه برو
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
        # ==================== شروع بلوک اصلی دیباگ و اصلاح ====================
        chat_id = update.effective_chat.id
        game: Game = context.chat_data.get(KEY_CHAT_DATA_GAME)

        # لاگ کردن اطلاعات مهم قبل از هر کاری
        query_data = update.callback_query.data
        print(f"DEBUG: Processing player action. Action: '{query_data}'")
        print(f"DEBUG: Current Game state: {game.state}, Turn message ID from game object: {game.turn_message_id}")

        # ۱. تلاش برای حذف دکمه‌های پیام نوبت قبلی
        if game.turn_message_id:
            try:
                print(f"DEBUG: Attempting to remove markup from message {game.turn_message_id} in chat {chat_id}.")
                self._view.remove_markup(
                    chat_id=chat_id,
                    message_id=game.turn_message_id,
                )
                print(f"DEBUG: Successfully removed markup for message {game.turn_message_id}.")
            except Exception:
                # این مهمترین لاگ است! خطا را با جزئیات چاپ می‌کند
                print(f"CRITICAL ERROR: Failed to remove markup for message {game.turn_message_id}.")
                traceback.print_exc()  # چاپ کامل خطا
                # با این حال ادامه می‌دهیم تا بازی قفل نشود
        else:
            print("WARNING: game.turn_message_id was None. Cannot remove markup.")


        # ۲. اجرای اکشن بازیکن
        try:
            if query_data == PlayerAction.CHECK.value or query_data == PlayerAction.CALL.value:
                self._model.call_check(update, context)
            elif query_data == PlayerAction.FOLD.value:
                self._model.fold(update, context)
            elif query_data == str(PlayerAction.SMALL.value):
                self._model.raise_rate_bet(update, context, PlayerAction.SMALL.value)
            elif query_data == str(PlayerAction.NORMAL.value):
                self._model.raise_rate_bet(update, context, PlayerAction.NORMAL.value)
            elif query_data == str(PlayerAction.BIG.value):
                self._model.raise_rate_bet(update, context, PlayerAction.BIG.value)
            elif query_data == PlayerAction.ALL_IN.value:
                self._model.all_in(update, context)
            else:
                print(f"WARNING: Unknown callback query data: {query_data}")

        except UserException as ex:
            print(f"INFO: Handled UserException: {ex}")
            self._view.send_message(chat_id=chat_id, text=str(ex))
        except Exception:
            # گرفتن تمام خطاهای دیگر برای دیباگ
            print(f"FATAL ERROR: Unexpected exception in player_action.")
            traceback.print_exc() # چاپ کامل خطا
            self._view.send_message(chat_id, "یک خطای بحرانی در پردازش حرکت رخ داد. بازی ریست می‌شود.")
            if game:
                game.reset() # ریست کردن بازی برای جلوگیری از قفل شدن

        # ==================== پایان بلوک اصلی دیباگ و اصلاح ====================
