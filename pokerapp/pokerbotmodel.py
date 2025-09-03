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
from pokerapp.winnerdetermination import WinnerDetermination, HAND_NAMES_TRANSLATIONS, HandsOfPoker
from pokerapp.cards import Card, Cards
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    ChatId,
    UserId,
    MessageId,
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
    MessageLifespan,
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
        self._round_rate = RoundRateModel(view=self._view, kv=self._kv, model=self)
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
        """کارت‌های روی میز را به درخواست بازیکن با فرمت جدید نمایش می‌دهد."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        # پیام درخواست بازیکن را حذف می‌کنیم تا چت تمیز بماند
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)

        if game.state in self.ACTIVE_GAME_STATES and game.cards_table:
            # از متد اصلاح‌شده برای نمایش میز استفاده می‌کنیم
            # با count=0 و یک عنوان عمومی و زیبا
            self.add_cards_to_table(0, game, chat_id, "🃏 کارت‌های روی میز")
        else:
            msg_id = self._view.send_message_return_id(chat_id, "هنوز بازی شروع نشده یا کارتی روی میز نیست.")
            if msg_id:
                self._view.remove_message_delayed(chat_id, msg_id, 5)

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
    
        # این متد به تنهایی تمام کارهای لازم برای شروع راند را انجام می‌دهد.
        # از جمله تعیین بلایندها، تعیین نوبت اول و ارسال پیام نوبت.
        self._round_rate.set_blinds(game, chat_id)
    
        # نیازی به هیچ کد دیگری در اینجا نیست.
        # کدهای اضافی حذف شدند.
        
        # ذخیره بازیکنان برای دست بعدی (این خط می‌تواند بماند)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]


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

    def _process_playing(self, chat_id: ChatId, game: Game, context: CallbackContext):
        """
        حلقه اصلی بازی: وضعیت را چک می‌کند، اگر دور تمام شده به مرحله بعد می‌رود،
        در غیر این صورت نوبت را به بازیکن بعدی می‌دهد.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            # اطمینان از پاس دادن context
            self._go_to_next_street(game, chat_id, context)
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
            # ===== نقطه اصلی اصلاح =====
            # خطای شما از اینجا بود. آرگومان context پاس داده نمی‌شد.
            self._go_to_next_street(game, chat_id, context)
            return

        player = self._current_turn_player(game)
        if player and player.state == PlayerState.ACTIVE:
            # FIX 1 (PART 2): Call _send_turn_message without the 'money' argument.
            self._send_turn_message(game, player, chat_id)
        else:
            # If current player is not active, move to the next one.
            self._move_to_next_player_and_process(game, chat_id, context)



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
    # --- Player Action Handlers ---
    # این بخش تمام حرکات ممکن بازیکنان در نوبتشان را مدیریت می‌کند.
    
    def player_action_fold(self, update: Update, context: CallbackContext, game: Game) -> None:
        """بازیکن فولد می‌کند، از دور شرط‌بندی کنار می‌رود و نوبت به نفر بعدی منتقل می‌شود."""
        current_player = self._current_turn_player(game)
        if not current_player:
            return
    
        chat_id = update.effective_chat.id
        current_player.state = PlayerState.FOLD
        self._view.send_message(chat_id, f"🏳️ {current_player.mention_markdown} فولد کرد.")
    
        # برای اطمینان از پاک شدن دکمه‌ها، مارک‌آپ را حذف می‌کنیم
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
    
        self._move_to_next_player_and_process(game, chat_id, context)
    
    def player_action_call_check(self, update: Update, context: CallbackContext, game: Game) -> None:
        """بازیکن کال (پرداخت) یا چک (عبور) را انجام می‌دهد."""
        current_player = self._current_turn_player(game)
        if not current_player:
            return
    
        chat_id = update.effective_chat.id
        call_amount = game.max_round_rate - current_player.round_rate
        current_player.has_acted = True
    
        try:
            if call_amount > 0:
                # منطق Call
                current_player.wallet.authorize(game.id, call_amount)
                current_player.round_rate += call_amount
                current_player.total_bet += call_amount
                game.pot += call_amount
                self._view.send_message(chat_id, f"🎯 {current_player.mention_markdown} با {call_amount}$ کال کرد.")
            else:
                # منطق Check
                self._view.send_message(chat_id, f"✋ {current_player.mention_markdown} چک کرد.")
        except UserException as e:
            self._view.send_message(chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}")
            return  # اگر پول نداشت، از ادامه متد جلوگیری کن
    
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
    
        self._move_to_next_player_and_process(game, chat_id, context)
    
    def player_action_raise_bet(self, update: Update, context: CallbackContext, game: Game, raise_amount: int) -> None:
        """بازیکن شرط را افزایش می‌دهد (Raise) یا برای اولین بار شرط می‌بندد (Bet)."""
        current_player = self._current_turn_player(game)
        if not current_player:
            return
    
        chat_id = update.effective_chat.id
        call_amount = game.max_round_rate - current_player.round_rate
        total_amount_to_bet = call_amount + raise_amount
    
        try:
            current_player.wallet.authorize(game.id, total_amount_to_bet)
            current_player.round_rate += total_amount_to_bet
            current_player.total_bet += total_amount_to_bet
            game.pot += total_amount_to_bet
    
            # به‌روزرسانی حداکثر شرط و اعلام آن
            game.max_round_rate = current_player.round_rate
            action_text = "بِت" if call_amount == 0 else "رِیز"
            self._view.send_message(chat_id, f"💹 {current_player.mention_markdown} {action_text} زد و شرط رو به {current_player.round_rate}$ رسوند.")
    
            # --- بخش کلیدی منطق پوکر ---
            # وقتی کسی رِیز می‌کند، نوبت بازی باید یک دور کامل دیگر بچرخد
            game.trading_end_user_id = current_player.user_id
            current_player.has_acted = True
            # وضعیت بقیه بازیکنان فعال را برای بازی در دور جدید ریست می‌کنیم
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False
    
        except UserException as e:
            self._view.send_message(chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}")
            return
    
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
    
        self._move_to_next_player_and_process(game, chat_id, context)
    
    def player_action_all_in(self, update: Update, context: CallbackContext, game: Game) -> None:
        """بازیکن تمام موجودی خود را شرط می‌بندد (All-in)."""
        current_player = self._current_turn_player(game)
        if not current_player:
            return
    
        chat_id = update.effective_chat.id
        all_in_amount = current_player.wallet.value()
    
        if all_in_amount <= 0:
            self._view.send_message(chat_id, f"👀 {current_player.mention_markdown} موجودی برای آل-این ندارد و چک می‌کند.")
            self.player_action_call_check(update, context, game) # این حرکت معادل چک است
            return
    
        current_player.wallet.authorize(game.id, all_in_amount)
        current_player.round_rate += all_in_amount
        current_player.total_bet += all_in_amount
        game.pot += all_in_amount
        current_player.state = PlayerState.ALL_IN
        current_player.has_acted = True
    
        self._view.send_message(chat_id, f"🀄 {current_player.mention_markdown} با {all_in_amount}$ آل‑این کرد!")
    
        if current_player.round_rate > game.max_round_rate:
            game.max_round_rate = current_player.round_rate
            # اگر آل-این باعث افزایش شرط شد، مانند رِیز عمل می‌کند
            game.trading_end_user_id = current_player.user_id
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False
    
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
    
        self._move_to_next_player_and_process(game, chat_id, context)
    

        
    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        """از ایندکس مشخص شده، به دنبال بازیکن بعدی که FOLD یا ALL_IN نکرده می‌گردد."""
        num_players = len(game.players)
        for i in range(1, num_players + 1):
            next_index = (start_index + i) % num_players
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index
        return -1 # هیچ بازیکن فعالی یافت نشد

    def _move_to_next_player_and_process(self, game: Game, chat_id: ChatId, context: CallbackContext):
        """
        ایندکس بازیکن را به نفر فعال بعدی منتقل کرده و حلقه بازی را ادامه می‌دهد.
        """
        next_player_index = self._find_next_active_player_index(
            game, game.current_player_index
        )
        if next_player_index == -1:
            # حالا که context را داریم، آن را به go_to_next_street هم پاس می‌دهیم
            self._go_to_next_street(game, chat_id, context)
        else:
            game.current_player_index = next_player_index
            # context را به process_playing پاس می‌دهیم
            self._process_playing(chat_id, game, context)
            
    def _go_to_next_street(self, game: Game, chat_id: ChatId, context: CallbackContext):
        """
        بازی را به مرحله بعدی (Flop, Turn, River, Showdown) منتقل می‌کند.
        این متد همچنین وضعیت بازیکنان را برای دور شرط‌بندی جدید ریست می‌کند.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) < 2:
            self._showdown(game, chat_id, context)
            return

        game.reset_round_rates_and_actions()
        if game.state != GameState.ROUND_PRE_FLOP:
            game.current_player_index = self._find_next_active_player_index(game, game.dealer_index)

        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            self.add_cards_to_table(3, game, chat_id, "🃏 فلاپ (Flop)")
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            # VVVV اموجی منطقی‌تر VVVV
            self.add_cards_to_table(1, game, chat_id, "4️⃣ تِرن (Turn)")
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            # VVVV اموجی منطقی‌تر VVVV
            self.add_cards_to_table(1, game, chat_id, "🏁 ریوِر (River)")
        elif game.state == GameState.ROUND_RIVER:
            self._showdown(game, chat_id, context)
            return
        else:
            self._showdown(game, chat_id, context)
            return

        if game.state != GameState.FINISHED:
             self._process_playing(chat_id, game, context)

    def _determine_all_scores(self, game: Game) -> List[Dict]:
        """
        برای تمام بازیکنان فعال، دست و امتیازشان را محاسبه کرده و لیستی از دیکشنری‌ها را برمی‌گرداند.
        این متد باید از نسخه بروز شده WinnerDetermination استفاده کند.
        """
        player_scores = []
        # بازیکنانی که فولد نکرده‌اند در تعیین نتیجه شرکت می‌کنند
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        
        if not contenders:
            return []

        for player in contenders:
            if not player.cards:
                continue
            
            # **نکته مهم**: متد get_hand_value در WinnerDetermination باید بروز شود تا سه مقدار برگرداند
            # score, best_hand, hand_type = self._winner_determine.get_hand_value(player.cards, game.cards_table)
            
            # پیاده‌سازی موقت تا زمان آپدیت winnerdetermination
            # در اینجا فرض می‌کنیم متد `get_hand_value_and_type` در کلاس `WinnerDetermination` وجود دارد
            try:
                score, best_hand, hand_type = self._winner_determine.get_hand_value_and_type(player.cards, game.cards_table)
            except AttributeError:
                # اگر `get_hand_value_and_type` هنوز پیاده سازی نشده است، این بخش اجرا می شود.
                # این یک fallback موقت است.
                print("WARNING: 'get_hand_value_and_type' not found in WinnerDetermination. Update winnerdetermination.py")
                score, best_hand = self._winner_determine.get_hand_value(player.cards, game.cards_table)
                # یک روش موقت برای حدس زدن نوع دست بر اساس امتیاز
                hand_type_value = score // (15**5)
                hand_type = HandsOfPoker(hand_type_value) if hand_type_value > 0 else HandsOfPoker.HIGH_CARD


            player_scores.append({
                "player": player,
                "score": score,
                "best_hand": best_hand,
                "hand_type": hand_type
            })
        return player_scores
    def _find_winners_from_scores(self, player_scores: List[Dict]) -> Tuple[List[Player], int]:
        """از لیست امتیازات، برندگان و بالاترین امتیاز را پیدا می‌کند."""
        if not player_scores:
            return [], 0
            
        highest_score = max(data['score'] for data in player_scores)
        winners = [data['player'] for data in player_scores if data['score'] == highest_score]
        return winners, highest_score
        
    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, street_name: str):
        """
        کارت‌های جدید را به میز اضافه کرده و تصویر میز را با فرمت جدید و زیبا ارسال می‌کند.
        اگر count=0 باشد، فقط کارت‌های فعلی را نمایش می‌دهد.
        """
        # مرحله ۱: اضافه کردن کارت‌های جدید در صورت نیاز
        if count > 0:
            for _ in range(count):
                if game.remain_cards:
                    game.cards_table.append(game.remain_cards.pop())

        # مرحله ۲: بررسی وجود کارت روی میز
        if not game.cards_table:
            # اگر کارتی روی میز نیست، به جای عکس، یک پیام متنی ساده می‌فرستیم.
            msg_id = self._view.send_message_return_id(chat_id, "هنوز کارتی روی میز نیامده است.")
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
                self._view.remove_message_delayed(chat_id, msg_id, 5)
            return

        # مرحله ۳: ساخت رشته کارت‌ها با فرمت جدید (دو فاصله بین هر کارت)
        cards_str = "  ".join(game.cards_table)

        # مرحله ۴: ساخت کپشن دو خطی و زیبا
        caption = f"{street_name}\n{cards_str}"

        # مرحله ۵: ارسال تصویر میز با کپشن جدید
        msg = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=caption,
        )

        # پیام تصویر میز را برای حذف در انتهای دست، ذخیره می‌کنیم
        if msg:
            game.message_ids_to_delete.append(msg.message_id)


    # --- این نسخه جدید و کامل را جایگزین _finish قبلی کن ---
    def _finish(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        پایان یک دست از بازی: برندگان را مشخص، نتایج را اعلام، و پول را تقسیم می‌کند.
        این متد به تنهایی تمام منطق پایان بازی را مدیریت می‌کند.
        """
        print("DEBUG: Entering the unified _finish method.")
    
        # --- بخش ۱: تعیین برنده(ها) ---
        active_players = [p for p in game.players if p.state != PlayerState.FOLD]
        winners_data = [] # ساختار داده: [((نوع دست, امتیاز), کارت‌ها, بازیکن), ...]
    
        if len(active_players) == 1:
            # حالت اول: فقط یک بازیکن باقی مانده (بقیه فولد کرده‌اند)
            winner_player = active_players[0]
            # چون Showdown رخ نداده، نوع دست و کارت‌ها اهمیتی ندارد.
            winners_data.append(((None, 1), [], winner_player))
            print(f"DEBUG: Only one player left. Winner: {winner_player.user_id}")
        else:
            # حالت دوم: Showdown! باید امتیازات محاسبه شود.
            player_scores_data = []
            for player in active_players:
                hand_type, score, best_hand = self._winner_determine.get_hand_value(
                    player_cards=player.cards,
                    table_cards=game.cards_table
                )
                player_scores_data.append(((hand_type, score), best_hand, player))
                print(f"DEBUG: Player {player.user_id} has {hand_type.name} with score {score}")
    
            # مرتب‌سازی بر اساس امتیاز
            player_scores_data.sort(key=lambda x: x[0][1], reverse=True)
    
            # پیدا کردن تمام بازیکنان با بالاترین امتیاز
            if player_scores_data:
                highest_score = player_scores_data[0][0][1]
                winners_data = [data for data in player_scores_data if data[0][1] == highest_score]
                print(f"DEBUG: Highest score is {highest_score}. Winners: {[w[2].user_id for w in winners_data]}")
    
        # --- بخش ۲: نمایش نتایج و تقسیم پول ---
        if not winners_data:
            self._view.send_message(chat_id, "خطایی در تعیین برنده رخ داد. هیچ برنده‌ای یافت نشد.")
            game.reset()
            return
    
        # نمایش نتایج
        # (این بخش منطق نمایش نتایج و تقسیم پول است که قبلاً هم در finish وجود داشت)
        winners_count = len(winners_data)
        win_amount = game.pot // winners_count
        
        # نمایش دست برنده
        first_winner_data = winners_data[0]
        win_hand_type = first_winner_data[0][0]
        win_hand_cards = first_winner_data[2].cards # نمایش کارت‌های خود بازیکن
    
        if win_hand_type: # اگر بازی به شودان رسیده باشد
            hand_info = HAND_NAMES_TRANSLATIONS.get(win_hand_type, {"fa": "نامشخص", "emoji": "❓"})
            hand_text = f"{hand_info['emoji']} دست برنده: **{hand_info['fa']}**"
            cards_text = " ".join(str(c) for c in win_hand_cards)
            self._view.send_message(chat_id, f"{hand_text}\n{cards_text}")
    
        # اعلام برندگان
        mentions = [f"🏆 {w[2].mention_markdown}" for w in winners_data]
        result_text = f"🎉 **برنده(ها):**\n" + "\n".join(mentions)
        result_text += f"\n\n💰 هر کدام **{win_amount}$** برنده شدید!"
        self._view.send_message(chat_id, result_text)
    
        # تقسیم پول
        for _, _, winner_player in winners_data:
            winner_player.wallet.inc(win_amount)
            winner_player.wallet.approve(game.id) # تایید تراکنش‌های این دست
    
        # برای بازیکنانی که در بازی بودند ولی نبردند
        for p in game.players:
            is_winner = any(p.user_id == w[2].user_id for w in winners_data)
            if not is_winner:
                p.wallet.approve(game.id) # پول آنها خرج شده و تمام
    
        # --- بخش ۳: آماده‌سازی برای دست بعدی ---
        self._view.send_message(
            chat_id,
            "برای شروع دست بعدی، /start را بزنید.\nبرای دیدن موجودی /money را بزنید."
        )
        game.state = GameState.FINISHED
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            # Replacing underscore with space and title-casing the output
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"
            
    def _cleanup_messages_by_lifespan(self, game: Game, chat_id: ChatId, lifespan: MessageLifespan):
        """
        تمام پیام‌های با چرخه عمر مشخص را از چت و از دفتر ثبت پاک می‌کند.
        """
        messages_to_keep = []
        messages_to_delete = []

        for msg_id, msg_lifespan in game.message_ledger:
            if msg_lifespan == lifespan:
                messages_to_delete.append(msg_id)
            else:
                messages_to_keep.append((msg_id, msg_lifespan))

        print(f"DEBUG: Cleaning up {len(messages_to_delete)} messages with lifespan '{lifespan.value}'.")
        for msg_id in messages_to_delete:
            self._view.remove_message(chat_id, msg_id)
        
        # دفتر ثبت را با پیام‌هایی که هنوز عمرشان تمام نشده، به‌روز می‌کنیم
        game.message_ledger = messages_to_keep

    def _cleanup_turn_messages(self, game: Game, chat_id: ChatId):
        """پیام‌های نوبت قبلی را پاک می‌کند."""
        # ۱. دکمه‌های پیام نوبت قبلی را حذف می‌کند
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
            game.turn_message_id = None
        
        # ۲. پیام‌های متنی با چرخه عمر TURN را پاک می‌کند
        self._cleanup_messages_by_lifespan(game, chat_id, MessageLifespan.TURN)
    
    # --- این نسخه را جایگزین _showdown قبلی کن ---
    def _showdown(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        مرحله نهایی بازی (Showdown): کارت‌ها رو می‌شود و برندگان مشخص می‌شوند.
        این متد اکنون به طور مستقیم متد _finish را برای پردازش نهایی فرا می‌خواند.
        """
        self._view.send_message(
            chat_id=chat_id,
            text="⚔️ **شــــــــــــودان!** ⚔️\n\nوقت رو کردن کارت‌ها و مشخص شدن برنده است..."
        )
    
        # پاک کردن تمام پیام‌های نوبت و کارت‌های قبلی
        self._clear_game_messages(game, chat_id)
    
        # فراخوانی مستقیم و تمیز متد نهایی برای تعیین برنده، تقسیم جوایز و اتمام دست
        self._finish(game, chat_id, context)
    def _send_managed_message(
        self,
        game: Game,
        chat_id: ChatId,
        lifespan: MessageLifespan,
        text: str,
        **kwargs
    ) -> Optional[MessageId]:
        """
        پیام را ارسال کرده و آن را با چرخه عمر مشخص در دفتر ثبت پیام، بایگانی می‌کند.
        این متد، نقطه مرکزی مدیریت پیام‌های موقتی است.
        """
        msg_id = self._view.send_message_return_id(chat_id, text, **kwargs)
        if msg_id:
            game.message_ledger.append((msg_id, lifespan))
            print(f"DEBUG: Message {msg_id} logged with lifespan '{lifespan.value}'.")
        return msg_id

class RoundRateModel:
    def __init__(self, view: PokerBotViewer, kv: redis.Redis, model: "PokerBotModel"):
        self._view = view
        self._kv = kv
        self._model = model # <<< نمونه model ذخیره شد

    # داخل کلاس RoundRateModel
    def set_blinds(self, game: Game, chat_id: ChatId) -> None:
        """
        بلایند کوچک و بزرگ را برای شروع دور جدید تعیین و از حساب بازیکنان کم می‌کند.
        این متد برای حالت دو نفره (Heads-up) نیز بهینه شده است.
        """
        num_players = len(game.players)
    
        if num_players < 2:
            # نباید این اتفاق بیفتد، اما برای اطمینان
            return 
    
        # --- بلوک اصلاح شده برای تعیین بلایندها ---
        if num_players == 2:
            # حالت دو نفره (Heads-up): دیلر اسمال بلایند است و اول بازی می‌کند.
            small_blind_index = game.dealer_index
            big_blind_index = (game.dealer_index + 1) % num_players
            first_action_index = small_blind_index # در pre-flop، اسمال بلایند اول حرکت می‌کند
        else:
            # حالت استاندارد برای بیش از دو بازیکن
            small_blind_index = (game.dealer_index + 1) % num_players
            big_blind_index = (game.dealer_index + 2) % num_players
            first_action_index = (big_blind_index + 1) % num_players
        # --- پایان بلوک اصلاح شده ---
    
        small_blind_player = game.players[small_blind_index]
        big_blind_player = game.players[big_blind_index]
        
        # اعمال بلایندها
        self._set_player_blind(game, small_blind_player, SMALL_BLIND, "کوچک", chat_id)
        self._set_player_blind(game, big_blind_player, SMALL_BLIND * 2, "بزرگ", chat_id)
    
        game.max_round_rate = SMALL_BLIND * 2
        
        # تعیین نوبت اولین بازیکن برای اقدام
        game.current_player_index = first_action_index
        # بازیکنی که دور شرط‌بندی به او ختم می‌شود، بیگ بلایند است
        game.trading_end_user_id = big_blind_player.user_id
        
        # ارسال پیام نوبت به بازیکن
        player_turn = game.players[game.current_player_index]
        self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=player_turn,
            money=player_turn.wallet.value()
        )


    def _set_player_blind(self, game: Game, player: Player, amount: Money, blind_type: str, chat_id: ChatId):

        """یک بلایند مشخص را روی بازیکن اعمال می‌کند."""
        try:
            player.wallet.authorize(game_id=str(chat_id), amount=amount)
            player.round_rate += amount
            game.pot += amount
            self._view.send_message(
                chat_id,
                f"💸 {player.mention_markdown} بلایند {blind_type} به مبلغ {amount}$ را پرداخت کرد."
            )
        except UserException as e:
            # اگر پول کافی نبود، بازیکن آل-این می‌شود
            available_money = player.wallet.value()
            player.wallet.authorize(game_id=str(chat_id), amount=available_money)
            player.round_rate += available_money
            game.pot += available_money
            player.state = PlayerState.ALL_IN
            self._view.send_message(
                chat_id,
                f"⚠️ {player.mention_markdown} موجودی کافی برای بلایند نداشت و All-in شد ({available_money}$)."
            )

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
