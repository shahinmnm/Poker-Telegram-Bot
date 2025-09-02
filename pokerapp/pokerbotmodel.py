#!/usr/bin/env python3

import datetime
import traceback
from threading import Timer
from typing import List, Tuple, Dict, Optional

import redis
from telegram import Message, ReplyKeyboardMarkup, Update, Bot, ParseMode
from telegram.ext import Handler, CallbackContext

from pokerapp.config import Config
from pokerapp.privatechatmodel import UserPrivateChatModel
from pokerapp.winnerdetermination import WinnerDetermination, HAND_NAMES_TRANSLATIONS
from pokerapp.cards import Cards
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    ChatId,
    UserId,
    MessageId,  # <--- این خط را اضافه کنید
    UserException,
    Money,
    PlayerAction,
    PlayerState,
    Score,
    Wallet,
    Mention,
    DEFAULT_MONEY,
    SMALL_BLIND,
    MIN_PLAYERS,
    MAX_PLAYERS,
)
from pokerapp.pokerbotview import PokerBotViewer

DICE_MULT = 10
DICE_DELAY_SEC = 5
BONUSES = (5, 20, 40, 80, 160, 320)
DICES = "⚀⚁⚂⚃⚄⚅"

KEY_CHAT_DATA_GAME = "game"
KEY_OLD_PLAYERS = "old_players"

# MAX_PLAYERS = 8 (Defined in entities)
# MIN_PLAYERS = 2 (Defined in entities)
# SMALL_BLIND = 5 (Defined in entities)
# DEFAULT_MONEY = 1000 (Defined in entities)
MAX_TIME_FOR_TURN = datetime.timedelta(minutes=2)
DESCRIPTION_FILE = "assets/description_bot.md"

class PokerBotModel:
    ACTIVE_GAME_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }

    def __init__(self, view: PokerBotViewer, bot: Bot, cfg: Config, kv: redis.Redis):
        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._cfg: Config = cfg
        self._kv = kv
        self._winner_determine: WinnerDetermination = WinnerDetermination()
        self._round_rate = RoundRateModel(view=self._view, kv=self._kv) # Pass kv to RoundRateModel

    @property
    def _min_players(self):
        return 1 if self._cfg.DEBUG else MIN_PLAYERS

    @staticmethod
    def _game_from_context(context: CallbackContext) -> Game:
        if KEY_CHAT_DATA_GAME not in context.chat_data:
            context.chat_data[KEY_CHAT_DATA_GAME] = Game()
        return context.chat_data[KEY_CHAT_DATA_GAME]

    @staticmethod
    def _current_turn_player(game: Game) -> Optional[Player]:
        if not game.players or game.current_player_index < 0:
            return None
        # Add boundary check to prevent IndexError
        if game.current_player_index >= len(game.players):
            return None
        return game.players[game.current_player_index]
    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
        """کیبورد مخصوص نمایش کارت‌های بازیکن و دکمه‌های کنترلی را می‌سازد."""
        # این دکمه‌ها برای مدیریت کیبورد توسط بازیکن استفاده می‌شوند
        hide_cards_button_text = "🙈 پنهان کردن کارت‌ها"
        show_table_button_text = "👁️ نمایش میز" # این دکمه را هم اضافه می‌کنیم
        return ReplyKeyboardMarkup(
            keyboard=[
                cards, # <-- ردیف اول: خود کارت‌ها
                [hide_cards_button_text, show_table_button_text]
            ],
            selective=True,  # <-- کیبورد فقط برای بازیکن مورد نظر نمایش داده می‌شود
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    def show_reopen_keyboard(self, chat_id: ChatId, player_mention: Mention) -> None:
        """کیبورد جایگزین را بعد از پنهان کردن کارت‌ها نمایش می‌دهد."""
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
            text=f"کارت‌های {player_mention} پنهان شد. برای مشاهده دوباره، از دکمه زیر استفاده کن.",
            reply_markup=reopen_keyboard,
        )

    def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: MessageId,
    ) -> Optional[MessageId]:
        """
        یک پیام در گروه با کیبورد حاوی کارت‌های بازیکن ارسال می‌کند و به پیام /ready ریپلای می‌زند.
        """
        markup = self._get_cards_markup(cards)
        try:
            # اینجا ما به جای محتوای کارت‌ها، یک متن عمومی می‌فرستیم
            # و خود کارت‌ها را در کیبورد ReplyKeyboardMarkup قرار می‌دهیم.
            message = self._bot.send_message(
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
            # اگر ریپلای شکست خورد (پیام /ready حذف شده)، بدون ریپلای تلاش می‌کنیم
            if 'message to be replied not found' in str(e).lower():
                print(f"INFO: ready_message_id {ready_message_id} not found. Sending cards without reply.")
                try:
                    message = self._bot.send_message(
                        chat_id=chat_id,
                        text="کارت‌های شما " + mention_markdown,
                        reply_markup=markup,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_notification=True,
                    )
                    if isinstance(message, Message):
                        return message.message_id
                except Exception as inner_e:
                     print(f"Error sending cards (second attempt): {inner_e}")
            else:
                 print(f"Error sending cards: {e}")
        return None
    def hide_cards(self, update: Update, context: CallbackContext) -> None:
        """
        کیبورد کارتی را پنهان کرده و کیبورد "نمایش مجدد" را نشان می‌دهد.
        """
        chat_id = update.effective_chat.id
        user = update.effective_user
        self._view.show_reopen_keyboard(chat_id, user.mention_markdown())
        # پیام "کارت‌ها پنهان شد" را پس از چند ثانیه حذف می‌کنیم تا چت شلوغ نشود.
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=5)


    def send_cards_to_user(self, update: Update, context: CallbackContext) -> None:
        """
        کارت‌های بازیکن را با کیبورد مخصوص در گروه دوباره ارسال می‌کند.
        این متد زمانی فراخوانی می‌شود که بازیکن دکمه "نمایش کارت‌ها" را می‌زند.
        """
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # پیدا کردن بازیکن در لیست بازیکنان بازی فعلی
        current_player = None
        for p in game.players:
            if p.user_id == user_id:
                current_player = p
                break
        
        if not current_player or not current_player.cards:
            self._view.send_message(chat_id, "شما در بازی فعلی حضور ندارید یا کارتی ندارید.")
            return

        # ارسال پیام با کیبورد کارتی
        # اینجا دیگر نیازی به ریپلای نیست.
        cards_message_id = self._view.send_cards(
            chat_id=chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=None, # <-- چون این یک نمایش مجدد است، ریپلای نمی‌زنیم.
        )
        if cards_message_id:
            game.message_ids_to_delete.append(cards_message_id)
        
        # حذف پیام "/نمایش کارت‌ها" که بازیکن فرستاده
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)


    def show_table(self, update: Update, context: CallbackContext):
        """کارت‌های روی میز را به درخواست بازیکن نمایش می‌دهد."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        if game.state in self.ACTIVE_GAME_STATES:
            self.add_cards_to_table(0, game, chat_id) # فراخوانی با count=0 فقط میز را نمایش می‌دهد
        else:
            self._view.send_message(chat_id, "هنوز بازی شروع نشده است.")
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)

    def ready(self, update: Update, context: CallbackContext) -> None:
        """بازیکن برای شروع بازی اعلام آمادگی می‌کند."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user = update.effective_message.from_user

        if game.state != GameState.INITIAL:
            self._view.send_message_reply(chat_id, update.message.message_id, "⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return

        if len(game.players) >= MAX_PLAYERS:
            self._view.send_message_reply(chat_id, update.message.message_id, "🚪 اتاق پر است!")
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < SMALL_BLIND * 2:
            self._view.send_message_reply(chat_id, update.message.message_id, f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).")
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.effective_message.message_id, # <-- کد صحیح
            )
            game.ready_users.add(user.id)
            game.players.append(player)

        ready_list = "\n".join([f"{i+1}. {p.mention_markdown} 🟢" for i, p in enumerate(game.players)])
        text = (
            f"👥 *لیست بازیکنان آماده*\n\n{ready_list}\n\n"
            f"📊 {len(game.players)}/{MAX_PLAYERS} بازیکن آماده\n\n"
            f"🚀 برای شروع بازی /start را بزنید یا منتظر بمانید."
        )

        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)

        if game.ready_message_main_id:
            try:
                self._bot.edit_message_text(chat_id=chat_id, message_id=game.ready_message_main_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception: # اگر ویرایش نشد، یک پیام جدید بفرست
                msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
                if msg: game.ready_message_main_id = msg
        else:
            msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
            if msg: game.ready_message_main_id = msg

        # بررسی برای شروع خودکار
        if len(game.players) >= self._min_players and (len(game.players) == self._bot.get_chat_member_count(chat_id) - 1 or self._cfg.DEBUG):
            self._start_game(context, game, chat_id)

    def start(self, update: Update, context: CallbackContext) -> None:
        """بازی را به صورت دستی شروع می‌کند."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            self._view.send_message(chat_id, "🎮 یک بازی در حال حاضر در جریان است.")
            return

        if game.state == GameState.FINISHED:
            game.reset()
            # بازیکنان قبلی را برای دور جدید نگه دار
            old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
            # Re-add players logic would go here if needed.
            # For now, just resetting allows new players to join.

        if len(game.players) >= self._min_players:
            self._start_game(context, game, chat_id)
        else:
            self._view.send_message(chat_id, f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).")

    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""
        if game.ready_message_main_id:
            self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None

        # Ensure dealer_index is initialized before use
        if not hasattr(game, 'dealer_index'):
             game.dealer_index = -1
        game.dealer_index = (game.dealer_index + 1) % len(game.players)

        self._view.send_message(chat_id, '🚀 !بازی شروع شد!')

        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game, chat_id)

        self._round_rate.set_blinds(game, chat_id)

        # Start player is the one after the big blind.
        # Handle 2-player game case where dealer=SB is first to act.
        if len(game.players) == 2:
            start_player_index = game.dealer_index
        else:
            start_player_index = (game.dealer_index + 3) % len(game.players)

        game.current_player_index = self._find_next_active_player_index(game, start_player_index)
        
        # In case the first player couldn't be found (e.g. all-in on blinds) start from beginning
        if game.current_player_index == -1:
             game.current_player_index = self._find_next_active_player_index(game, 0)

        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]
        self._process_playing(chat_id, game)


    def _divide_cards(self, game: Game, chat_id: ChatId):
        """
        کارت‌ها را بین بازیکنان پخش می‌کند:
        ۱. کارت‌ها را در PV بازیکن ارسال می‌کند.
        ۲. یک پیام در گروه با کیبورد حاوی کارت‌های بازیکن ارسال می‌کند.
        """
        for player in game.players:
            if len(game.remain_cards) < 2:
                self._view.send_message(chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود.")
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

            # --- شروع بلوک اصلاح شده ---

            # ۱. ارسال کارت‌ها به چت خصوصی (برای سابقه و دسترسی آسان)
            try:
                self._view.send_desk_cards_img(
                    chat_id=player.user_id,
                    cards=cards,
                    caption="🃏 کارت‌های شما برای این دست."
                )
            except Exception as e:
                print(f"WARNING: Could not send cards to private chat for user {player.user_id}. Error: {e}")
                self._view.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {player.mention_markdown}، نتوانستم کارت‌ها را در PV ارسال کنم. لطفاً ربات را استارت کن (/start).",
                    parse_mode="Markdown"
                )

            # ۲. ارسال پیام با کیبورد کارتی در گروه
            # این پیام برای دسترسی سریع بازیکن به کارت‌هایش است.
            cards_message_id = self._view.send_cards(
                chat_id=chat_id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )

            # این پیام موقتی است و در آخر دست پاک خواهد شد.
            if cards_message_id:
                game.message_ids_to_delete.append(cards_message_id)
            
            # --- پایان بلوک اصلاح شده ---

    def _process_playing(self, chat_id: ChatId, game: Game):
        """
        حلقه اصلی بازی: وضعیت را چک می‌کند، اگر دور تمام شده به مرحله بعد می‌رود،
        در غیر این صورت نوبت را به بازیکن بعدی می‌دهد.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            self._go_to_next_street(game, chat_id)
            return

        active_players = game.players_by(states=(PlayerState.ACTIVE,))

        # The round ends if no active players are left, or if all active players have acted
        # and their current round bets are equal.
        all_acted = all(p.has_acted for p in active_players)
        rates_equal = len(set(p.round_rate for p in active_players)) <= 1

        # Exception for Big Blind pre-flop: they can still act if no one raised.
        is_preflop_bb_option = (game.state == GameState.ROUND_PRE_FLOP and
                                game.max_round_rate == SMALL_BLIND * 2 and
                                not all_acted)

        if not active_players or (all_acted and rates_equal and not is_preflop_bb_option):
            self._go_to_next_street(game, chat_id)
            return

        player = self._current_turn_player(game)
        if player and player.state == PlayerState.ACTIVE:
            # FIX 1 (PART 2): Call _send_turn_message without the 'money' argument.
            self._send_turn_message(game, player, chat_id)
        else:
            # If current player is not active, move to the next one.
            self._move_to_next_player_and_process(game, chat_id)

    # FIX 1 (PART 1): Remove the 'money' parameter. The function will fetch the latest wallet value itself.
    def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف در آینده ذخیره می‌کند."""
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)

        # Fetch the most current wallet value right here, ensuring it's up-to-date.
        money = player.wallet.value()
        
        msg_id = self._view.send_turn_actions(chat_id, game, player, money)
        
        if msg_id:
            game.turn_message_id = msg_id
        game.last_turn_time = datetime.datetime.now()
        
    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        """از ایندکس مشخص شده، به دنبال بازیکن بعدی که FOLD یا ALL_IN نکرده می‌گردد."""
        num_players = len(game.players)
        for i in range(1, num_players + 1):
            next_index = (start_index + i) % num_players
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index
        return -1 # هیچ بازیکن فعالی یافت نشد

    def _move_to_next_player_and_process(self, game: Game, chat_id: ChatId):
        """
        ایندکس بازیکن را به نفر فعال بعدی منتقل کرده و حلقه بازی را ادامه می‌دهد.
        این متد، مشکل حلقه بی‌نهایت را حل می‌کند.
        """
        next_player_index = self._find_next_active_player_index(
            game, game.current_player_index
        )
        if next_player_index == -1: # اگر بازیکن فعال دیگری نمانده
            # مستقیم به مرحله بعد برو، چون شرط‌بندی تمام است
            self._go_to_next_street(game, chat_id)
        else:
            game.current_player_index = next_player_index
            self._process_playing(chat_id, game)
            
    def _go_to_next_street(self, game: Game, chat_id: ChatId):
        """بازی را به مرحله بعدی (Flop, Turn, River) یا به پایان (Finish) می‌برد."""
        self._round_rate.collect_bets_for_pot(game)

        # Reset has_acted for all players for the new betting round
        for p in game.players:
            # Don't reset for FOLD players, keep their state
            if p.state != PlayerState.FOLD:
                p.has_acted = False

        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            self.add_cards_to_table(3, game, chat_id, "فلاپ (Flop)")
            self._process_playing(chat_id, game)
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            self.add_cards_to_table(1, game, chat_id, "تِرن (Turn)")
            self._process_playing(chat_id, game)
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            self.add_cards_to_table(1, game, chat_id, "ریوِر (River)")
            self._process_playing(chat_id, game)
        else: # اگر در River بودیم یا شرایط پایان بازی برقرار بود
            self._finish(game, chat_id)
            return

    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, street_name: str):
        """Adds cards to the table and announces the new street."""
        if len(game.remain_cards) < count:
            self._view.send_message(chat_id, f"کارت کافی برای {street_name} نیست. بازی تمام می‌شود.")
            self._finish(game, chat_id)
            return
            
        new_cards = [game.remain_cards.pop() for _ in range(count)]
        game.cards_table.extend(new_cards)
        
        cards_str = " ".join(game.cards_table)
        self._view.send_message(chat_id, f"--- {street_name}: {cards_str} ---")

    def player_action_fold(self, update: Update, context: CallbackContext, game: Game) -> None:
        player = self._current_turn_player(game)
        if not player: return
        self._round_rate.player_action_fold(game, player, update.effective_chat.id)
        player.has_acted = True
        self._move_to_next_player_and_process(game, update.effective_chat.id)

    def player_action_call_check(self, update: Update, context: CallbackContext, game: Game) -> None:
        player = self._current_turn_player(game)
        if not player: return
        self._round_rate.player_action_call_check(game, player, update.effective_chat.id)
        player.has_acted = True
        self._move_to_next_player_and_process(game, update.effective_chat.id)

    def player_action_all_in(self, update: Update, context: CallbackContext, game: Game) -> None:
        player = self._current_turn_player(game)
        if not player: return
        self._round_rate.player_action_all_in(game, player, update.effective_chat.id)
        player.has_acted = True
        self._move_to_next_player_and_process(game, update.effective_chat.id)

    def player_action_raise_bet(self, update: Update, context: CallbackContext, game: Game, amount: int) -> None:
        player = self._current_turn_player(game)
        if not player: return
        try:
            self._round_rate.player_action_raise_bet(game, player, amount, update.effective_chat.id)
            player.has_acted = True
            # When someone raises, the action is on other players again.
            # Reset `has_acted` for all other active players.
            for p in game.players:
                if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                    p.has_acted = False
            self._move_to_next_player_and_process(game, update.effective_chat.id)
        except UserException as e:
            # Answer callback query to show the error message to the user
            context.bot.answer_callback_query(callback_query_id=update.callback_query.id, text=str(e), show_alert=True)
    
    def _finish(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        """پایان دادن به دست، محاسبه برندگان و ارسال پیام نتایج حرفه‌ای."""
        game.state = GameState.FINISHED
        self._view.remove_message(chat_id, game.turn_message_id)

        # 1. شناسایی بازیکنانی که باید دستشان رو شود (Showdown)
        # اگر فقط یک نفر باقی مانده (بقیه فولد کرده‌اند)، showdown نداریم.
        active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        showdown_players = []
        final_text = "🏁 *پایان دست!* 🏁\n\n"

        if len(active_players) == 1:
            # حالت حرفه‌ای: همه فولد کرده‌اند
            winner = active_players[0]
            winner.wallet.inc(game.pot)
            final_text += f"👤 برنده نهایی:\n{winner.mention_markdown}\n\n"
            final_text += f"💰 **مبلغ برد: {game.pot}$**\n"
            final_text += "_همه بازیکنان دیگر فولد کردند._"
        else:
            # حالت Showdown: چند بازیکن باقی مانده‌اند
            showdown_players = active_players
            winnings = self._calculate_winnings(game, showdown_players)
            
            # مرتب‌سازی برندگان بر اساس مبلغ برد
            sorted_winners = sorted(winnings.items(), key=lambda item: item[1][0], reverse=True)

            final_text += f"💳 **کارت‌های روی میز:**\n`{' '.join(game.cards_table)}`\n\n"
            final_text += "🏆 **نتایج و برندگان:**\n"
            final_text += "--------------------\n"

            for player, (amount, score, hand) in sorted_winners:
                hand_rank = score // HAND_RANK_MULTIPLIER
                hand_info = HAND_NAMES_TRANSLATIONS.get(HandsOfPoker(hand_rank), {"fa": "نامشخص", "en": "Unknown", "emoji": "❓"})
                
                # جدا کردن نام بازیکن برای جلوگیری از به هم ریختگی متن
                final_text += f"👤 بازیکن:\n{player.mention_markdown}\n"
                
                # نمایش دست برتر (۵ کارت) و هایلایت کردن کارت‌های بازیکن
                hand_str_list = []
                player_card_set = set(player.cards)
                for card in hand:
                    if card in player_card_set:
                        hand_str_list.append(f"({card})") # کارت بازیکن در پرانتز
                    else:
                        hand_str_list.append(str(card))
                
                hand_display = ' '.join(hand_str_list)

                final_text += f"{hand_info['emoji']} **{hand_info['fa']}** `({hand_info['en']})`\n"
                final_text += f"   🃏 دست: `{hand_display}`\n"
                final_text += f"   💰 برد: **{amount}$**\n"
                final_text += "--------------------\n"

        final_text += f"\n💰 **موجودی نهایی پات: {game.pot}$**"
        
        # ارسال پیام نهایی
        self._view.send_message(
            chat_id=chat_id,
            text=final_text,
            parse_mode="Markdown"
        )
        
        # آماده‌سازی برای دست بعدی
        Timer(15, self._prepare_new_round, args=(context, chat_id)).start()

    def _calculate_winnings(self, game: Game, showdown_players: List[Player]) -> Dict[Player, Tuple[Money, Score, Tuple]]:
        """
        محاسبه برندگان، مبلغ برد، امتیاز و دست برتر هر بازیکن.
        این متد پیچیدگی ساید-پات (side-pot) را نیز مدیریت می‌کند.
        """
        # اگر بازیکنی برای نمایش دست وجود ندارد (مثلاً همه فولد کردند)، محاسبات را رد کن
        if not showdown_players:
            return {}

        # 1. محاسبه امتیاز و دست برتر برای هر بازیکن در showdown
        player_scores = {}
        for player in showdown_players:
            score, best_hand = self._winner_determine.get_hand_value(player.cards, game.cards_table)
            player_scores[player.user_id] = {"score": score, "hand": best_hand, "player": player}
        
        # 2. مرتب‌سازی بازیکنان بر اساس امتیاز (از بیشترین به کمترین)
        sorted_players = sorted(player_scores.values(), key=lambda x: x['score'], reverse=True)

        # 3. محاسبه پات‌ها (اصلی و جانبی)
        all_bets = sorted([p.total_bet for p in game.players if p.total_bet > 0])
        unique_bet_levels = sorted(list(set(all_bets)))
        
        pots = []
        last_level = 0
        for level in unique_bet_levels:
            pot_amount = 0
            eligible_player_ids = []
            
            # محاسبه سهم هر بازیکن در این pot
            for player in game.players:
                contribution = min(player.total_bet, level) - last_level
                if contribution > 0:
                    pot_amount += contribution
            
            # شناسایی بازیکنانی که در این pot شریک هستند
            for p in showdown_players:
                if p.total_bet >= level:
                    eligible_player_ids.append(p.user_id)

            if pot_amount > 0:
                pots.append({"amount": pot_amount, "eligible_ids": eligible_player_ids})
            last_level = level

        # 4. توزیع پات‌ها بین برندگان
        winnings = {} # {player: (total_win, score, hand)}

        for pot in pots:
            best_score_in_pot = 0
            winners_in_pot = []
            
            # پیدا کردن بهترین دست(ها) در بین بازیکنان مجاز این pot
            for p_data in sorted_players:
                if p_data['player'].user_id in pot['eligible_ids']:
                    if not winners_in_pot or p_data['score'] == best_score_in_pot:
                        best_score_in_pot = p_data['score']
                        winners_in_pot.append(p_data)
                    elif p_data['score'] < best_score_in_pot:
                        break # چون بازیکنان مرتب هستند، نیازی به ادامه نیست
            
            if not winners_in_pot:
                continue

            # تقسیم پول pot بین برندگان
            win_share = pot['amount'] // len(winners_in_pot)
            remainder = pot['amount'] % len(winners_in_pot)

            for i, winner_data in enumerate(winners_in_pot):
                payout = win_share + (1 if i < remainder else 0)
                player = winner_data['player']
                
                if player not in winnings:
                    winnings[player] = [0, winner_data['score'], winner_data['hand']]
                
                winnings[player][0] += payout
                player.wallet.inc(payout)

        # تبدیل لیست به تاپل برای خروجی نهایی
        final_winnings = {p: (money, score, hand) for p, (money, score, hand) in winnings.items()}
        return final_winnings

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            # Replacing underscore with space and title-casing the output
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"
class RoundRateModel:
    def __init__(self, view: PokerBotViewer, kv: redis.Redis):
        self._view = view
        self._kv = kv # Storing kv for wallet interactions

    def set_blinds(self, game: Game, chat_id: ChatId):
        """Sets small and big blinds for the players."""
        num_players = len(game.players)
        if num_players < 2: return

        # In 2-player games (heads-up), dealer is SB and acts first pre-flop.
        if num_players == 2:
            sb_player_index = game.dealer_index
            bb_player_index = (game.dealer_index + 1) % num_players
        else:
            sb_player_index = (game.dealer_index + 1) % num_players
            bb_player_index = (game.dealer_index + 2) % num_players

        sb_player = game.players[sb_player_index]
        bb_player = game.players[bb_player_index]

        # Small Blind
        try:
            sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
            sb_player.wallet.authorize(game.id, sb_amount)
            sb_player.round_rate += sb_amount
            sb_player.total_bet += sb_amount
            game.pot += sb_amount
            self._view.send_message(chat_id, f"👤 {sb_player.mention_markdown} بلایند کوچک ({sb_amount}$) را گذاشت.")
        except UserException as e:
            self._view.send_message(chat_id, f"⚠️ {sb_player.mention_markdown} موجودی کافی برای بلایند کوچک ندارد و فولد می‌شود.")
            sb_player.state = PlayerState.FOLD

        # Big Blind
        try:
            bb_amount = min(SMALL_BLIND * 2, bb_player.wallet.value())
            bb_player.wallet.authorize(game.id, bb_amount)
            bb_player.round_rate += bb_amount
            bb_player.total_bet += bb_amount
            game.pot += bb_amount
            game.max_round_rate = bb_amount
            self._view.send_message(chat_id, f"👤 {bb_player.mention_markdown} بلایند بزرگ ({bb_amount}$) را گذاشت.")
        except UserException as e:
            self._view.send_message(chat_id, f"⚠️ {bb_player.mention_markdown} موجودی کافی برای بلایند بزرگ ندارد و فولد می‌شود.")
            bb_player.state = PlayerState.FOLD

    def player_action_fold(self, game: Game, player: Player, chat_id: ChatId):
        player.state = PlayerState.FOLD
        self._view.send_message(chat_id, f"🏳️ {player.mention_markdown} فولد کرد.")
        # Player's money is already authorized. It will be handled at the end of the hand.

    def player_action_call_check(self, game: Game, player: Player, chat_id: ChatId):
        amount_to_call = game.max_round_rate - player.round_rate
        if amount_to_call > 0:
            # This is a Call
            actual_call = min(amount_to_call, player.wallet.value())
            player.wallet.authorize(game.id, actual_call)
            player.round_rate += actual_call
            player.total_bet += actual_call
            game.pot += actual_call
            self._view.send_message(chat_id, f"🎯 {player.mention_markdown} کال کرد ({actual_call}$).")
            if actual_call < amount_to_call:
                player.state = PlayerState.ALL_IN
                self._view.send_message(chat_id, f"🀄 {player.mention_markdown} با کال کردن آل-این شد.")
        else:
            # This is a Check
            self._view.send_message(chat_id, f"✋ {player.mention_markdown} چک کرد.")
    
    def player_action_all_in(self, game: Game, player: Player, chat_id: ChatId):
        all_in_amount = player.wallet.value()
        player.wallet.authorize(game.id, all_in_amount)
        
        # Add to pot and update player/game state
        game.pot += all_in_amount
        player.round_rate += all_in_amount
        player.total_bet += all_in_amount
        player.state = PlayerState.ALL_IN
        
        # Update max round rate if this all-in is a raise
        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate

        self._view.send_message(chat_id, f"🀄 {player.mention_markdown} آل-این کرد (مبلغ کل: {player.round_rate}$).")

    def player_action_raise_bet(self, game: Game, player: Player, amount: int, chat_id: ChatId):
        # amount is the total new bet amount (e.g., raise to 50)
        current_bet = player.round_rate
        raise_amount = amount - current_bet # The additional money needed

        if raise_amount <= 0:
            raise UserException("مقدار رِیز باید از شرط فعلی شما بیشتر باشد.")
        
        if amount < game.max_round_rate * 2 and game.max_round_rate > 0:
            raise UserException(f"حداقل رِیز باید دو برابر آخرین شرط باشد ({game.max_round_rate * 2}$).")

        player.wallet.authorize(game.id, raise_amount)
        
        # --- FIX 2: Correctly update the pot ---
        # Instead of adding the full `amount`, add only the `raise_amount`.
        game.pot += raise_amount
        # ----------------------------------------
        
        player.round_rate = amount
        player.total_bet += raise_amount
        game.max_round_rate = amount
        self._view.send_message(chat_id, f"💹 {player.mention_markdown} شرط را به {amount}$ افزایش داد.")

    def collect_bets_for_pot(self, game: Game):
        # This function resets the round-specific bets for the next street.
        # The money is already in the pot.
        for player in game.players:
            player.round_rate = 0
        game.max_round_rate = 0
class WalletManagerModel(Wallet):
    """
    این کلاس مسئولیت مدیریت موجودی (Wallet) هر بازیکن را با استفاده از Redis بر عهده دارد.
    این کلاس به صورت اتمی (atomic) کار می‌کند تا از مشکلات همزمانی (race condition) جلوگیری کند.
    """
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv: redis.Redis = kv
        self._val_key = f"u_m:{user_id}"
        self._daily_bonus_key = f"u_db:{user_id}"
        self._authorized_money_key = f"u_am:{user_id}" # برای پول رزرو شده در بازی

        # اسکریپت Lua برای کاهش اتمی موجودی (جلوگیری از race condition)
        # این اسکریپت ابتدا مقدار فعلی را می‌گیرد، اگر کافی بود کم می‌کند و مقدار جدید را برمیگرداند
        # در غیر این صورت -1 را برمیگرداند.
        self._LUA_DECR_IF_GE = self._kv.register_script("""
            local current = tonumber(redis.call('GET', KEYS[1]))
            if current == nil then
                redis.call('SET', KEYS[1], ARGV[2])
                current = tonumber(ARGV[2])
            end
            local amount = tonumber(ARGV[1])
            if current >= amount then
                return redis.call('DECRBY', KEYS[1], amount)
            else
                return -1
            end
        """)

    def value(self) -> Money:
        """موجودی فعلی بازیکن را برمی‌گرداند. اگر بازیکن وجود نداشته باشد، با مقدار پیش‌فرض ایجاد می‌شود."""
        val = self._kv.get(self._val_key)
        if val is None:
            self._kv.set(self._val_key, DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(val)

    def inc(self, amount: Money = 0) -> Money:
        """موجودی بازیکن را به مقدار مشخص شده افزایش می‌دهد."""
        return self._kv.incrby(self._val_key, amount)

    def dec(self, amount: Money) -> Money:
        """
        موجودی بازیکن را به مقدار مشخص شده کاهش می‌دهد، تنها اگر موجودی کافی باشد.
        این عملیات به صورت اتمی با استفاده از اسکریپت Lua انجام می‌شود.
        """
        if amount < 0:
            raise ValueError("Amount to decrease cannot be negative.")
        if amount == 0:
            return self.value()

        result = self._LUA_DECR_IF_GE(keys=[self._val_key], args=[amount, DEFAULT_MONEY])
        if result == -1:
            raise UserException("موجودی شما کافی نیست.")
        return int(result)

    def has_daily_bonus(self) -> bool:
        """چک می‌کند آیا بازیکن پاداش روزانه خود را دریافت کرده است یا خیر."""
        return self._kv.exists(self._daily_bonus_key) > 0

    def add_daily(self, amount: Money) -> Money:
        """پاداش روزانه را به بازیکن می‌دهد و زمان آن را تا روز بعد ثبت می‌کند."""
        if self.has_daily_bonus():
            raise UserException("شما قبلاً پاداش روزانه خود را دریافت کرده‌اید.")

        now = datetime.datetime.now()
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        ttl = int((tomorrow - now).total_seconds())

        self._kv.setex(self._daily_bonus_key, ttl, "1")
        return self.inc(amount)

    # --- متدهای مربوط به تراکنش‌های بازی (برای تطابق با Wallet ABC) ---
    def authorize(self, game_id: str, amount: Money) -> None:
        """مبلغی از پول بازیکن را برای یک بازی خاص رزرو (dec) می‌کند."""
        # در این پیاده‌سازی، ما مستقیماً پول را کم می‌کنیم.
        # متد dec خودش در صورت کمبود موجودی، خطا می‌دهد.
        self.dec(amount)
        self._kv.hincrby(self._authorized_money_key, game_id, amount)

    def approve(self, game_id: str) -> None:
        """تراکنش موفق یک بازی را تایید می‌کند (پول خرج شده و نیاز به بازگشت نیست)."""
        # پول قبلاً در authorize/dec کم شده است، فقط مبلغ رزرو شده را پاک می‌کنیم.
        self._kv.hdel(self._authorized_money_key, game_id)

    def cancel(self, game_id: str) -> None:
        """تراکنش ناموفق را لغو و پول رزرو شده را به بازیکن برمی‌گرداند."""
        # مبلغی که برای این بازی رزرو شده بود را به کیف پول برمی‌گردانیم.
        # hget returns bytes, so convert to int. Default to 0 if key doesn't exist.
        amount_to_return_bytes = self._kv.hget(self._authorized_money_key, game_id)
        if amount_to_return_bytes:
            amount_to_return = int(amount_to_return_bytes)
            if amount_to_return > 0:
                self.inc(amount_to_return)
                self._kv.hdel(self._authorized_money_key, game_id)
