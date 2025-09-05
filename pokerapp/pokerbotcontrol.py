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
import threading  # یا import asyncio اگر async

from pokerapp.entities import PlayerAction, UserException, Game
from pokerapp.pokerbotmodel import PokerBotModel

KEY_CHAT_DATA_GAME = "game" # <--- این متغیر برای دسترسی به بازی اضافه شد

class PokerBotCotroller:
    def __init__(self, model: PokerBotModel, updater: Updater):
        self._model = model
        self._view = model._view # <--- دسترسی به view برای ارسال پیام خطا
        self._lock = threading.Lock()  # برای sync. اگر async: self._lock = asyncio.Lock()

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
        تغییرات: lock برای atomicity، چک state دقیق‌تر. جلوگیری از race ریشه‌ای.
        """
        with self._lock:  # Atomic: فقط یک callback همزمان پردازش شود
            chat_id = update.effective_chat.id
            user_id = update.effective_user.id
            game: Game = context.chat_data.get(KEY_CHAT_DATA_GAME)

            print(f"\nDEBUG: Callback received from user {user_id} in chat {chat_id}.")

            if not game or game.state not in self._model.ACTIVE_GAME_STATES or game.state == GameState.FINISHED:
                print("DEBUG: Game not active or finished. Ignoring callback.")
                query = update.callback_query
                if query:
                    query.answer(text="بازی در جریان نیست.", show_alert=False)
                return

            current_player = self._model._current_turn_player(game)
            if not current_player or current_player.user_id != user_id:
                print(f"DEBUG: Not user's turn or invalid player. Ignoring.")
                if update.callback_query:
                    update.callback_query.answer(text="☝️ نوبت شما نیست!", show_alert=True)
                return

            # پردازش اقدام (به handler منتقل شد برای atomicity)
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
        """
        تغییرات: 
        - اضافه کردن چک atomic برای پایان راند پس از هر اقدام (ریشه‌ای برای جلوگیری از race condition).
        - بروزرسانی has_acted برای بازیکن فعلی.
        - چک is_round_ended برای تصمیم‌گیری فراخوانی _showdown یا پیشرفت راند.
        - محاسبه نوبت بعدی اگر راند تمام نشده باشد.
        - لاگ بیشتر برای دیباگ.
        - حفظ کد موجود برای اجرای اقدامات و حذف مارک‌آپ (با فرض وجود آن).
        """
        chat_id = update.effective_chat.id
        game: Game = context.chat_data.get(KEY_CHAT_DATA_GAME)
    
        # بخش حذف مارک‌آپ (کد موجود - اگر دارید، نگه دارید؛ در غیر این صورت، کامنت کنید)
        # ... (بخش حذف مارک‌آپ، مثل self._view.remove_markup(chat_id, game.turn_message_id))
    
        # گرفتن بازیکن فعلی (برای بروزرسانی has_acted)
        current_player = self._model._current_turn_player(game)
        if not current_player:
            print("WARNING: No current player found in _handle_button_clicked.")
            return
    
        # ۲. اجرای اکشن بازیکن (کد موجود بدون تغییر)
        try:
            query_data = update.callback_query.data  # <--- دریافت دیتا از کوئری
    
            # --- شروع بلوک اصلاح شده (کد موجود) ---
            if query_data == PlayerAction.CHECK.value or query_data == PlayerAction.CALL.value:
                self._model.player_action_call_check(update, context, game)  # <--- نام صحیح جدید
            elif query_data == PlayerAction.FOLD.value:
                self._model.player_action_fold(update, context, game)  # <--- نام صحیح جدید
            elif query_data == str(PlayerAction.SMALL.value):
                self._model.player_action_raise_bet(update, context, game, PlayerAction.SMALL.value)  # <--- نام صحیح جدید
            elif query_data == str(PlayerAction.NORMAL.value):
                self._model.player_action_raise_bet(update, context, game, PlayerAction.NORMAL.value)  # <--- نام صحیح جدید
            elif query_data == str(PlayerAction.BIG.value):
                self._model.player_action_raise_bet(update, context, game, PlayerAction.BIG.value)  # <--- نام صحیح جدید
            elif query_data == PlayerAction.ALL_IN.value:
                self._model.player_action_all_in(update, context, game)  # <--- نام صحیح جدید
            # --- پایان بلوک اصلاح شده ---
            else:
                print(f"WARNING: Unknown callback query data: {query_data}")
    
        except UserException as ex:
            print(f"INFO: Handled UserException: {ex}")
            self._view.send_message(chat_id=chat_id, text=str(ex))
        except Exception:
            # گرفتن تمام خطاهای دیگر برای دیباگ
            print(f"FATAL ERROR: Unexpected exception in player_action.")
            traceback.print_exc()  # چاپ کامل خطا
            self._view.send_message(chat_id, "یک خطای بحرانی در پردازش حرکت رخ داد. بازی ریست می‌شود.")
            if game:
                game.reset()  # ریست کردن بازی برای جلوگیری از قفل شدن
            return  # زود خارج شوید تا ادامه ندهد
    
        # --- بخش جدید: بروزرسانی state پس از اقدام (ریشه‌ای برای جلوگیری از تکرار) ---
        print(f"DEBUG: Action processed for player {current_player.user_id}. Updating state...")
    
        # مارک بازیکن به عنوان اقدام‌کرده
        current_player.has_acted = True
    
        # چک پایان راند
        if game.is_round_ended():
            print("DEBUG: Round ended detected.")
            if game.state == GameState.ROUND_RIVER:
                print("DEBUG: Calling _showdown.")
                self._model._showdown(game, chat_id, context)
            else:
                # پیشرفت به راند بعدی (مثل flop به turn)
                print("DEBUG: Advancing to next round.")
                self._model._advance_round(game, chat_id)
        else:
            # نوبت به بازیکن بعدی (با استفاده از next_occupied_seat)
            next_index = game.next_occupied_seat(game.current_player_index)
            if next_index != -1:
                game.current_player_index = next_index
                print(f"DEBUG: Advancing turn to next player at seat {next_index}.")
                # بروزرسانی view برای نوبت جدید (مثل ارسال پیام نوبت)
                next_player = game.get_player_by_seat(next_index)
                if next_player:
                    self._model._send_turn_message(chat_id, game, next_player)  # فرض: متدی برای ارسال پیام نوبت
            else:
                print("WARNING: No next player found - possible game state error.")
    
        print("DEBUG: _handle_button_clicked completed.")
