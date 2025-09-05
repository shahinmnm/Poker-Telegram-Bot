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
        self._delete_manager = None
        
    def set_delete_manager(self, manager):
        self._delete_manager = manager
        
    def send_message_return_id(self, chat_id: ChatId, text: str, reply_markup: ReplyKeyboardMarkup = None, game: Game = None) -> Optional[MessageId]:
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                parse_mode=ParseMode.MARKDOWN,
                text=text,
                reply_markup=reply_markup,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                if self._delete_manager and game:
                    self._delete_manager.add_message(game.id, game.id, chat_id, message.message_id, tag="generic_message")
                return message.message_id
        except Exception as e:
            print(f"Error sending message and returning ID: {e}")
        return None

    def send_message(self, chat_id: ChatId, text: str, reply_markup: ReplyKeyboardMarkup = None, parse_mode: str = ParseMode.MARKDOWN, game: Game = None) -> Optional[MessageId]:
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                parse_mode=parse_mode,
                text=text,
                reply_markup=reply_markup,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                if self._delete_manager and game:
                    self._delete_manager.add_message(game.id, game.id, chat_id, message.message_id, tag="plain_message")
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
    ) -> None:
        try:
            self._bot.send_message(
                reply_to_message_id=message_id,
                chat_id=chat_id,
                parse_mode=ParseMode.MARKDOWN,
                text=text,
                disable_notification=True,
            )
        except Exception as e:
            print(f"Error sending message reply: {e}")

    def send_desk_cards_img(self, chat_id: ChatId, cards: Cards, caption: str = "", disable_notification: bool = True, game: Game = None) -> Optional[Message]:
        try:
            im_cards = self._desk_generator.generate_desk(cards)
            bio = BytesIO()
            bio.name = 'desk.png'
            im_cards.save(bio, 'PNG')
            bio.seek(0)
            messages = self._bot.send_media_group(
                chat_id=chat_id,
                media=[InputMediaPhoto(media=bio, caption=caption)],
                disable_notification=disable_notification,
            )
            if messages and isinstance(messages, list) and len(messages) > 0:
                msg = messages[0]
                if self._delete_manager and game:
                    self._delete_manager.add_message(game.id, game.id, chat_id, msg.message_id, tag="desk_cards")
                return msg
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

    def send_cards(self, chat_id: ChatId, cards: Cards, mention_markdown: Mention, ready_message_id: str, game: Game = None) -> Optional[MessageId]:
        markup = self._get_cards_markup(cards)
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                text="کارت‌های شما " + mention_markdown,
                reply_markup=markup,
                reply_to_message_id=ready_message_id,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
            )
            if isinstance(message, Message):
                if self._delete_manager and game:
                    self._delete_manager.add_message(game.id, game.id, chat_id, message.message_id, tag="player_cards")
                return message.message_id
        except Exception as e:
            print(f"Error sending cards: {e}")
        return None

    @staticmethod
    def define_check_call_action(game: Game, player: Player) -> PlayerAction:
        if player.round_rate >= game.max_round_rate:
            return PlayerAction.CHECK
        return PlayerAction.CALL

    def send_turn_actions(self, chat_id: ChatId, game: Game, player: Player, money: Money) -> Optional[MessageId]:
            self,
            chat_id: ChatId,
            game: Game,
            player: Player,
            money: Money,
    ) -> Optional[MessageId]:
        """ارسال پیام نوبت بازیکن با فرمت فارسی/ایموجی و استفاده از delay جدید 0.5s."""
        # نمایش کارت‌های میز
        if not game.cards_table:
            cards_table = "🚫 کارتی روی میز نیست"
        else:
            cards_table = " ".join(game.cards_table)

        # محاسبه CALL یا CHECK
        call_amount = game.max_round_rate - player.round_rate
        call_check_action = self.define_check_call_action(game, player)
        if call_check_action == PlayerAction.CALL:
            call_check_text = f"{call_check_action.value} ({call_amount}$)"
        else:
            call_check_text = call_check_action.value

        text = (
            f"🎯 **نوبت بازی {player.mention_markdown} (صندلی {player.seat_index+1})**\n\n"
            f"🃏 **کارت‌های روی میز:** {cards_table}\n"
            f"💰 **پات فعلی:** `{game.pot}$`\n"
            f"💵 **موجودی شما:** `{money}$`\n"
            f"🎲 **بِت فعلی شما:** `{player.round_rate}$`\n"
            f"📈 **حداکثر شرط این دور:** `{game.max_round_rate}$`\n\n"
            f"⬇️ حرکت خود را انتخاب کنید:"
        )
        markup = self._get_turns_markup(call_check_text, call_check_action)
        try:
            message = self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=False,
            )
            if isinstance(message, Message):
                if self._delete_manager:
                    self._delete_manager.add_message(game.id, game.id, chat_id, message.message_id, tag="turn_action")
                return message.message_id
        except Exception as e:
            print(f"Error sending turn actions: {e}")
        return None

    @staticmethod
    def _get_turns_markup(check_call_text: str, check_call_action: PlayerAction) -> InlineKeyboardMarkup:
        keyboard = [[
            InlineKeyboardButton(text=PlayerAction.FOLD.value, callback_data=PlayerAction.FOLD.value),
            InlineKeyboardButton(text=PlayerAction.ALL_IN.value, callback_data=PlayerAction.ALL_IN.value),
            InlineKeyboardButton(text=check_call_text, callback_data=check_call_action.value),
        ], [
            InlineKeyboardButton(text=str(PlayerAction.SMALL.value), callback_data=str(PlayerAction.SMALL.value)),
            InlineKeyboardButton(text=str(PlayerAction.NORMAL.value), callback_data=str(PlayerAction.NORMAL.value)),
            InlineKeyboardButton(text=str(PlayerAction.BIG.value), callback_data=str(PlayerAction.BIG.value)),
        ]]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)


    from telegram.error import BadRequest, Unauthorized  # اضافه کردن بالای فایل
    
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
            print(f"[INFO] Cannot edit markup, bot unauthorized in chat {chat_id}: {e}")
        except Exception as e:
            print(f"[ERROR] Unexpected error removing markup (ID={message_id}): {e}")
    
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
            
    def remove_message_delayed(self, chat_id: ChatId, message_id: MessageId, delay: float = 3.0) -> None:
        """حذف پیام با تأخیر برحسب ثانیه."""
        if not message_id:
            return

        def _remove():
            try:
                self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                print(f"Could not delete message {message_id} in chat {chat_id}: {e}")

        Timer(delay, _remove).start()
        
    def send_showdown_results(self, chat_id: ChatId, game: Game, winners_by_pot: list) -> None:
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

        msg_id = self.send_message(chat_id, final_message, game=game)
        if self._delete_manager:
            self._delete_manager.add_message(game.id, game.id, chat_id, msg_id, tag="game_results")
            self._delete_manager.whitelist_tag("game_results")
        
    def send_new_hand_ready_message(self, chat_id: ChatId, game: Game) -> None:
        if self._delete_manager:
            self._delete_manager.delete_all_for_hand(game.id, game.id, delay=0.2)
        # ارسال پیام آماده بعدی
        msg_id = self.send_message(chat_id, "♻️ آماده برای دست بعد؟", game=game)
        if self._delete_manager:
            self._delete_manager.add_message(game.id, game.id, chat_id, msg_id, tag="ready_message")

