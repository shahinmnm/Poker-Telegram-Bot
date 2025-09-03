#!/usr/bin/env python3

import datetime
import random
from typing import Dict, List, Optional
import redis

from telegram import (
    Update,
    User,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    CallbackContext,
)

from pokerapp.entities import (
    Money,
    Player,
    PlayerState,
    Game,
    GameState,
    UserException,
    UserId,
    ChatId,
    MessageId,
    MessageLifespan,
    Wallet,
)

from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.config import Config
from pokerapp.winnerdetermination import WinnerDetermination, HAND_NAMES_TRANSLATIONS # <-- ایمپورت جدید
from pokerapp.roundratemodel import RoundRateModel

# --- Constants ---
KEY_GAME = "game"
KEY_OLD_PLAYERS = "old_players"
MIN_PLAYERS = 2
MAX_PLAYERS = 9
SMALL_BLIND = 1
BIG_BLIND = 2
DEFAULT_MONEY = 200
PLAYER_TIME_OUT = 45.0  # Seconds


class PokerBotModel:
    def __init__(self, cfg: Config, view: PokerBotViewer, kv: redis.Redis, bot):
        self._cfg = cfg
        self._view = view
        self._kv = kv
        self._bot = bot
        self._winner_determination = WinnerDetermination()
        self._round_rate = RoundRateModel(view, self)
        self._min_players = self._cfg.MIN_PLAYERS
        self.ACTIVE_GAME_STATES = [
            GameState.ROUND_PRE_FLOP,
            GameState.ROUND_FLOP,
            GameState.ROUND_TURN,
            GameState.ROUND_RIVER,
        ]

    def _game_from_context(self, context: CallbackContext) -> Game:
        if KEY_GAME not in context.chat_data:
            context.chat_data[KEY_GAME] = Game(
                players=[],
                pot=0,
                small_blind=self._cfg.SMALL_BLIND,
                big_blind=self._cfg.BIG_BLIND,
            )
        return context.chat_data[KEY_GAME]

    def _cleanup_round_messages(self, game: Game, chat_id: ChatId) -> None:
        """
        تمام پیام‌های موقتی (مانند پیام نوبت، پیام‌های شرط‌بندی) را که برای یک راند ثبت شده‌اند، پاک می‌کند.
        این متد در پایان هر دست فراخوانی می‌شود.
        """
        # ابتدا پیام نوبت فعلی را حذف می‌کنیم (اگر وجود داشته باشد)
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
            game.turn_message_id = None # پاک کردن شناسه

        # پیام‌های ثبت‌شده در دفتر کل را پاک می‌کنیم
        for message_id, lifespan in game.message_ledger:
            if lifespan in (MessageLifespan.TURN, MessageLifespan.ROUND):
                self._view.remove_message(chat_id, message_id)

        # دفتر کل را از پیام‌های پاک‌شده، خالی می‌کنیم
        game.message_ledger = [
            (mid, ls) for mid, ls in game.message_ledger
            if ls not in (MessageLifespan.TURN, MessageLifespan.ROUND)
        ]

        # پیام‌های متفرقه ثبت‌شده برای حذف را پاک می‌کنیم
        for message_id in game.message_ids_to_delete:
            self._view.remove_message(chat_id, message_id)
        game.message_ids_to_delete.clear()


    def new_game(self, update: Update, context: CallbackContext) -> None:
        """Starts a new game, waiting for players to join."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state in self.ACTIVE_GAME_STATES:
            self._view.send_message(chat_id, "⚠️ یک بازی در حال حاضر در جریان است. برای شروع دست جدید، منتظر بمانید یا از دستور /stop استفاده کنید.")
            return

        # Reset game state for a new round
        game.reset()
        context.chat_data[KEY_OLD_PLAYERS] = []

        # Send initial message to gather players
        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)
        msg_id = self._view.send_message_return_id(
            chat_id,
            "🎉 بازی جدید پوکر! برای اعلام آمادگی از دکمه /ready استفاده کنید.",
            reply_markup=keyboard
        )
        if msg_id:
            game.ready_message_main_id = msg_id


    def hide_cards(self, update: Update, context: CallbackContext) -> None:
        """
        کیبورد کارت‌های بازیکن را پنهان کرده و دکمه "نمایش کارت‌ها" را نشان می‌دهد.
        """
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        player_mention = update.effective_user.mention_markdown()

        # پیدا کردن بازیکن
        current_player = next((p for p in game.players if p.user_id == user_id), None)
        if not current_player:
            return # اگر بازیکن در بازی نیست، کاری نکن

        # حذف پیام حاوی کیبورد کارت‌ها
        # چون شناسه این پیام را ذخیره نکرده‌ایم، نمی‌توانیم مستقیم حذفش کنیم.
        # در عوض، یک پیام جدید برای جایگزینی آن می‌فرستیم.

        # نمایش کیبورد جدید
        self._view.show_reopen_keyboard(chat_id, player_mention)

        # حذف پیام "/پنهان کردن کارت‌ها"
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)

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

    def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف و مدیریت آینده ثبت می‌کند. (نسخه نهایی)"""
        # ۱. دکمه‌های پیام نوبت قبلی را حذف می‌کنیم تا بازیکن قبلی نتواند بازی کند.
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)

        # ۲. موجودی بازیکن را به‌روز دریافت می‌کنیم.
        money = player.wallet.value()

        # ۳. پیام نوبت جدید را ارسال و شناسه آن را دریافت می‌کنیم.
        msg_id = self._view.send_turn_actions(chat_id, game, player, money)

        # ۴. اگر پیام با موفقیت ارسال شد، شناسه آن را در دو محل کلیدی ذخیره می‌کنیم.
        if msg_id:
            # این متغیر برای حذف دکمه‌ها در نوبت بعدی استفاده می‌شود (کد شما این را داشت).
            game.turn_message_id = msg_id

            # *** این خط مهم‌ترین بخش اصلاح است ***
            # شناسه را به دفتر ثبت کل پیام‌ها اضافه می‌کنیم تا متد cleanup بتواند آن را پیدا و حذف کند.
            game.message_ledger.append((msg_id, MessageLifespan.TURN))

        # ۵. زمان آخرین نوبت را برای مدیریت تایم‌اوت ثبت می‌کنیم.
        game.last_turn_time = datetime.datetime.now()

    def player_action_fold(self, update: Update, context: CallbackContext, game: Game) -> None:
        """بازیکن فولد می‌کند و پیام آن با چرخه عمر TURN ثبت می‌شود."""
        current_player = self._current_turn_player(game)
        if not current_player:
            return

        chat_id = update.effective_chat.id
        current_player.state = PlayerState.FOLD
        current_player.has_acted = True

        msg_text = f"✋ {current_player.mention_markdown} فولد داد."
        msg_id = self._view.send_message_return_id(chat_id, msg_text)
        if msg_id:
            game.message_ledger.append((msg_id, MessageLifespan.TURN))

        self._move_to_next_player_and_process(game, chat_id, context)

    def _move_to_next_player_and_process(self, game: Game, chat_id: ChatId, context: CallbackContext):
        """نوبت را به بازیکن بعدی منتقل کرده و حلقه بازی را ادامه می‌دهد."""
        game.turn_index = self._get_next_player_turn_index(game)
        self._process_playing(chat_id, game, context)

    def get_user(self, update: Update, context: CallbackContext) -> Optional[User]:
        user_id = update.effective_user.id
        return self._bot.get_chat_member(context.job.context["chat_id"], user_id).user

    def player_action_check_or_call(self, update: Update, context: CallbackContext, game: Game) -> None:
        """بازیکن حرکت Check یا Call را انجام می‌دهد."""
        self._round_rate.player_action_check_or_call(update, game, context)

    def player_action_bet_or_raise(self, update: Update, context: CallbackContext, game: Game, amount_percent: int) -> None:
        """بازیکن حرکت Bet یا Raise را انجام می‌دهد."""
        self._round_rate.player_action_bet_or_raise(update, game, context, amount_percent)

    def player_action_all_in(self, update: Update, context: CallbackContext, game: Game) -> None:
        """بازیکن All-in می‌کند."""
        self._round_rate.player_action_all_in(update, game, context)

    def _get_next_player_turn_index(self, game: Game) -> int:
        """
        ایندکس بازیکن بعدی که در بازی فعال است را پیدا می‌کند.
        این متد از حلقه for برای جلوگیری از لوپ بی‌نهایت استفاده می‌کند.
        """
        if not any(p.state == PlayerState.ACTIVE for p in game.players):
             return -1 # No active players

        current_index = game.turn_index
        for i in range(len(game.players)):
            next_index = (current_index + 1 + i) % len(game.players)
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index

        return -1 # Should not be reached if there is at least one active player

    def _current_turn_player(self, game: Game) -> Optional[Player]:
        """بازیکنی که در حال حاضر نوبت بازی اوست را برمی‌گرداند."""
        if game.turn_index is None or not (0 <= game.turn_index < len(game.players)):
            return None
        return game.players[game.turn_index]

    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, title: str = "") -> None:
        """
        تعداد مشخصی کارت به میز اضافه کرده و تصویر آن را با یک عنوان مناسب ارسال می‌کند.
        """
        if count > 0:
            # Burn one card before dealing
            if game.remain_cards:
                game.remain_cards.pop()

            new_cards = [game.remain_cards.pop() for _ in range(count) if game.remain_cards]
            game.cards_table.extend(new_cards)

        # همیشه تصویر میز را ارسال کن، حتی اگر کارتی اضافه نشود (برای دستور /show_table)
        if game.cards_table:
            message = self._view.send_desk_cards_img(
                chat_id=chat_id,
                cards=game.cards_table,
                caption=f"{title}\n💰 Pot: {game.pot}$"
            )
            if message:
                game.message_ids_to_delete.append(message.message_id)

    def _go_to_next_street(self, game: Game, chat_id: ChatId, context: CallbackContext):
        """
        بازی را به مرحله بعدی (Flop, Turn, River, Showdown) می‌برد.
        این متد مسئول پاکسازی پیام‌ها، جمع‌آوری شرط‌ها و توزیع کارت‌های جدید است.
        """
        # ۱. جمع‌آوری شرط‌ها در پات و ریست کردن مقادیر شرط در این راند
        self.collect_bets_for_pot(game)

        # ۲. ریست کردن وضعیت 'has_acted' برای همه بازیکنان برای راند بعدی
        for p in game.players:
            p.has_acted = False

        # ۳. انتقال به مرحله بعدی بر اساس وضعیت فعلی
        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            self.add_cards_to_table(3, game, chat_id, "--- FLOP ---")
            game.turn_index = self._get_first_player_index(game)
            self._process_playing(chat_id, game, context)

        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            self.add_cards_to_table(1, game, chat_id, "--- TURN ---")
            game.turn_index = self._get_first_player_index(game)
            self._process_playing(chat_id, game, context)

        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            self.add_cards_to_table(1, game, chat_id, "--- RIVER ---")
            game.turn_index = self._get_first_player_index(game)
            self._process_playing(chat_id, game, context)

        elif game.state == GameState.ROUND_RIVER:
            # اطمینان از پاس دادن context
            self._showdown(game, chat_id, context)

    def _get_first_player_index(self, game: Game) -> int:
        """
        ایندکس اولین بازیکن فعال بعد از دیلر را برای شروع راندهای بعد از فلاپ پیدا می‌کند.
        """
        dealer_index = game.dealer_index
        num_players = len(game.players)

        for i in range(1, num_players + 1):
            player_index = (dealer_index + i) % num_players
            player = game.players[player_index]
            if player.state in (PlayerState.ACTIVE, PlayerState.ALL_IN):
                # For betting rounds, the first *active* player should start.
                if player.state == PlayerState.ACTIVE:
                    return player_index

        # Fallback in case only all-in players are left
        for i in range(1, num_players + 1):
            player_index = (dealer_index + i) % num_players
            if game.players[player_index].state in (PlayerState.ACTIVE, PlayerState.ALL_IN):
                return player_index

        return -1 # Should not happen

    def stop(self, update: Update, context: CallbackContext) -> None:
        """بازی فعلی را متوقف کرده و وضعیت را ریست می‌کند."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state == GameState.INITIAL:
            self._view.send_message(chat_id, "هنوز بازی شروع نشده است.")
            return

        # پاک کردن تمام پیام‌های باقی‌مانده از بازی
        self._cleanup_round_messages(game, chat_id)

        # بازگرداندن پول بازیکنانی که در بازی بودند
        for player in game.players:
            if player.in_pot > 0:
                player.wallet.inc(player.in_pot)
                # Note: This is a simplified refund. A full transaction system would be better.

        game.reset()
        context.chat_data[KEY_GAME] = game # Save the reset state

        self._view.send_message(chat_id, "🛑 بازی متوقف و ریست شد. برای شروع یک دست جدید، از /new_game استفاده کنید.")


    def _showdown(self, game: Game, chat_id: ChatId, context: CallbackContext):
        """
        مرحله نهایی بازی: تعیین برنده(ها) و توزیع پات.
        این متد هم حالت نمایش کارت (Showdown) و هم حالت برد به دلیل فولد بقیه را مدیریت می‌کند.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        is_showdown = True
        if len(contenders) == 1:
            # حالت برد به دلیل فولد دیگران
            is_showdown = False
            winner = contenders[0]
            # ما به داده‌های win_data نیازی نداریم، اما ساختار را برای سازگاری با متد توزیع پات حفظ می‌کنیم
            winners_data = [{'player': winner, 'win_data': None}]
        else:
            # حالت نمایش کارت‌ها (Showdown)
            is_showdown = True
            winners_data = self._determine_winners(game)

        # توزیع پات و ارسال پیام نتایج
        self._distribute_pot(game, winners_data, chat_id, is_showdown)

        # پاکسازی پیام‌های این دور
        self._cleanup_round_messages(game, chat_id)

        # تنظیم وضعیت بازی به پایان یافته
        game.state = GameState.FINISHED

        # ارسال پیام برای شروع دست بعدی
        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)
        self._view.send_message(
            chat_id,
            "یک دست دیگر بازی کنیم؟ /ready را بزنید تا به لیست اضافه شوید و سپس /start.",
            reply_markup=keyboard
        )

    def _determine_winners(self, game: Game) -> List[Dict]:
        """
        برنده(ها) را بر اساس بهترین دست ۵ کارتی از بین بازیکنان باقی‌مانده (contenders) تعیین می‌کند.
        اطلاعات دست برنده را در player.win_data ذخیره می‌کند.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if not contenders:
            return []

        # محاسبه امتیاز دست هر بازیکن
        for p in contenders:
            hand_type, score, best_5_cards = self._winner_determination.get_hand_value(p.cards, game.cards_table)
            p.win_data = {
                "hand_type": hand_type,
                "score": score,
                "best_5_cards": best_5_cards
            }

        # مرتب‌سازی بازیکنان بر اساس امتیاز دست (از بیشترین به کمترین)
        contenders.sort(key=lambda p: p.win_data['score'], reverse=True)

        # پیدا کردن بالاترین امتیاز
        max_score = contenders[0].win_data['score']

        # شناسایی تمام بازیکنانی که امتیازشان برابر با بالاترین امتیاز است (حالت مساوی)
        winners = [p for p in contenders if p.win_data['score'] == max_score]

        return [{'player': p, 'win_data': p.win_data} for p in winners]

    def _distribute_pot(self, game: Game, winners_data: list, chat_id: ChatId, is_showdown: bool):
        """
        پات را بین برندگان توزیع می‌کند و پیام نتایج نهایی را با فرمت حرفه‌ای ارسال می‌کند.
        """
        total_pot = game.pot
        num_winners = len(winners_data)
        if num_winners == 0:
            # این حالت نباید اتفاق بیفتد، اما برای اطمینان
            print("[WARNING] No winners found to distribute the pot.")
            return

        # محاسبه مقدار برد برای هر نفر
        win_amount_each = total_pot // num_winners
        # اگر پات به طور مساوی تقسیم نشود، باقیمانده را نادیده می‌گیریم یا به نفر اول می‌دهیم
        # برای سادگی، فعلاً تقسیم صحیح انجام می‌دهیم.

        winners_info_for_message = []
        for item in winners_data:
            winner_player = item['player']
            winner_player.wallet.inc(win_amount_each)
            winners_info_for_message.append({
                "player": winner_player,
                "win_amount": win_amount_each
            })

        # --- ساخت پیام نهایی ---
        summary_lines = ["🏁 *پایان دست!* 🏁\n"]

        # 1. کارت‌های روی میز (همیشه نمایش داده می‌شود اگر وجود داشته باشد)
        if game.cards_table:
            table_cards_str = "  ".join(c.symbol for c in game.cards_table)
            summary_lines.append(f"💳 *کارت‌های روی میز:*\n`{table_cards_str}`\n")

        # 2. نتایج و برندگان
        summary_lines.append("🏆 *نتایج و برندگان:*")

        for info in winners_info_for_message:
            player = info['player']
            win_amount = info['win_amount']

            summary_lines.append("--------------------")
            summary_lines.append(f"👤 *بازیکن:* {player.mention_markdown}")

            # 3. اگر Showdown باشد، جزئیات دست را نمایش بده
            if is_showdown and player.win_data:
                # کارت‌های دست بازیکن
                player_cards_str = "  ".join(c.symbol for c in player.cards)
                summary_lines.append(f"👋 *کارت‌های دست:* `{player_cards_str}`")

                # نوع دست (با ترجمه فارسی و انگلیسی)
                hand_type = player.win_data['hand_type']
                best_5_cards = player.win_data['best_5_cards']
                hand_info = HAND_NAMES_TRANSLATIONS.get(hand_type, {
                    "fa": hand_type.name, "en": hand_type.name.replace('_', ' '), "emoji": "🃏"
                })
                summary_lines.append(f"{hand_info['emoji']} {hand_info['fa']} ({hand_info['en']})")

                # بهترین ترکیب ۵ کارتی
                best_5_cards_str = "  ".join(c.symbol for c in best_5_cards)
                summary_lines.append(f"🃏 *دست:* `{best_5_cards_str}`")

            # 4. مقدار برد
            summary_lines.append(f"💰 *برد:* {win_amount}$")

        summary_lines.append("--------------------")

        # 5. پات نهایی
        summary_lines.append(f"\n💰 *پات نهایی:* {total_pot}$")

        # ارسال پیام به گروه
        final_message = "\n".join(summary_lines)
        self._view.send_message(chat_id, final_message, parse_mode="Markdown")


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
