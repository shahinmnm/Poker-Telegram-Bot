#!/usr/bin/env python3

import logging

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import traceback  # <--- برای لاگ دقیق خطا اضافه شد

from pokerapp.entities import PlayerAction, UserException
from pokerapp.pokerbotmodel import (
    PokerBotModel,
    STOP_CONFIRM_CALLBACK,
    STOP_RESUME_CALLBACK,
)
from pokerapp.game_engine import clear_all_message_ids


logger = logging.getLogger(__name__)

class PokerBotCotroller:
    def __init__(self, model: PokerBotModel, application: Application):
        self._model = model
        self._view = model._view  # view for error messages

        application.add_handler(CommandHandler('ready', self._handle_ready))
        application.add_handler(CommandHandler('start', self._handle_start))
        application.add_handler(CommandHandler('stop', self._handle_stop))
        application.add_handler(CommandHandler('money', self._handle_money))
        application.add_handler(CommandHandler('ban', self._handle_ban))
        application.add_handler(CommandHandler('get_save_error', self._handle_get_save_error))

        # game management command
        application.add_handler(CommandHandler('newgame', self._handle_create_game))

        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_text_buttons,
            )
        )

        application.add_handler(CallbackQueryHandler(self._handle_start, pattern="^start_game$"))
        application.add_handler(CallbackQueryHandler(self._handle_join_game, pattern="^join_game$"))
        application.add_handler(CallbackQueryHandler(self._handle_board_card, pattern="^board_card_"))
        application.add_handler(CallbackQueryHandler(self._handle_hand_card, pattern="^hand_card_"))
        application.add_handler(CallbackQueryHandler(self._handle_anchor_menu, pattern="^anchor:"))
        application.add_handler(
            CallbackQueryHandler(
                self._handle_stop_vote,
                pattern=f"^({'|'.join([STOP_CONFIRM_CALLBACK, STOP_RESUME_CALLBACK])})$",
            )
        )
        application.add_handler(CallbackQueryHandler(self.middleware_user_turn))


    async def middleware_user_turn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        این میدل‌ور قبل از اجرای هر دستور دکمه اینلاین، نوبت بازیکن را چک می‌کند
        و لاگ‌های دقیقی برای دیباگ ثبت می‌کند.
        """
        user_id = update.effective_user.id
        game, chat_id = await self._model._get_game(update, context)

        print(f"\nDEBUG: Callback received from user {user_id} in chat {chat_id}.")

        if not game or game.state not in self._model.ACTIVE_GAME_STATES:
            print("DEBUG: Game not active or finished. Ignoring callback.")
            # می‌توانید یک پیام به کاربر بدهید که بازی فعال نیست
            query = update.callback_query
            if query:
                try:
                    await query.answer(text="بازی در جریان نیست.", show_alert=False)
                except BadRequest as e:
                    if "query is too old" not in str(e).lower():
                        raise
            return

        current_player = self._model._current_turn_player(game)
        if not current_player:
            print("WARNING: No current player found in active game. Ignoring callback.")
            return

        if current_player.user_id != user_id:
            print(f"DEBUG: Not user's turn. Current turn: {current_player.user_id}, Requester: {user_id}.")
            query = update.callback_query
            if query:
                try:
                    await query.answer(text="☝️ نوبت شما نیست!", show_alert=True)
                except BadRequest as e:
                    if "query is too old" not in str(e).lower():
                        raise
            return

        # اگر نوبت کاربر بود، به متد اصلی برای پردازش دکمه برو
        print("DEBUG: User's turn confirmed. Proceeding to _handle_button_clicked.")
        await self._handle_button_clicked(update, context)


    async def _handle_text_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles clicks on custom reply keyboard buttons."""
        text = update.message.text
        chat = update.effective_chat
        if text == "📊 آمار بازی":
            if chat.type != chat.PRIVATE:
                await self._view.send_message(
                    chat.id,
                    "ℹ️ برای مشاهده آمار دقیق، لطفاً در گفت‌وگوی خصوصی ربات دکمه «📊 آمار بازی» را بزنید.",
                )
            else:
                await self._model._send_statistics_report(update, context)
            return
        if text == "🎁 بونوس روزانه":
            if chat.type != chat.PRIVATE:
                await self._view.send_message(
                    chat.id,
                    "🎁 برای دریافت بونوس روزانه، این گزینه را در چت خصوصی انتخاب کنید.",
                )
            else:
                await self._model.bonus(update, context)
            return
        if text == "⚙️ تنظیمات":
            await self._view.send_message(
                chat.id,
                "⚙️ بخش تنظیمات به‌زودی با گزینه‌های شخصی‌سازی و مدیریت کیف‌پول فعال می‌شود.",
            )
            return
        if text == "🃏 شروع بازی":
            await self._view.send_message(
                chat.id,
                "🃏 برای راه‌اندازی میز جدید، در گروه مورد نظر دستور /newgame را ارسال کنید یا از مدیر گروه بخواهید بازی را آغاز کند.",
            )
            return
        if text == "🤝 بازی با ناشناس":
            if chat.type != chat.PRIVATE:
                await self._view.send_message(
                    chat.id,
                    "ℹ️ برای جستجوی حریف ناشناس، از چت خصوصی ربات استفاده کنید.",
                )
            else:
                await self._model.handle_private_matchmaking_request(update, context)
            return
        normalized = text.replace("✅ ", "").replace("🔁 ", "")
        if normalized == "فلاپ":
            game, chat_id = await self._model._get_game(update, context)
            await self._model.add_cards_to_table(0, game, chat_id, "🃏 فلاپ")
        elif normalized == "ترن":
            game, chat_id = await self._model._get_game(update, context)
            await self._model.add_cards_to_table(0, game, chat_id, "🃏 ترن")
        elif normalized == "ریور":
            game, chat_id = await self._model._get_game(update, context)
            await self._model.add_cards_to_table(0, game, chat_id, "🃏 ریور")

    async def _handle_get_save_error(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat = update.effective_chat
        admin_chat_id = getattr(self._view, "_admin_chat_id", None)
        if admin_chat_id is None or chat is None or chat.id != admin_chat_id:
            return

        args = list(getattr(context, "args", []) or [])
        await self._model.handle_admin_command("/get_save_error", args, admin_chat_id)

    async def _handle_ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = None
        message_id = None

        if update.callback_query:
            msg = update.callback_query.message
            if msg:
                chat = getattr(msg, "chat", None)
                chat_id = getattr(chat, "id", getattr(msg, "chat_id", None))
                message_id = getattr(msg, "message_id", None)
        elif update.message:
            chat = getattr(update.message, "chat", None)
            chat_id = getattr(chat, "id", getattr(update.message, "chat_id", None))
            message_id = getattr(update.message, "message_id", None)

        if chat_id is not None and message_id is not None:
            game = await self._model._table_manager.get_game(chat_id)
            current_game_id = getattr(game, "id", None)
            messaging_service = getattr(self._view, "_messaging_service", None)
            if messaging_service is None:
                messaging_service = getattr(self._view, "_messenger", None)
            if messaging_service is None:
                logger.info(
                    "Skipping /ready: messaging service unavailable",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "current_game_id": current_game_id,
                    },
                )
                return
            if not await messaging_service.is_message_id_active(
                chat_id, message_id, current_game_id
            ):
                logger.info(
                    "Skipping /ready: inactive or stale message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "current_game_id": current_game_id,
                    },
                )
                return

        await self._model.ready(update, context)

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query:
            await update.callback_query.answer()
        await self._model.start(update, context)

    async def _handle_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self._model.stop(update, context)
        except UserException as ex:
            chat_id = update.effective_chat.id
            message_text = str(ex)
            cleanup_messages = {
                getattr(self._model._game_engine, "ERROR_NO_ACTIVE_GAME", None),
                getattr(self._model._game_engine, "STOPPED_NOTIFICATION", None),
            }
            if message_text in cleanup_messages:
                game = await self._model._table_manager.get_game(chat_id)
                await self._model._player_manager.cleanup_ready_prompt(game, chat_id)
                clear_all_message_ids(game)
                logger.info(
                    "Cleared all message IDs after stop",
                    extra={
                        "chat_id": chat_id,
                        "game_id": getattr(game, "id", None),
                    },
                )
                await self._model._table_manager.save_game(chat_id, game)
            await self._view.send_message(chat_id, message_text)

    async def _handle_stop_vote(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        try:
            if query.data == STOP_CONFIRM_CALLBACK:
                await self._model.confirm_stop_vote(update, context)
                await query.answer()
            elif query.data == STOP_RESUME_CALLBACK:
                await self._model.resume_stop_vote(update, context)
                await query.answer()
        except UserException as exc:
            await query.answer(text=str(exc), show_alert=True)
            await self._view.send_message(update.effective_chat.id, str(exc))

    async def _handle_ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._model.ban_player(update, context)

    async def _handle_money(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._model.bonus(update, context)

    async def _handle_create_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._model.create_game(update, context)

    async def _handle_join_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._model.join_game(update, context)

    async def _handle_board_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Sends a larger image of a board card to the requesting user."""
        query = update.callback_query
        if not query or not query.data:
            return

        index_str = query.data.split("_")[-1]
        try:
            index = int(index_str)
        except ValueError:
            await query.answer()
            return

        game, _ = await self._model._get_game(update, context)
        if 0 <= index < len(game.cards_table):
            card = game.cards_table[index]
            await query.answer(text=str(card), show_alert=True)
            return
        await query.answer()

    async def _handle_hand_card(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        parts = query.data.split("_")
        if len(parts) < 4:
            await query.answer()
            return

        player_id = parts[2]
        index_str = parts[3]
        requester_id = str(update.effective_user.id)
        if player_id != requester_id:
            await query.answer(text="این کارت متعلق به شما نیست!", show_alert=True)
            return

        try:
            index = int(index_str)
        except ValueError:
            await query.answer()
            return

        try:
            game, _ = await self._model._get_game(update, context)
        except Exception:
            await query.answer()
            return

        player = next((p for p in game.players if str(p.user_id) == player_id), None)
        if not player or index < 0 or index >= len(player.cards):
            await query.answer()
            return

        await query.answer(text=str(player.cards[index]), show_alert=True)

    async def _handle_anchor_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        action = query.data.split(":", 1)[1] if ":" in query.data else ""
        chat = update.effective_chat

        if action == "noop":
            await query.answer()
            return

        if action == "stats":
            await query.answer()
            await self._model._send_statistics_report(update, context)
            return

        if action == "help":
            await query.answer()
            help_text = (
                "🛠 راهنما:\n"
                "• /newgame — ساخت میز جدید\n"
                "• /ready — آماده شدن برای دست بعدی\n"
                "• /money — دریافت بونوس روزانه"
            )
            await self._view.send_message(chat.id, help_text)
            return

        if action == "wallet":
            await query.answer()
            await self._model._send_wallet_balance(update, context)
            return

        if action == "chat":
            await query.answer(text="💬 یک گفتگوی دوستانه تازه شروع کن!", show_alert=False)
            return

        await query.answer()

    async def _handle_button_clicked(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        # ... (کدهای دیباگ و حذف مارک‌آپ که قبلاً داشتیم)
        chat_id = update.effective_chat.id

        # ۲. اجرای اکشن بازیکن
        try:
            query_data = update.callback_query.data # <--- دریافت دیتا از کوئری

            # --- شروع بلوک اصلاح شده ---
            if query_data == PlayerAction.CHECK.value or query_data == PlayerAction.CALL.value:
                # self._model.call_check(update, context)  # <--- این متد دیگر وجود ندارد
                await self._model.player_action_call_check(update, context)
            elif query_data == PlayerAction.FOLD.value:
                # self._model.fold(update, context) # <--- این متد دیگر وجود ندارد
                await self._model.player_action_fold(update, context)
            elif query_data == str(PlayerAction.SMALL.value):
                # self._model.raise_rate_bet(update, context, PlayerAction.SMALL.value) # <--- این متد دیگر وجود ندارد
                await self._model.player_action_raise_bet(update, context, PlayerAction.SMALL.value)
            elif query_data == str(PlayerAction.NORMAL.value):
                # self._model.raise_rate_bet(update, context, PlayerAction.NORMAL.value) # <--- این متد دیگر وجود ندارد
                await self._model.player_action_raise_bet(update, context, PlayerAction.NORMAL.value)
            elif query_data == str(PlayerAction.BIG.value):
                # self._model.raise_rate_bet(update, context, PlayerAction.BIG.value) # <--- این متد دیگر وجود ندارد
                await self._model.player_action_raise_bet(update, context, PlayerAction.BIG.value)
            elif query_data == PlayerAction.ALL_IN.value:
                # self._model.all_in(update, context) # <--- این متد دیگر وجود ندارد
                await self._model.player_action_all_in(update, context)
            # --- پایان بلوک اصلاح شده ---
            else:
                print(f"WARNING: Unknown callback query data: {query_data}")

        except UserException as ex:
            print(f"INFO: Handled UserException: {ex}")
            await self._view.send_message(chat_id=chat_id, text=str(ex))
        except Exception:
            # گرفتن تمام خطاهای دیگر برای دیباگ
            print(f"FATAL ERROR: Unexpected exception in player_action.")
            traceback.print_exc() # چاپ کامل خطا
            await self._view.send_message(chat_id, "یک خطای بحرانی در پردازش حرکت رخ داد. بازی ریست می‌شود.")

        # ==================== پایان بلوک اصلی دیباگ و اصلاح ====================
