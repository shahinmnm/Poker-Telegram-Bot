#!/usr/bin/env python3

import datetime
import traceback
from threading import Timer
from typing import List, Tuple, Dict, Optional

import redis
from telegram import Message, ReplyKeyboardMarkup, Update, Bot
from telegram.ext import Handler, CallbackContext

from pokerapp.config import Config
from pokerapp.privatechatmodel import UserPrivateChatModel
from pokerapp.winnerdetermination import WinnerDetermination, HAND_RANK, HandsOfPoker
from pokerapp.cards import Cards
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    ChatId,
    UserId,
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

MAX_PLAYERS = 8
MIN_PLAYERS = 2
SMALL_BLIND = 5
DEFAULT_MONEY = 1000
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
        self._round_rate = RoundRateModel(view=self._view)

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
        return game.players[game.current_player_index]

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
                ready_message_id=update.effective_message.message_id,
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
                msg = self._view.send_message(chat_id, text, reply_markup=keyboard)
                game.ready_message_main_id = msg
        else:
            msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
            game.ready_message_main_id = msg

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
            old_players = context.chat_data.get(KEY_OLD_PLAYERS, [])
            for user_id in old_players:
                # TODO: اضافه کردن مجدد بازیکنان به صورت خودکار
                pass

        if len(game.players) >= self._min_players:
            self._start_game(context, game, chat_id)
        else:
            self._view.send_message(chat_id, f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).")
            
    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        """از ایندکس مشخص شده، به دنبال بازیکن بعدی که FOLD یا ALL_IN نکرده می‌گردد."""
        num_players = len(game.players)
        for i in range(num_players):
            next_index = (start_index + i) % num_players
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index
        return -1 # هیچ بازیکن فعالی یافت نشد
        
    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""
        if game.ready_message_main_id:
            self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None

        game.dealer_index = (game.dealer_index + 1) % len(game.players)
        
        self._view.send_message(chat_id, '🚀 !بازی شروع شد!')
        
        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game, chat_id)
        
        self._round_rate.set_blinds(game)
        
        # نفر بعد از Big Blind شروع می‌کند
        start_player_index = (game.dealer_index + 3) % len(game.players)
        game.current_player_index = self._find_next_active_player_index(game, start_player_index)
        
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]
        self._process_playing(chat_id, game)

    def _divide_cards(self, game: Game, chat_id: ChatId):
        """کارت‌ها را بین بازیکنان پخش می‌کند."""
        # کد این متد از نسخه شما مناسب بود و بدون تغییر باقی می‌ماند.
        # من فقط بخش ارسال در گروه را کمی تمیزتر می‌کنم.
        for player in game.players:
            if len(game.remain_cards) < 2:
                self._view.send_message(chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود.")
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards
            
            # ارسال کارت‌ها در گروه با دکمه‌های کنترلی
            msg_id = self._view.send_cards(
                chat_id=chat_id,
                cards=cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )
            if msg_id:
                game.message_ids_to_delete.append(msg_id)

    def _process_playing(self, chat_id: ChatId, game: Game):
        """
        حلقه اصلی بازی: وضعیت را چک می‌کند، اگر دور تمام شده به مرحله بعد می‌رود،
        در غیر این صورت نوبت را به بازیکن بعدی می‌دهد.
        """
        # شرط پایان بازی: فقط یک نفر (یا کمتر) باقی مانده که فولد نکرده
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            self._go_to_next_street(game, chat_id)
            return

        # شرط پایان دور (Street): همه بازیکنان فعال، یک مبلغ مساوی شرط بسته‌اند.
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        
        # اگر هیچ بازیکن فعالی نیست (همه یا فولد یا آل‌این) یا همه بازی کرده‌اند و شرط‌ها برابر است
        all_acted = all(p.has_acted for p in active_players)
        rates_are_equal = len(set(p.round_rate for p in active_players)) <= 1

        if not active_players or (all_acted and rates_are_equal):
            self._go_to_next_street(game, chat_id)
            return

        # پیدا کردن بازیکن بعدی و ارسال پیام نوبت
        player = self._current_turn_player(game)
        if player and player.state == PlayerState.ACTIVE:
            self._send_turn_message(game, player, chat_id)
        else:
            # اگر بازیکن فعلی دیگر فعال نیست، نوبت را به نفر بعدی بده
            next_player_index = self._find_next_active_player_index(game, (game.current_player_index + 1) % len(game.players))
            if next_player_index != -1:
                game.current_player_index = next_player_index
                self._process_playing(chat_id, game) # دوباره این متد را صدا بزن
            else: # هیچ بازیکن فعالی نمانده
                self._go_to_next_street(game, chat_id)


    def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف در آینده ذخیره می‌کند."""
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)

        money = player.wallet.value()
        msg_id = self._view.send_turn_actions(chat_id, game, player, money)
        game.turn_message_id = msg_id
        game.last_turn_time = datetime.datetime.now()
        
    def _go_to_next_street(self, game: Game, chat_id: ChatId):
        """بازی را به مرحله بعدی (Flop, Turn, River) یا به پایان (Finish) می‌برد."""
        self._round_rate.collect_bets_for_pot(game)
        
        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            self.add_cards_to_table(3, game, chat_id, "فلاپ (Flop)")
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            self.add_cards_to_table(1, game, chat_id, "تِرن (Turn)")
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            self.add_cards_to_table(1, game, chat_id, "ریوِر (River)")
        else: # اگر در River بودیم یا شرایط پایان بازی برقرار بود
            self._finish(game, chat_id)
            return

        # ریست کردن نوبت برای شروع دور جدید شرط‌بندی از نفر بعد از دیلر
        start_index = self._find_next_active_player_index(game, (game.dealer_index + 1) % len(game.players))
        if start_index == -1: # اگر هیچکس برای شرط‌بندی نمانده
            self._fast_forward_to_finish(game, chat_id)
        else:
            game.current_player_index = start_index
            self._process_playing(chat_id, game)
            
    def _fast_forward_to_finish(self, game: Game, chat_id: ChatId):
        """وقتی شرط‌بندی ممکن نیست، کارت‌های باقی‌مانده را رو کرده و به انتها می‌رود."""
        if game.state == GameState.ROUND_PRE_FLOP: self.add_cards_to_table(3, game, chat_id)
        if game.state == GameState.ROUND_FLOP: self.add_cards_to_table(1, game, chat_id)
        if game.state == GameState.ROUND_TURN: self.add_cards_to_table(1, game, chat_id)
        self._finish(game, chat_id)

    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, street_name: str = ""):
        """کارت به میز اضافه کرده و تصویر آن را ارسال می‌کند."""
        for _ in range(count):
            if game.remain_cards: game.cards_table.append(game.remain_cards.pop())
        
        caption = f"🔥 **مرحله {street_name}** 🔥\n💰 پات فعلی: {game.pot}$"
        msg = self._view.send_desk_cards_img(chat_id, game.cards_table, caption)
        if msg: game.message_ids_to_delete.append(msg.message_id)

    def _finish(self, game: Game, chat_id: ChatId):
        """بازی را تمام کرده، برندگان را اعلام و پات را تقسیم می‌کند."""
        if game.turn_message_id: self._view.remove_message(chat_id, game.turn_message_id)
        
        # نمایش کارت‌های نهایی اگر رو نشده‌اند
        while len(game.cards_table) < 5 and game.remain_cards:
            game.cards_table.append(game.remain_cards.pop())

        self._view.send_desk_cards_img(chat_id, game.cards_table, f"🃏 میز نهایی — 💰 پات: {game.pot}$")
        
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        scores = self._winner_determine.determinate_scores(contenders, game.cards_table)
        winners_money = self._round_rate.finish_game_and_distribute_pot(game, scores)

        text = self._format_winner_message(winners_money)
        self._view.send_message(chat_id=chat_id, text=text)

        # پاکسازی پیام‌های موقت
        for mid in game.message_ids_to_delete: self._view.remove_message_delayed(chat_id, mid)
        game.message_ids_to_delete.clear()
        
        game.state = GameState.FINISHED
        
        def reset_and_prompt():
            game.reset() # بازی برای دور بعد آماده می‌شود
            self._view.send_message(chat_id, "✅ با دستور /ready برای دست بعد آماده شوید.")
        
        Timer(5.0, reset_and_prompt).start()
        
    def _format_winner_message(self, winners_money: Dict[str, List[Tuple[Player, Money]]]) -> str:
        """پیام نهایی اعلام برندگان را با فرمت زیبا تولید می‌کند."""
        if not winners_money:
            return "🏁 این دست بدون برنده پایان یافت."
        
        lines = ["🏁 **نتایج دست** 🏁"]
        for hand_name, plist in winners_money.items():
            lines.append(f"\n*{hand_name.upper()}*")
            for player, money in plist:
                lines.append(f"🏆 {player.mention_markdown} ➡️ برنده `{money}$` شد.")
        return "\n".join(lines)
        
    # --- متدهای مربوط به اکشن‌های بازیکن ---
    def call_check(self, update: Update, context: CallbackContext):
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        try:
            action_text = "چک کرد" if game.max_round_rate == player.round_rate else "کال کرد"
            self._round_rate.player_action_call_check(game, player)
            self._view.send_message(update.effective_chat.id, f"✅ {player.mention_markdown} {action_text}.")
            self._process_playing(update.effective_chat.id, game)
        except UserException as e:
            update.callback_query.answer(str(e), show_alert=True)
            
    def fold(self, update: Update, context: CallbackContext):
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        player.state = PlayerState.FOLD
        self._view.send_message(update.effective_chat.id, f"🏳️ {player.mention_markdown} فولد کرد.")
        self._process_playing(update.effective_chat.id, game)

    def raise_rate_bet(self, update: Update, context: CallbackContext, amount: int):
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        try:
            new_rate = self._round_rate.player_action_raise_bet(game, player, amount)
            action_text = "شرط بست" if game.max_round_rate == new_rate else "افزایش داد"
            self._view.send_message(update.effective_chat.id, f"💹 {player.mention_markdown} شرط را به {new_rate}$ {action_text}.")
            self._process_playing(update.effective_chat.id, game)
        except UserException as e:
            update.callback_query.answer(str(e), show_alert=True)

    def all_in(self, update: Update, context: CallbackContext):
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        try:
            total_bet = self._round_rate.player_action_all_in(game, player)
            self._view.send_message(update.effective_chat.id, f"💥 {player.mention_markdown} با تمام موجودی خود ({total_bet}$) آل-این کرد!")
            self._process_playing(update.effective_chat.id, game)
        except UserException as e:
            update.callback_query.answer(str(e), show_alert=True)

    # --- متدهای کمکی و جانبی ---
    def bonus(self, update: Update, context: CallbackContext):
        # این متد بدون تغییر باقی می‌ماند
        wallet = WalletManagerModel(update.effective_message.from_user.id, self._kv)
        chat_id = update.effective_chat.id
        message_id = update.effective_message.message_id

        if wallet.has_daily_bonus():
            self._view.send_message_reply(chat_id, message_id, f"💰 پولت: *{wallet.value()}$*\nشما جایزه امروز را گرفته‌اید.")
            return

        dice_msg = self._view.send_dice_reply(chat_id, message_id)
        bonus = BONUSES[dice_msg.dice.value - 1]
        icon = DICES[dice_msg.dice.value-1]
        
        def print_bonus():
            money = wallet.add_daily(amount=bonus)
            self._view.send_message_reply(
                chat_id, dice_msg.message_id,
                f"🎁 پاداش: *{bonus}$* {icon}\n💰 پولت: *{money}$*\n"
            )

        Timer(DICE_DELAY_SEC, print_bonus).start()

    def money(self, update: Update, context: CallbackContext):
        wallet = WalletManagerModel(update.effective_message.from_user.id, self._kv)
        self._view.send_message_reply(update.message.chat_id, update.message.message_id, f"💰 موجودی فعلی شما: *{wallet.value()}$*")

    def hide_cards(self, update: Update, context: CallbackContext):
        """کیبورد کارت‌ها را مخفی کرده و دکمه نمایش مجدد را نشان می‌دهد."""
        player_mention = update.effective_user.mention_markdown()
        self._view.show_reopen_keyboard(update.effective_chat.id, player_mention)

    def show_table(self, update: Update, context: CallbackContext):
        """وضعیت فعلی میز بازی را مجدداً ارسال می‌کند."""
        game = self._game_from_context(context)
        if game.state not in self.ACTIVE_GAME_STATES:
            self._view.send_message(update.effective_chat.id, "بازی در جریان نیست.")
            return
        
        caption = f"💰 پات فعلی: {game.pot}$"
        self._view.send_desk_cards_img(update.effective_chat.id, game.cards_table, caption)

    def send_cards_to_user(self, update: Update, context: CallbackContext):
        """در پاسخ به دکمه 'نمایش کارت'، کارت‌های کاربر را مجدد ارسال می‌کند."""
        game = self._game_from_context(context)
        player = next((p for p in game.players if p.user_id == update.effective_user.id), None)
        
        if player and player.cards:
            self._view.send_cards(
                chat_id=update.effective_chat.id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=update.effective_message.message_id
            )

class RoundRateModel:
    """
    این کلاس تمام منطق‌های پیچیده مربوط به شرط‌بندی، بلایندها، پات و محاسبه برندگان
    را مدیریت می‌کند تا کلاس اصلی PokerBotModel تمیزتر باقی بماند.
    """
    def __init__(self, view: PokerBotViewer):
        self._view = view

    def set_blinds(self, game: Game):
        """بلایند کوچک و بزرگ را در ابتدای دست (Pre-Flop) تنظیم می‌کند."""
        num_players = len(game.players)
        if num_players < 2:
            return # بلایندها فقط با حداقل دو بازیکن معنی دارند

        dealer_index = game.dealer_index

        # تعیین بازیکنان Small و Big Blind
        sb_player = game.players[(dealer_index + 1) % num_players]
        bb_player = game.players[(dealer_index + 2) % num_players]

        print(f"DEBUG: Dealer is {game.players[dealer_index].mention_markdown}")
        print(f"DEBUG: SB is {sb_player.mention_markdown}, BB is {bb_player.mention_markdown}")

        # پرداخت Small Blind
        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.wallet.authorize(game.id, sb_amount)
        sb_player.round_rate = sb_amount
        sb_player.total_bet = sb_amount
        if sb_player.wallet.value() == 0: sb_player.state = PlayerState.ALL_IN

        # پرداخت Big Blind
        bb_amount = min(SMALL_BLIND * 2, bb_player.wallet.value())
        bb_player.wallet.authorize(game.id, bb_amount)
        bb_player.round_rate = bb_amount
        bb_player.total_bet = bb_amount
        if bb_player.wallet.value() == 0: bb_player.state = PlayerState.ALL_IN

        game.pot = sb_amount + bb_amount
        game.max_round_rate = bb_amount
        game.last_raise = SMALL_BLIND # تفاوت بین BB و SB به عنوان اولین رِیز محسوب می‌شود

        print(f"DEBUG: Blinds posted. Pot: {game.pot}, Max Round Rate: {game.max_round_rate}")

    def player_action_call_check(self, game: Game, player: Player):
        """منطق حرکت Call یا Check را برای بازیکن اجرا می‌کند."""
        call_amount = game.max_round_rate - player.round_rate
        if call_amount > 0:
            # این یک Call است
            amount_to_pay = min(call_amount, player.wallet.value())
            player.wallet.authorize(game.id, amount_to_pay)
            player.round_rate += amount_to_pay
            player.total_bet += amount_to_pay
            game.pot += amount_to_pay
            if player.wallet.value() == 0:
                player.state = PlayerState.ALL_IN
        # اگر call_amount صفر باشد، این یک Check است و پولی رد و بدل نمی‌شود.
        player.has_acted = True

    def player_action_raise_bet(self, game: Game, player: Player, raise_amount: int) -> Money:
        """منطق حرکت Raise یا Bet را برای بازیکن اجرا می‌کند."""
        # حداقل مبلغ برای یک raise معتبر
        min_raise = game.last_raise if game.last_raise > 0 else SMALL_BLIND * 2
        
        # مبلغی که بازیکن می‌خواهد شرطش به آن برسد
        target_rate = game.max_round_rate + raise_amount
        
        # کل پولی که باید در این حرکت بپردازد
        amount_to_pay = target_rate - player.round_rate
        
        if raise_amount < min_raise and player.wallet.value() > amount_to_pay:
            raise UserException(f"مبلغ افزایش (Raise) باید حداقل {min_raise}$ باشد.")

        if player.wallet.value() < amount_to_pay:
            raise UserException("موجودی شما برای این افزایش کافی نیست.")
        
        player.wallet.authorize(game.id, amount_to_pay)
        player.round_rate += amount_to_pay
        player.total_bet += amount_to_pay
        game.pot += amount_to_pay

        game.last_raise = target_rate - game.max_round_rate
        game.max_round_rate = target_rate
        player.has_acted = True

        # پس از یک raise، همه بازیکنان فعال دیگر باید دوباره بازی کنند
        for p in game.players:
            if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                p.has_acted = False
        
        return target_rate

    def player_action_all_in(self, game: Game, player: Player) -> Money:
        """منطق حرکت All-in را برای بازیکن اجرا می‌کند."""
        all_in_amount = player.wallet.value()
        player.wallet.authorize(game.id, all_in_amount)
        player.round_rate += all_in_amount
        player.total_bet += all_in_amount
        game.pot += all_in_amount
        player.state = PlayerState.ALL_IN
        player.has_acted = True

        # اگر مبلغ آل-این او از بیشترین شرط فعلی بیشتر است، max_round_rate را آپدیت کن
        if player.round_rate > game.max_round_rate:
            game.last_raise = player.round_rate - game.max_round_rate
            game.max_round_rate = player.round_rate
            # بقیه بازیکنان فعال باید دوباره بازی کنند
            for p in game.players:
                if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                    p.has_acted = False

        return player.total_bet

    def collect_bets_for_pot(self, game: Game):
        """شرط‌های روی میز (round_rate) را جمع‌آوری و به پات اصلی منتقل می‌کند."""
        # در این مدل جدید، پول مستقیما به پات می‌رود، پس این متد فقط مقادیر را ریست می‌کند
        game.max_round_rate = 0
        game.last_raise = 0
        for p in game.players:
            p.round_rate = 0
            p.has_acted = False

    def finish_game_and_distribute_pot(
        self,
        game: Game,
        player_scores: Dict[Score, List[Tuple[Player, Cards]]],
    ) -> Dict[str, List[Tuple[Player, Money]]]:
        """
        پیچیده‌ترین بخش: پات را بر اساس Side Pot ها بین برندگان تقسیم می‌کند.
        """
        final_winnings: Dict[str, List[Tuple[Player, Money]]] = {}
        all_contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        # اگر فقط یک نفر باقی مانده (بقیه فولد کرده‌اند)
        if len(all_contenders) == 1:
            winner = all_contenders[0]
            winnings = game.pot
            winner.wallet.approve(game.id) # تایید تراکنش‌های موفق او
            # بقیه پول (اگر در پات مانده) باید به او برسد
            # در مدل جدید، تمام پول‌های شرط‌بندی شده از قبل در پات هستند
            # و wallet.inc لازم نیست، چون پول از کیف پول بقیه کم شده
            final_winnings["Winner by Fold"] = [(winner, winnings)]
            # لغو تراکنش بازیکنان فولد کرده
            for p in game.players_by(states=(PlayerState.FOLD,)):
                p.wallet.cancel(game.id)
            return final_winnings

        # مرتب‌سازی بازیکنان بر اساس میزان شرط‌بندی‌شان
        sorted_players = sorted(all_contenders, key=lambda p: p.total_bet)
        
        last_bet_level = 0
        
        while game.pot > 0:
            # پیدا کردن کمترین شرط در بین بازیکنان باقی‌مانده
            if not sorted_players: break
            
            lowest_bet = sorted_players[0].total_bet
            if lowest_bet <= last_bet_level:
                # این بازیکن قبلا در پات‌های قبلی محاسبه شده
                sorted_players.pop(0)
                continue

            current_pot_level = lowest_bet - last_bet_level
            side_pot = 0
            
            # ساختن یک ساید-پات
            eligible_for_this_pot = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN, PlayerState.FOLD))
            for p in eligible_for_this_pot:
                contribution = min(max(0, p.total_bet - last_bet_level), current_pot_level)
                side_pot += contribution

            if side_pot <= 0:
                break # پات تمام شده
            
            game.pot -= side_pot
            
            # پیدا کردن برنده(های) این ساید-پات
            pot_winners = []
            best_score = -1
            
            # تنها بازیکنانی که تا این سطح شرط بسته‌اند، واجد شرایطند
            pot_contenders = [p for p in all_contenders if p.total_bet >= lowest_bet]

            for score, player_list in player_scores.items():
                for p, _ in player_list:
                    if p in pot_contenders:
                        if best_score == -1: best_score = score
                        if score == best_score:
                            pot_winners.append(p)
            
            if not pot_winners: # نباید اتفاق بیفتد
                sorted_players.pop(0)
                continue

            # تقسیم ساید-پات بین برندگان
            win_share = side_pot // len(pot_winners)
            for winner in pot_winners:
                # پول مستقیما به کیف پول اضافه نمی‌شود، چون از کیف پول بقیه کم شده.
                # فقط تراکنش او را تایید می‌کنیم
                winner.wallet.approve(game.id)
                hand_name = self._hand_name_from_score(best_score)
                if hand_name not in final_winnings:
                    final_winnings[hand_name] = []
                
                # اضافه کردن به لیست برندگان
                found = False
                for i, (p, m) in enumerate(final_winnings[hand_name]):
                    if p.user_id == winner.user_id:
                        final_winnings[hand_name][i] = (p, m + win_share)
                        found = True
                        break
                if not found:
                    final_winnings[hand_name].append((winner, win_share))

            last_bet_level = lowest_bet
            sorted_players.pop(0)
        
        # لغو تراکنش‌های بازیکنان بازنده
        all_winners_id = {p.user_id for plist in final_winnings.values() for p, m in plist}
        for p in all_contenders:
            if p.user_id not in all_winners_id:
                p.wallet.cancel(game.id)

        return final_winnings

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"

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
            local current = tonumber(redis.call('GET', KEYS[1])) or 0
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

        result = self._LUA_DECR_IF_GE(keys=[self._val_key], args=[amount])
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
        amount_to_return = int(self._kv.hget(self._authorized_money_key, game_id) or 0)
        if amount_to_return > 0:
            self.inc(amount_to_return)
            self._kv.hdel(self._authorized_money_key, game_id)
