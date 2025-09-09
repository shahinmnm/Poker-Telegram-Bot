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
from telegram.error import BadRequest, Unauthorized
from threading import Timer
from io import BytesIO
from typing import List, Optional
from pokerapp.winnerdetermination import HAND_NAMES_TRANSLATIONS
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
    PlayerState,
)
class PokerBotViewer:
    def __init__(self, bot: Bot):
        self._bot = bot
        self._desk_generator = DeskImageGenerator()
        
    def _format_last_actions(self, game: Game) -> str:
        """
        متن «۳ اکشن اخیر» را به‌صورت لیست گلوله‌ای تولید می‌کند.
        اگر لیست خالی بود، رشته خالی برمی‌گرداند.
        """
        if not game.last_actions:
            return ""
        bullets = "\n".join(f"• {a}" for a in game.last_actions)
        return f"\n📝 آخرین اکشن‌ها:\n{bullets}"

    def _build_hud_text(self, game: Game) -> str:
        """
        متن HUD را می‌سازد:
        - خط اول کوتاه و مناسب پین: میز/پات/نوبت (برای یک نگاه سریع)
        - سپس وضعیت کارت‌های رو میز و سقف دور
        """
        # --- نوبت فعلی به‌صورت امن + برچسب ALL-IN ---
        turn_str = "—"
        try:
            if 0 <= game.current_player_index < game.seated_count():
                p = game.get_player_by_seat(game.current_player_index)
                if p:
                    turn_str = p.mention_markdown if p.state != PlayerState.ALL_IN else f"{p.mention_markdown} (🔴 ALL-IN)"
        except Exception:
            pass
    
        # --- خط اول کوتاه برای پین (یک‌خطه و پرمعنا) ---
        # مثال خروجی: "🃏 میز | پات: 120$ | نوبت: @Alice (🔴 ALL-IN)"
        line1 = f"🃏 میز | پات: {game.pot}$ | نوبت: {turn_str}"
    
        # --- کارت‌های روی میز ---
        # اسم فیلد درست: cards_table  (لیست از Card که str هم هست)
        table_cards = "🚫" if not getattr(game, "cards_table", None) else "  ".join(map(str, game.cards_table))   
        # --- سقف دور ---
        cap = game.max_round_rate if game.max_round_rate else 0
    
        # --- بدنه ---
        body = (
            f"\n\n🃏 کارت‌های روی میز:\n{table_cards}\n\n"
            f"💰 پات: {game.pot}$ | 🪙 سقف این دور: {cap}$\n"
            f"▶️ نوبت: {turn_str}"
        )
    
        # --- آخرین اکشن‌ها (۳ تای اخیر) ---
        body += self._format_last_actions(game)
    
        header = line1
        return f"{header}\n\n{body}"
    def ensure_hud(self, chat_id: ChatId, game: Game) -> Optional[MessageId]:
        """
        یک پیام HUD ثابت می‌سازد و آیدی‌اش را در game.hud_message_id ذخیره می‌کند.
        اگر موجود باشد، همان را برمی‌گرداند.
        (بدون هیچ fallback متنی که پیام جدید بسازد)
        """
        if getattr(game, "hud_message_id", None):
            return game.hud_message_id
        if getattr(game, "_hud_creating", False):
            return getattr(game, "hud_message_id", None)
    
        game._hud_creating = True
        try:
            text = self._build_hud_text(game)
            msg_id = self.send_message_return_id(chat_id=chat_id, text=text)
            if msg_id:
                game.hud_message_id = msg_id
                return msg_id
            return None  # ← هیچ پیام اضافی نساز
        finally:
            game._hud_creating = False

    def edit_hud(self, chat_id: ChatId, game: Game) -> None:
        """
        متن HUD را روی همان پیام ثابت ادیت می‌کند.
        اگر HUD هنوز ساخته نشده باشد، یک‌بار می‌سازد.
        برای جلوگیری از ادیتِ بی‌فایده، اگر متن تغییری نکرده باشد، ادیت انجام نمی‌شود.
        """
        if not getattr(game, "hud_message_id", None):
            self.ensure_hud(chat_id, game)
    
        if not game.hud_message_id:
            return
    
        new_text = self._build_hud_text(game)
    
        # --- Debounce: اگر متن تغییری نکرده، ادیت نزنیم
        if getattr(game, "_last_hud_text", None) == new_text:
            return
    
        try:
            self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.hud_message_id,
                text=new_text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            game._last_hud_text = new_text  # به خاطر بسپاریم برای دیباونس بعدی
        except Exception as e:
            print(f"[HUD] edit_message_text error: {e}")
    
        # اطمینان از حذف مارک‌آپ اگر قبلاً دکمه‌ای روی HUD بوده
        self.remove_markup(chat_id=chat_id, message_id=game.hud_message_id)


    def _build_turn_text(self, game: Game, player: Player, money: Money) -> str:
        """
        متن پیام «نوبت بازیکن» را می‌سازد.
        """
        table_cards_str = "🚫 کارتی روی میز نیست" if not getattr(game, "cards_table", None) else " ".join(map(str, game.cards_table))
        return (
            f"🔴 نوبت: {player.mention_markdown} | پات: {game.pot}$\n\n"
            f"🃏 کارت‌های روی میز: {table_cards_str}\n"
            f"💰 پات: {game.pot}$\n"
            f"💵 موجودی شما: {player.money}$\n"
            f"🎲 بت فعلی شما: {player.round_rate}$\n"
            f"📈 سقف این دور: {game.max_round_rate}$\n"
            f"⬇️ حرکت خود را انتخاب کنید:"
        )

        
    def pin_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        """پین‌کردن پیام با کنترل خطا (بدون قطع جریان بازی)."""
        if not message_id:
            return
        try:
            self._bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
        except BadRequest as e:
            err = str(e).lower()
            if "not enough rights" in err or "rights" in err:
                print("[PIN] Bot lacks permission to pin in this chat.")
            elif "message to pin not found" in err or "message_id" in err:
                print(f"[PIN] Message not found to pin (id={message_id}).")
            else:
                print(f"[PIN] BadRequest pinning message: {e}")
        except Unauthorized as e:
            print(f"[PIN] Unauthorized in chat {chat_id}: {e}")
        except Exception as e:
            print(f"[PIN] Unexpected error pinning message: {e}")
    
    def unpin_message(self, chat_id: ChatId, message_id: MessageId = None) -> None:
        """
        آن‌پین پیام. اگر message_id None باشد، طبق رفتار تلگرام آخرین پین را هدف می‌گیرد.
        """
        try:
            if message_id:
                # Bot API فعلاً آن‌پین بر اساس message_id ندارد؛ پس به‌صورت کلی آن‌پین می‌کنیم.
                self._bot.unpin_chat_message(chat_id=chat_id)
            else:
                self._bot.unpin_chat_message(chat_id=chat_id)
        except Exception as e:
            print(f"[TURN] unpin_message error: {e}")

    def ensure_pinned_turn_message(self, chat_id: ChatId, game: Game, player: Player, money: Money) -> Optional[MessageId]:
        """
        اگر پیام نوبت وجود نداشت، یکی می‌سازد و پین می‌کند؛ در غیر این‌صورت همان id را برمی‌گرداند.
        قفل نرم _turn_creating جلوی ساخت همزمان چند پیام را می‌گیرد.
        """
        if getattr(game, "turn_message_id", None):
            return game.turn_message_id
        if getattr(game, "_turn_creating", False):
            return getattr(game, "turn_message_id", None)
    
        game._turn_creating = True
        try:
            text = self._build_turn_text(game, player, money)
    
            call_action = self.define_check_call_action(game, player)
            call_amount = max(0, game.max_round_rate - player.round_rate)
            call_text = call_action.value if call_action.name == "CHECK" else f"{call_action.value} ({call_amount}$)"
            markup = self._get_turns_markup(check_call_text=call_text, check_call_action=call_action)
    
            msg = self._bot.send_message_sync(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            if isinstance(msg, Message):
                game.turn_message_id = msg.message_id
                self.pin_message(chat_id, msg.message_id)  # ← پین قطعی
                return msg.message_id
            return None
        except Exception as e:
            print(f"[TURN] ensure_pinned_turn_message send error: {e}")
            return None
        finally:
            game._turn_creating = False



    def edit_turn_message_text_and_markup(self, chat_id: ChatId, game: Game, player: Player, money: Money) -> None:
        """
        متن و کیبورد پیام نوبتِ پین‌شده را ادیت می‌کند (بدون ساخت پیام جدید).
        """
        if not getattr(game, "turn_message_id", None):
            self.ensure_pinned_turn_message(chat_id, game, player, money)
            return
    
        text = self._build_turn_text(game, player, money)
        call_action = self.define_check_call_action(game, player)
        call_amount = max(0, game.max_round_rate - player.round_rate)
        call_text = call_action.value if call_action.name == "CHECK" else f"{call_action.value} ({call_amount}$)"
        markup = self._get_turns_markup(check_call_text=call_text, check_call_action=call_action)
    
        try:
            self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.turn_message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        except Exception as e:
            print(f"[TURN] edit_turn_message error: {e}")

    def send_message_return_id(
            self,
            chat_id: ChatId,
            text: str,
            reply_markup: ReplyKeyboardMarkup = None,
        ) -> Optional[MessageId]:
        """Sends a message and returns its ID, or None if not applicable.
        ⚠️ از مسیر همزمان استفاده می‌کنیم تا بلافاصله message_id داشته باشیم.
        """
        try:
            message = self._bot.send_message_sync(
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
        parse_mode: str = ParseMode.MARKDOWN,  # <--- پارامتر جدید اضافه شد
    ) -> Optional[MessageId]:
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                parse_mode=parse_mode,  # <--- از پارامتر ورودی استفاده شد
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
    ) -> Optional[MessageId]:
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                return message.message_id
        except Exception as e:
            print(f"Error sending message reply: {e}")
        return None

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

    def send_cards(self, chat_id: ChatId, mention_markdown: str, cards: Cards, ready_message_id: MessageId) -> Optional[MessageId]:
        markup = self._get_cards_markup(cards)
        try:
            message = self._bot.send_message_sync(
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
    def define_check_call_action(self, game: Game, player: Player) -> PlayerAction:
        """
        تعیین می‌کند دکمهٔ چک/کال چه باشد.
        """
        need = game.max_round_rate - player.round_rate
        return PlayerAction.CHECK if need <= 0 else PlayerAction.CALL

    @staticmethod
    def _get_turns_markup(self, check_call_text: str, check_call_action: PlayerAction) -> InlineKeyboardMarkup:
        """
        کیبورد اینلاینِ پیام نوبت را می‌سازد.
        """
        keyboard = [
            [
                InlineKeyboardButton(check_call_text, callback_data=check_call_action.value),
                InlineKeyboardButton(PlayerAction.FOLD.value, callback_data=PlayerAction.FOLD.value),
            ],
            [
                InlineKeyboardButton("⬆️ 10$", callback_data=str(PlayerAction.SMALL.value)),
                InlineKeyboardButton("⬆️ 25$", callback_data=str(PlayerAction.NORMAL.value)),
                InlineKeyboardButton("⬆️ 50$", callback_data=str(PlayerAction.BIG.value)),
            ],
            [
                InlineKeyboardButton(PlayerAction.ALL_IN.value, callback_data=PlayerAction.ALL_IN.value),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    

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
            print(f"[INFO] Cannot remove markup, bot unauthorized in chat {chat_id}: {e}")
        except Exception as e:
            print(f"[ERROR] remove_markup unexpected error: {e}")
    
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
            
        
    def send_showdown_results(self, chat_id: ChatId, game: Game, winners_by_pot: list) -> None:
        """
        پیام نهایی نتایج بازی را با فرمت زیبا ساخته و ارسال می‌کند.
        این نسخه برای مدیریت ساختار داده جدید Side Pot (لیست دیکشنری‌ها) به‌روز شده است.
        """
        final_message = "🏆 *نتایج نهایی و نمایش کارت‌ها*\n\n"

        if not winners_by_pot:
            final_message += "خطایی در تعیین برنده رخ داد. پات تقسیم نشد."
        else:
            # نام‌گذاری پات‌ها برای نمایش بهتر (اصلی، فرعی ۱، فرعی ۲ و...)
            pot_names = ["*پات اصلی*", "*پات فرعی ۱*", "*پات فرعی ۲*", "*پات فرعی ۳*"]
            
            # FIX: حلقه برای پردازش صحیح "لیست دیکشنری‌ها" اصلاح شد
            for i, pot_data in enumerate(winners_by_pot):
                pot_amount = pot_data.get("amount", 0)
                winners_info = pot_data.get("winners", [])

                if pot_amount == 0 or not winners_info:
                    continue
                
                # انتخاب نام پات بر اساس ترتیب آن
                pot_name = pot_names[i] if i < len(pot_names) else f"*پات فرعی {i}*"
                final_message += f"💰 {pot_name}: {pot_amount}$\n"
                
                win_amount_per_player = pot_amount // len(winners_info)

                for winner in winners_info:
                    player = winner.get("player")
                    if not player: continue # اطمینان از وجود بازیکن

                    hand_type = winner.get('hand_type')
                    hand_cards = winner.get('hand_cards', [])
                    
                    hand_name_data = HAND_NAMES_TRANSLATIONS.get(hand_type, {})
                    hand_display_name = f"{hand_name_data.get('emoji', '🃏')} {hand_name_data.get('fa', 'دست نامشخص')}"

                    final_message += (
                        f"  - {player.mention_markdown} با دست {hand_display_name} "
                        f"برنده *{win_amount_per_player}$* شد.\n"
                    )
                    final_message += f"    کارت‌ها: {' '.join(map(str, hand_cards))}\n"
                
                final_message += "\n" # یک خط فاصله برای جداسازی پات‌ها

        final_message += "⎯" * 20 + "\n"
        final_message += f"🃏 *کارت‌های روی میز:* {' '.join(map(str, game.cards_table)) if game.cards_table else '🚫'}\n\n"

        final_message += "🤚 *کارت‌های سایر بازیکنان:*\n"
        all_players_in_hand = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN, PlayerState.FOLD))

        # FIX: استخراج صحیح ID برندگان از ساختار داده جدید
        winner_user_ids = set()
        for pot_data in winners_by_pot:
            for winner_info in pot_data.get("winners", []):
                if "player" in winner_info:
                    winner_user_ids.add(winner_info["player"].user_id)

        for p in all_players_in_hand:
            if p.user_id not in winner_user_ids:
                card_display = ' '.join(map(str, p.cards)) if p.cards else 'کارت‌ها نمایش داده نشد'
                state_info = " (فولد)" if p.state == PlayerState.FOLD else ""
                final_message += f"  - {p.mention_markdown}{state_info}: {card_display}\n"

        self.send_message(chat_id=chat_id, text=final_message, parse_mode="Markdown")

    def send_new_hand_ready_message(self, chat_id: ChatId) -> None:
        """پیام آمادگی برای دست جدید را ارسال می‌کند."""
        message = (
            "♻️ دست به پایان رسید. بازیکنان باقی‌مانده برای دست بعد حفظ شدند.\n"
            "برای شروع دست جدید، /start را بزنید یا بازیکنان جدید می‌توانند با /ready اعلام آمادگی کنند."
        )
        self.send_message(chat_id, message)
