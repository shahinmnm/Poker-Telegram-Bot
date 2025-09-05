#!/usr/bin/env python3

import datetime
import traceback
from threading import Timer, Lock  # برای Lock و Timer
from typing import List, Tuple, Dict, Optional

import json
import inspect
import redis
from telegram import Message, ReplyKeyboardMarkup, Update, Bot, ParseMode
from telegram.ext import CallbackContext

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
)
from pokerapp.pokerbotview import PokerBotViewer

DICE_MULT = 10
DICE_DELAY_SEC = 5
BONUSES = (5, 20, 40, 80, 160, 320)
DICES = "⚀⚁⚂⚃⚄⚅"

KEY_CHAT_DATA_GAME = "game"
KEY_OLD_PLAYERS = "old_players"

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
        self._turn_lock = Lock()  # قفل برای مدیریت دسترسی همزمان به نوبت بازیکنان

    @property
    def _min_players(self):
        return 1 if self._cfg.DEBUG else MIN_PLAYERS

    def _game_from_context(self, context: CallbackContext) -> Game:
        if KEY_CHAT_DATA_GAME not in context.chat_data:
            game = Game()
            context.chat_data[KEY_CHAT_DATA_GAME] = game
        game = context.chat_data[KEY_CHAT_DATA_GAME]
        # تنظیم chat_id اگر موجود نباشد (برای رفع AttributeError)
        if not hasattr(game, 'chat_id') or game.chat_id is None:
            game.chat_id = None  # یا مقدار پیش‌فرض، اما در متدها از update استفاده می‌شود
        return game

    @staticmethod
    def _current_turn_player(game: Game) -> Optional[Player]:
        if game.current_player_index < 0:
            return None
        return game.get_player_by_seat(game.current_player_index)

    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
        """کیبورد مخصوص نمایش کارت‌های بازیکن و دکمه‌های کنترلی را می‌سازد."""
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

    def _cleanup_hand_messages(self, chat_id: ChatId, game: Game) -> None:
        """
        حذف متمرکز همه پیام‌های موقت جز پیام نتیجه و پیام پایان دست.
        """
        preserve_ids = set(filter(None, [
            game.last_hand_result_message_id,
            game.last_hand_end_message_id
        ]))

        for msg_id in list(game.message_ids_to_delete):
            if msg_id not in preserve_ids:
                self._view.remove_message(chat_id, msg_id)
        game.message_ids_to_delete.clear()

        if game.turn_message_id and game.turn_message_id not in preserve_ids:
            self._view.remove_markup(chat_id, game.turn_message_id)
        game.turn_message_id = None

        if game.last_hand_end_message_id and game.state == GameState.INITIAL:
            self._view.remove_message(chat_id, game.last_hand_end_message_id)
            game.last_hand_end_message_id = None

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
        self._view.send_message(
            chat_id=chat_id,
            text=f"کارت‌های {player_mention} پنهان شد. برای مشاهده دوباره از دکمه‌ها استفاده کن.",
            reply_markup=reopen_keyboard,
        )

    def send_cards(
        self,
        chat_id: ChatId,
        cards: Cards,
        mention_markdown: Mention,
        ready_message_id: Optional[MessageId],
    ) -> Optional[MessageId]:
        """ارسال کارت‌ها با کیبورد و مدیریت ریپلای."""
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
            return message.message_id
        except Exception as e:
            if 'message to be replied not found' in str(e).lower():
                message = self._bot.send_message(
                    chat_id=chat_id,
                    text="کارت‌های شما " + mention_markdown,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                )
                return message.message_id
            else:
                print(f"Error sending cards: {e}")
        return None

    def hide_cards(self, update: Update, context: CallbackContext) -> None:
        """پنهان کردن کارت‌ها و نمایش کیبورد بازگشتی."""
        chat_id = update.effective_chat.id
        user = update.effective_user
        self.show_reopen_keyboard(chat_id, user.mention_markdown())
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=5)

    def send_cards_to_user(self, update: Update, context: CallbackContext) -> None:
        """نمایش مجدد کارت‌ها برای بازیکن."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        current_player = next((p for p in game.players if p.user_id == user_id), None)
        if not current_player or not current_player.cards:
            self._view.send_message(chat_id, "شما در بازی فعلی حضور ندارید یا کارتی ندارید.")
            return

        cards_message_id = self.send_cards(
            chat_id=chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=None,
        )
        if cards_message_id:
            game.message_ids_to_delete.append(cards_message_id)

    def show_table(self, update: Update, context: CallbackContext) -> None:
        """نمایش کارت‌های میز."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state in self.ACTIVE_GAME_STATES and game.cards_table:
            self.add_cards_to_table(0, game, chat_id, "🃏 کارت‌های روی میز")
        else:
            msg_id = self._view.send_message_return_id(chat_id, "هنوز بازی شروع نشده یا کارتی روی میز نیست.")
            if msg_id:
                self._view.remove_message_delayed(chat_id, msg_id, 5)

    def ready(self, update: Update, context: CallbackContext) -> None:
        """اعلام آمادگی بازیکن."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user = update.effective_user

        if game.state != GameState.INITIAL:
            self._view.send_message(chat_id, "⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return

        if game.seated_count() >= MAX_PLAYERS:
            self._view.send_message(chat_id, "🚪 اتاق پر است!")
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < SMALL_BLIND * 2:
            self._view.send_message(chat_id, f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).")
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.message.message_id,
                seat_index=None,
            )
            game.ready_users.add(user.id)
            seat_assigned = game.add_player(player)
            if seat_assigned == -1:
                self._view.send_message(chat_id, "🚪 اتاق پر است!")
                return

        ready_list = "\n".join([
            f"{idx+1}. (صندلی {idx+1}) {p.mention_markdown} 🟢"
            for idx, p in enumerate(game.seats) if p
        ])
        text = (
            f"👥 *لیست بازیکنان آماده*\n\n{ready_list}\n\n"
            f"📊 {game.seated_count()}/{MAX_PLAYERS} بازیکن آماده\n\n"
            f"🚀 برای شروع بازی /start را بزنید یا منتظر بمانید."
        )

        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)
        if game.ready_message_main_id:
            try:
                self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=game.ready_message_main_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                )
            except Exception as e:
                print(f"Error editing ready message: {e}")
                game.ready_message_main_id = self._view.send_message(chat_id, text, reply_markup=keyboard)
        else:
            game.ready_message_main_id = self._view.send_message(chat_id, text, reply_markup=keyboard)

        if game.seated_count() >= self._min_players:
            self.start(update, context)

    def start(self, update: Update, context: CallbackContext) -> None:
        """شروع بازی."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state == GameState.FINISHED:
            game.reset()

        if game.state != GameState.INITIAL:
            self._view.send_message(chat_id, "⚠️ بازی قبلاً شروع شده است!")
            return

        if game.seated_count() < self._min_players and not self._cfg.DEBUG:
            self._view.send_message(chat_id, f"حداقل {self._min_players} بازیکن نیاز است!")
            return

        self._start_game(game, chat_id, context)

    def _start_game(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """راه‌اندازی بازی جدید."""
        if game.ready_message_main_id:
            self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None

        game.advance_dealer()
        self._view.send_message(chat_id, "🎮 بازی شروع شد!")

        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game, chat_id)
        self._round_rate.set_blinds(game, chat_id)

        context.chat_data[KEY_OLD_PLAYERS] = {p.user_id: p for p in game.players}

        self._start_next_turn(game, chat_id, context)  # پاس context

    def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        """تقسیم کارت‌ها به بازیکنان."""
        for player in game.players:
            player.cards = [game.remain_cards.pop(), game.remain_cards.pop()]

            try:
                UserPrivateChatModel(self._kv, player.user_id).send_message(
                    text=f"کارت‌های شما: {player.cards[0]} {player.cards[1]}",
                    image=self._view._desk_generator.generate_desk(player.cards)
                )
            except Exception as e:
                print(f"Error sending PV cards: {e}")
                self._view.send_message(chat_id, f"⚠️ مشکل در ارسال کارت‌ها به PV {player.mention_markdown}")

            cards_message_id = self.send_cards(
                chat_id=chat_id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )
            if cards_message_id:
                game.message_ids_to_delete.append(cards_message_id)

    def player_action_call_check(self, update: Update, context: CallbackContext) -> None:
        """اکشن کال/چک."""
        with self._turn_lock:
            game = self._game_from_context(context)
            chat_id = update.effective_chat.id  # مستقیم از update
            player = self._current_turn_player(game)
            if not player or player.user_id != update.effective_user.id:
                return

            call_amount = game.max_round_rate - player.round_rate
            if call_amount > 0:
                player.wallet.authorize(game.id, call_amount)
                player.round_rate += call_amount
                player.total_bet += call_amount
                game.pot += call_amount
                action_text = f"{player.mention_markdown} کال کرد ({call_amount}$)"
            else:
                action_text = f"{player.mention_markdown} چک کرد"

            player.has_acted = True
            self._view.send_message(chat_id, action_text)
            self._process_playing(game, chat_id, context)

    def player_action_fold(self, update: Update, context: CallbackContext) -> None:
        """اکشن فولد."""
        with self._turn_lock:
            game = self._game_from_context(context)
            chat_id = update.effective_chat.id
            player = self._current_turn_player(game)
            if not player or player.user_id != update.effective_user.id:
                return

            player.state = PlayerState.FOLD
            player.has_acted = True
            self._view.send_message(chat_id, f"{player.mention_markdown} فولد کرد")
            self._process_playing(game, chat_id, context)

    def player_action_raise_bet(self, update: Update, context: CallbackContext, raise_type: str) -> None:
        """اکشن ریز/بت."""
        with self._turn_lock:
            game = self._game_from_context(context)
            chat_id = update.effective_chat.id
            player = self._current_turn_player(game)
            if not player or player.user_id != update.effective_user.id:
                return

            raise_amount = int(raise_type)
            total_raise = game.max_round_rate + raise_amount - player.round_rate
            if total_raise > player.wallet.value():
                total_raise = player.wallet.value()

            player.wallet.authorize(game.id, total_raise)
            player.round_rate += total_raise
            player.total_bet += total_raise
            game.max_round_rate = player.round_rate
            game.pot += total_raise
            player.has_acted = True

            self._view.send_message(chat_id, f"{player.mention_markdown} ریز/بت کرد ({total_raise}$)")
            self._process_playing(game, chat_id, context)

    def player_action_all_in(self, update: Update, context: CallbackContext) -> None:
        """اکشن آل-این."""
        with self._turn_lock:
            game = self._game_from_context(context)
            chat_id = update.effective_chat.id
            player = self._current_turn_player(game)
            if not player or player.user_id != update.effective_user.id:
                return

            all_in_amount = player.wallet.authorize_all(game.id)
            player.round_rate += all_in_amount
            player.total_bet += all_in_amount
            game.pot += all_in_amount
            if player.round_rate > game.max_round_rate:
                game.max_round_rate = player.round_rate
            player.state = PlayerState.ALL_IN
            player.has_acted = True

            self._view.send_message(chat_id, f"{player.mention_markdown} آل-این کرد ({all_in_amount}$)")
            self._process_playing(game, chat_id, context)

    def _process_playing(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """پردازش بعد از اکشن."""
        if self._is_betting_round_over(game):
            self._advance_round(game, chat_id, context)
        else:
            self._start_next_turn(game, chat_id, context)

    def _is_betting_round_over(self, game: Game) -> bool:
        """بررسی پایان دور شرط‌بندی."""
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        all_in_players = game.players_by(states=(PlayerState.ALL_IN,))
        if len(active_players) <= 1 and not all_in_players:
            return True
        return all(p.has_acted for p in active_players) and game.all_in_players_are_covered()

    def _advance_round(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """پیشرفت به دور بعدی."""
        self._reset_round_flags(game)

        if game.state == GameState.ROUND_PRE_FLOP:
            self._go_to_next_street(game, chat_id, GameState.ROUND_FLOP, 3)
        elif game.state == GameState.ROUND_FLOP:
            self._go_to_next_street(game, chat_id, GameState.ROUND_TURN, 1)
        elif game.state == GameState.ROUND_TURN:
            self._go_to_next_street(game, chat_id, GameState.ROUND_RIVER, 1)
        elif game.state == GameState.ROUND_RIVER:
            self._end_hand(game, chat_id, context)  # پاس context
        self._start_next_turn(game, chat_id, context)  # پاس context

    def _reset_round_flags(self, game: Game) -> None:
        """ریست فلگ‌های دور."""
        for player in game.players:
            player.has_acted = False
            player.round_rate = 0
        game.max_round_rate = 0

    def _go_to_next_street(self, game: Game, chat_id: ChatId, next_state: GameState, num_cards: int) -> None:
        """انتقال به استریت بعدی و اضافه کردن کارت‌ها."""
        game.state = next_state
        new_cards = [game.remain_cards.pop() for _ in range(num_cards)]
        game.cards_table.extend(new_cards)
        self.add_cards_to_table(num_cards, game, chat_id, f"🃏 {num_cards} کارت جدید روی میز: {' '.join(map(str, new_cards))}")

    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, caption: str) -> None:
        """نمایش کارت‌های میز."""
        message = self._view.send_desk_cards_img(chat_id, game.cards_table, caption)
        if message:
            game.message_ids_to_delete.append(message.message_id)

    def _start_next_turn(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """شروع نوبت بعدی."""
        game.current_player_index = game.next_occupied_seat(game.current_player_index)
        player = self._current_turn_player(game)
        if not player:
            self._end_hand(game, chat_id, context)  # پاس context
            return

        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)

        money = player.wallet.value()
        game.turn_message_id = self._view.send_turn_actions(chat_id, game, player, money)

    def _end_hand(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """پایان دست با دریافت context."""
        self._cleanup_hand_messages(chat_id, game)
        self._showdown(game, chat_id)

        old_players = context.chat_data.get(KEY_OLD_PLAYERS, {})
        for user_id, player in old_players.items():
            player.wallet.approve(game.id)

        game.state = GameState.FINISHED
        game.reset()
        self._view.send_new_hand_ready_message(chat_id, game)

    def _showdown(self, game: Game, chat_id: ChatId) -> None:
        """نمایش نتایج showdown."""
        winners_by_pot = self._determine_winners(game)
        self._view.send_showdown_results(chat_id, game, winners_by_pot)

    def _determine_winners(self, game: Game) -> List[Dict]:
        """تعیین برندگان با side pots."""
        # پیاده‌سازی کامل برای side pots (بر اساس winnerdetermination)
        scores = {}
        for player in game.players_by((PlayerState.ACTIVE, PlayerState.ALL_IN)):
            hand = player.cards + game.cards_table
            score, hand_type, hand_cards = self._winner_determine.determine_winner(hand)
            scores[player] = (score, hand_type, hand_cards)

        # منطق side pots: مرتب‌سازی بازیکنان بر اساس total_bet و تقسیم پات
        sorted_players = sorted(scores.keys(), key=lambda p: p.total_bet)
        pots = []
        current_pot = 0
        prev_bet = 0
        for i, player in enumerate(sorted_players):
            pot_contribution = player.total_bet - prev_bet
            current_pot += pot_contribution * (len(sorted_players) - i)
            eligible_players = [p for p in sorted_players[i:] if p.state != PlayerState.FOLD]
            max_score = max(scores[p][0] for p in eligible_players)
            winners = [p for p in eligible_players if scores[p][0] == max_score]
            win_amount = current_pot // len(winners)
            pots.append({
                "amount": current_pot,
                "winners": [{"player": w, "hand_type": scores[w][1], "hand_cards": scores[w][2]} for w in winners]
            })
            for w in winners:
                w.wallet.inc(win_amount)
            prev_bet = player.total_bet
            current_pot = 0

        return pots

    # متدهای اضافی مانند bonus, stop, etc. (برای کامل بودن)
    def bonus(self, update: Update, context: CallbackContext) -> None:
        """پاداش روزانه."""
        user_id = update.effective_user.id
        wallet = WalletManagerModel(user_id, self._kv)
        if wallet.has_daily_bonus():
            amount = wallet.add_daily(100)  # مثال
            self._view.send_message(update.effective_chat.id, f"🎁 پاداش روزانه: {amount}$")
        else:
            self._view.send_message(update.effective_chat.id, "پاداش امروز قبلاً گرفته شده!")

    def stop(self, update: Update, context: CallbackContext) -> None:
        """توقف بازی."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        game.reset()
        self._view.send_message(chat_id, "🛑 بازی متوقف شد.")

    # کلاس کمکی برای RoundRateModel (برای blinds)
    class RoundRateModel:
        def __init__(self, view: PokerBotViewer, kv: redis.Redis, model: 'PokerBotModel'):
            self._view = view
            self._kv = kv
            self._model = model

        def set_blinds(self, game: Game, chat_id: ChatId) -> None:
            """تنظیم اسمال و بیگ بلایند."""
            game.small_blind_index = game.next_occupied_seat(game.dealer_index)
            game.big_blind_index = game.next_occupied_seat(game.small_blind_index)

            small_player = game.get_player_by_seat(game.small_blind_index)
            big_player = game.get_player_by_seat(game.big_blind_index)

            small_player.wallet.authorize(game.id, SMALL_BLIND)
            small_player.round_rate = SMALL_BLIND
            small_player.total_bet = SMALL_BLIND
            game.pot += SMALL_BLIND

            big_player.wallet.authorize(game.id, SMALL_BLIND * 2)
            big_player.round_rate = SMALL_BLIND * 2
            big_player.total_bet = SMALL_BLIND * 2
            game.pot += SMALL_BLIND * 2
            game.max_round_rate = SMALL_BLIND * 2

            self._view.send_message(chat_id, f"🪙 اسمال بلایند: {small_player.mention_markdown} ({SMALL_BLIND}$)\nبیگ بلایند: {big_player.mention_markdown} ({SMALL_BLIND * 2}$)")

# کلاس WalletManagerModel (پیاده‌سازی Wallet با Redis)
class WalletManagerModel(Wallet):
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv = kv

    @staticmethod
    def _prefix(id: int, suffix: str = "") -> str:
        return f"wallet:{id}:{suffix}"

    def add_daily(self, amount: Money) -> Money:
        key = self._prefix(self._user_id, "daily")
        if not self._kv.exists(key):
            self.inc(amount)
            self._kv.set(key, datetime.date.today().isoformat(), ex=86400)
            return amount
        return 0

    def has_daily_bonus(self) -> bool:
        key = self._prefix(self._user_id, "daily")
        return not self._kv.exists(key)

    def inc(self, amount: Money = 0) -> None:
        key = self._prefix(self._user_id)
        self._kv.incr(key, amount)

    def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        auth_key = self._prefix(self._user_id, f"auth:{game_id}")
        self._kv.incr(auth_key, amount)

    def authorized_money(self, game_id: str) -> Money:
        auth_key = self._prefix(self._user_id, f"auth:{game_id}")
        return int(self._kv.get(auth_key) or 0)

    def authorize(self, game_id: str, amount: Money) -> None:
        if self.value() < amount:
            raise UserException("موجودی کافی نیست!")
        self.inc(-amount)
        self.inc_authorized_money(game_id, amount)

    def authorize_all(self, game_id: str) -> Money:
        amount = self.value()
        self.authorize(game_id, amount)
        return amount

    def value(self) -> Money:
        key = self._prefix(self._user_id)
        return int(self._kv.get(key) or DEFAULT_MONEY)

    def approve(self, game_id: str) -> None:
        amount = self.authorized_money(game_id)
        self.inc(amount)
        auth_key = self._prefix(self._user_id, f"auth:{game_id}")
        self._kv.delete(auth_key)

    def cancel(self, game_id: str) -> None:
        amount = self.authorized_money(game_id)
        self.inc(-amount)
        auth_key = self._prefix(self._user_id, f"auth:{game_id}")
        self._kv.delete(auth_key)
