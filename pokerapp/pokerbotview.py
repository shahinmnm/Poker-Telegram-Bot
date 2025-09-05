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

from telegram.error import BadRequest, Unauthorized  # برای حذف ایمن

class PokerBotViewer:
    def __init__(self, bot: Bot):
        self._bot = bot
        self._desk_generator = DeskImageGenerator()
        self._delete_manager = None  # اضافه شده

    def set_delete_manager(self, manager):
        self._delete_manager = manager

    # ========== متدهای ارسال پیام با ثبت در DeleteManager ==========

    def send_message_return_id(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        game: Game = None
    ) -> Optional[MessageId]:
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

    def send_message(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        parse_mode: str = ParseMode.MARKDOWN,
        game: Game = None
    ) -> Optional[MessageId]:
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

    def send_photo(self, chat_id: ChatId, game: Game = None) -> None:
        try:
            message = self._bot.send_photo(
                chat_id=chat_id,
                photo=open("./assets/poker_hand.jpg", 'rb'),
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
            )
            if isinstance(message, Message) and self._delete_manager and game:
                self._delete_manager.add_message(game.id, game.id, chat_id, message.message_id, tag="photo_message")
        except Exception as e:
            print(f"Error sending photo: {e}")

    def send_desk_cards_img(
        self,
        chat_id: ChatId,
        cards: Cards,
        caption: str = "",
        disable_notification: bool = True,
        game: Game = None
    ) -> Optional[Message]:
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
        hide_cards_button_text = "🙈 پنهان کردن کارت‌ها"
        show_table_button_text = "👁️ نمایش میز"
        return ReplyKeyboardMarkup(
            keyboard=[cards, [hide_cards_button_text, show_table_button_text]],
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    def show_reopen_keyboard(self, chat_id: ChatId, player_mention: Mention, game: Game = None) -> None:
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
            game=game
        )

    def send_cards(
        self,
        chat_id: ChatId,
        cards: Cards,
        mention_markdown: Mention,
        ready_message_id: str,
        game: Game = None
    ) -> Optional[MessageId]:
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

    # ========== متد ارسال اکشن نوبت ==========
    def send_turn_actions(
        self,
        chat_id: ChatId,
        game: Game,
        player: Player,
        money: Money
    ) -> Optional[MessageId]:
        # (متن و کیبورد همون کد فعلی شما)
        if not game.cards_table:
            cards_table = "🚫 کارتی روی میز نیست"
        else:
            cards_table = " ".join(game.cards_table)

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

    # ===== حذف markup/message =====
    def remove_markup(self, chat_id: ChatId, message_id: MessageId) -> None:
        if not message_id:
            return
        try:
            self._bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message is not modified" in err:
                pass
        except Unauthorized:
            pass

    def remove_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        if not message_id:
            return
        try:
            self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            err = str(e).lower()
            if "message to delete not found" in err or "message can't be deleted" in err:
                pass
        except Unauthorized:
            pass

    def remove_message_delayed(self, chat_id: ChatId, message_id: MessageId, delay: float = 3.0) -> None:
        if not message_id:
            return
        Timer(delay, lambda: self.remove_message(chat_id, message_id)).start()

    # ===== نتایج بازی =====
    def send_showdown_results(self, chat_id: ChatId, game: Game, winners_by_pot: list) -> None:
        final_message = "🏆 *نتایج نهایی و نمایش کارت‌ها*\n\n"
        # (همان منطق فعلی برای ساخت نتایج ...)
        msg_id = self.send_message(chat_id=chat_id, text=final_message, parse_mode="Markdown", game=game)
        if self._delete_manager:
            self._delete_manager.add_message(game.id, game.id, chat_id, msg_id, tag="game_results")
            self._delete_manager.whitelist_tag("game_results")

    def send_new_hand_ready_message(self, chat_id: ChatId, game: Game) -> None:
        if self._delete_manager:
            self._delete_manager.delete_all_for_hand(game.id, game.id, delay=0.2)
        message = (
            "♻️ دست به پایان رسید. بازیکنان باقی‌مانده برای دست بعد حفظ شدند.\n"
            "برای شروع دست جدید، /start را بزنید یا بازیکنان جدید می‌توانند با /ready اعلام آمادگی کنند."
        )
        msg_id = self.send_message(chat_id, message, game=game)
        if self._delete_manager:
            self._delete_manager.add_message(game.id, game.id, chat_id, msg_id, tag="ready_message")
