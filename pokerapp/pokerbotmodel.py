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
        if game.current_player_index < 0:
            return None
        return game.get_player_by_seat(game.current_player_index)

    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
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

    def _log_bet_change(player, amount, source):
        print(f"[DEBUG] {source}: {player.mention_markdown} bet +{amount}, total_bet={player.total_bet}, round_rate={player.round_rate}, pot={game.pot}")

    def show_reopen_keyboard(self, chat_id: ChatId, player_mention: Mention) -> None:
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
                return message.message_id
        except Exception as e:
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
        chat_id = update.effective_chat.id
        user = update.effective_user
        self._view.show_reopen_keyboard(chat_id, user.mention_markdown())
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=5)
    def send_cards_to_user(self, update: Update, context: CallbackContext) -> None:
        """
        کارت‌های بازیکن را با کیبورد مخصوص در گروه دوباره ارسال می‌کند.
        این متد زمانی فراخوانی می‌شود که بازیکن دکمه "نمایش کارت‌ها" را می‌زند.
        """
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        # پیدا کردن بازیکن در لیست فعلی
        current_player = next((p for p in game.players if p.user_id == user_id), None)

        if not current_player or not current_player.cards:
            self._view.send_message(chat_id, "شما در بازی فعلی حضور ندارید یا کارتی ندارید.")
            return

        # ارسال پیام با کیبورد کارت
        cards_message_id = self._view.send_cards(
            chat_id=chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=None,
        )
        if cards_message_id:
            game.message_ids_to_delete.append(cards_message_id)

        # حذف پیام دستور کاربر
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)
        
    def set_delete_manager(self, delete_manager):
        """
        اتصال مدیر حذف پیام‌ها به مدل و ویو.
        این امکان رو میده که مدل هم به این قابلیت دسترسی داشته باشه.
        """
        self._delete_manager = delete_manager
        # اگر View هم متد مشابه داره، بهش پاس می‌دیم
        if hasattr(self._view, "set_delete_manager"):
            self._view.set_delete_manager(delete_manager)
            
    def show_table(self, update: Update, context: CallbackContext):
        """نمایش کارت‌های روی میز بنا به درخواست بازیکن."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        # حذف پیام دستور
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)

        if game.state in self.ACTIVE_GAME_STATES and game.cards_table:
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

        if game.seated_count() >= MAX_PLAYERS:
            self._view.send_message_reply(chat_id, update.message.message_id, "🚪 اتاق پر است!")
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < SMALL_BLIND * 2:
            self._view.send_message_reply(chat_id, update.message.message_id, f"💸 موجودی شما کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).")
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.effective_message.message_id,
                seat_index=None,
            )
            game.ready_users.add(user.id)
            seat_assigned = game.add_player(player)
            if seat_assigned == -1:
                self._view.send_message_reply(chat_id, update.message.message_id, "🚪 اتاق پر است!")
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
                self._bot.edit_message_text(chat_id=chat_id, message_id=game.ready_message_main_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception:
                msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
                if msg:
                    game.ready_message_main_id = msg
        else:
            msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
            if msg:
                game.ready_message_main_id = msg

        if game.seated_count() >= self._min_players and (game.seated_count() == self._bot.get_chat_member_count(chat_id) - 1 or self._cfg.DEBUG):
            self._start_game(context, game, chat_id)

    def start(self, update: Update, context: CallbackContext) -> None:
        """شروع دستی بازی."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            self._view.send_message(chat_id, "🎮 یک بازی در حال حاضر در جریان است.")
            return

        if game.state == GameState.FINISHED:
            game.reset()
            old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])

        if game.seated_count() >= self._min_players:
            self._start_game(context, game, chat_id)
        else:
            self._view.send_message(chat_id, f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).")

    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""
        if game.ready_message_main_id:
            # حذف پیام لیست بازیکنان آماده
            self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None

        # اگر dealer_index وجود نداشته باشد، مقدار اولیه بده
        if not hasattr(game, 'dealer_index'):
            game.dealer_index = -1
        # گردش دیلر بین صندلی‌های پر
        game.dealer_index = (game.dealer_index + 1) % game.seated_count()

        self._view.send_message(chat_id, '🚀 !بازی شروع شد!')

        # شروع مرحله پیش‌فلاپ
        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game, chat_id)

        # تعیین big blind / small blind و نوبت بازیکن اول
        self._round_rate.set_blinds(game, chat_id)

        # ذخیره شناسه‌های بازیکنان حاضر برای دست بعدی
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]

    def _divide_cards(self, game: Game, chat_id: ChatId):
        """
        کارت‌ها را بین بازیکنان پخش می‌کند:
          1. ارسال کارت‌ها به PV بازیکن
          2. ارسال پیام کارت‌ها با کیبورد در گروه
        """
        for player in game.seated_players():
            if len(game.remain_cards) < 2:
                self._view.send_message(chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود.")
                game.reset()
                return

            # گرفتن دو کارت و افزودن به لیست کارت‌های بازیکن
            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

            # --- ارسال تصویر کارت‌ها در پیام خصوصی ---
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
                    text=f"⚠️ {player.mention_markdown}، لطفاً ربات را استارت کن (/start) تا کارت‌ها را PV ببینی.",
                    parse_mode="Markdown"
                )

            # --- ارسال پیام با کیبورد کارت‌ها در گروه ---
            cards_message_id = self._view.send_cards(
                chat_id=chat_id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )
            if cards_message_id:
                game.message_ids_to_delete.append(cards_message_id)

    def _is_betting_round_over(self, game: Game) -> bool:
        """
        بررسی پایان یک دور شرط‌بندی:
          1. همه بازیکنان فعال حداقل یک حرکت انجام داده باشند.
          2. مقدار شرط همه برابر باشد.
        """
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            return True

        # آیا همه حرکت کرده‌اند؟
        if not all(p.has_acted for p in active_players):
            return False

        # آیا مقدار شرط برابر است؟
        reference_rate = active_players[0].round_rate
        if not all(p.round_rate == reference_rate for p in active_players):
            return False

        return True

    def _determine_winners(self, game: Game, contenders: list[Player]):
        """
        تعیین برندگان با پشتیبانی از Side Pot.
        خروجی: لیستی از پات‌ها و سهم برندگان.
        """
        if not contenders or game.pot == 0:
            return []

        # ۱. قدرت دست هر بازیکن
        contender_details = []
        for player in contenders:
            hand_type, score, best_hand_cards = self._winner_determine.get_hand_value(
                player.cards, game.cards_table
            )
            contender_details.append({
                "player": player,
                "total_bet": player.total_bet,
                "score": score,
                "hand_cards": best_hand_cards,
                "hand_type": hand_type,
            })

        # ۲. لایه‌بندی شرط‌ها (برای Side Pot)
        bet_tiers = sorted(list(set(p['total_bet'] for p in contender_details if p['total_bet'] > 0)))

        winners_by_pot = []
        last_bet_tier = 0
        calculated_pot_total = 0

        # ۳. ساخت پات‌ها به ترتیب لایه‌ها
        for tier in bet_tiers:
            tier_contribution = tier - last_bet_tier
            eligible_for_this_pot = [p for p in contender_details if p['total_bet'] >= tier]

            pot_size = tier_contribution * len(eligible_for_this_pot)
            calculated_pot_total += pot_size

            if pot_size > 0:
                best_score_in_pot = max(p['score'] for p in eligible_for_this_pot)
                pot_winners_info = [
                    {
                        "player": p['player'],
                        "hand_cards": p['hand_cards'],
                        "hand_type": p['hand_type'],
                    }
                    for p in eligible_for_this_pot if p['score'] == best_score_in_pot
                ]
                winners_by_pot.append({
                    "amount": pot_size,
                    "winners": pot_winners_info
                })
            if pot_size > 0:
                best_score_in_pot = max(p['score'] for p in eligible_for_this_pot)
                pot_winners_info = [
                    {
                        "player": p['player'],
                        "hand_cards": p['hand_cards'],
                        "hand_type": p['hand_type'],
                    }
                    for p in eligible_for_this_pot if p['score'] == best_score_in_pot
                ]
                winners_by_pot.append({
                    "amount": pot_size,
                    "winners": pot_winners_info
                })
    
            last_bet_tier = tier
    
        # --- فیکس: تطبیق مقدار پات با واقعیت ---
        discrepancy = game.pot - calculated_pot_total
        if discrepancy > 0 and winners_by_pot:
            winners_by_pot[0]['amount'] += discrepancy
        elif discrepancy < 0:
            print(f"[ERROR] Pot mismatch! Game pot: {game.pot}, Calculated: {calculated_pot_total}")
    
        # --- فیکس ۲: ادغام پات های غیرضروری ---
        if len(bet_tiers) == 1 and len(winners_by_pot) > 1:
            print("[INFO] Merging unnecessary side pots.")
            main_pot = {"amount": game.pot, "winners": winners_by_pot[0]['winners']}
            return [main_pot]
    
        return winners_by_pot
    
    def _process_playing(self, chat_id: ChatId, game: Game, context: CallbackContext) -> None:
        """کنترل اصلی گردش نوبت و تصمیم‌گیری پیشروی بازی."""
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None
    
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            self._go_to_next_street(game, chat_id, context)
            return
    
        if self._is_betting_round_over(game):
            self._go_to_next_street(game, chat_id, context)
            return
    
        next_index = self._round_rate._find_next_active_player_index(game, game.current_player_index)
        if next_index != -1:
            game.current_player_index = next_index
            player = game.players[next_index]
            self._send_turn_message(game, player, chat_id)
        else:
            self._go_to_next_street(game, chat_id, context)
    
    def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """ارسال پیام نوبت به بازیکن."""
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
        money = player.wallet.value()
        msg_id = self._view.send_turn_actions(chat_id, game, player, money)
        if msg_id:
            game.turn_message_id = msg_id
        game.last_turn_time = datetime.datetime.now()
    
    # --- Player Action Handlers ---
    
    def player_action_fold(self, update: Update, context: CallbackContext, game: Game) -> None:
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        chat_id = update.effective_chat.id
        current_player.state = PlayerState.FOLD
        self._view.send_message(chat_id, f"🏳️ {current_player.mention_markdown} فولد کرد.")
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
        self._process_playing(chat_id, game, context)
    
    def player_action_call_check(self, update: Update, context: CallbackContext, game: Game) -> None:
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        chat_id = update.effective_chat.id
        call_amount = game.max_round_rate - current_player.round_rate
        current_player.has_acted = True
        try:
            if call_amount > 0:
                current_player.wallet.authorize(game.id, call_amount)
                current_player.round_rate += call_amount
                current_player.total_bet += call_amount
                game.pot += call_amount
                self._view.send_message(chat_id, f"🎯 {current_player.mention_markdown} با {call_amount}$ کال کرد.")
            else:
                self._view.send_message(chat_id, f"✋ {current_player.mention_markdown} چک کرد.")
        except UserException as e:
            self._view.send_message(chat_id, f"⚠️ خطا: {e}")
            return
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
        self._process_playing(chat_id, game, context)
    
    def player_action_raise_bet(self, update: Update, context: CallbackContext, game: Game, raise_amount: int) -> None:
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        chat_id = update.effective_chat.id
        call_amount = game.max_round_rate - current_player.round_rate
        total_to_bet = call_amount + raise_amount
        try:
            current_player.wallet.authorize(game.id, total_to_bet)
            current_player.round_rate += total_to_bet
            current_player.total_bet += total_to_bet
            game.pot += total_to_bet
            game.max_round_rate = current_player.round_rate
            action = "بِت" if call_amount == 0 else "رِیز"
            self._view.send_message(chat_id, f"💹 {current_player.mention_markdown} {action} زد و شرط رو به {current_player.round_rate}$ رسوند.")
            game.trading_end_user_id = current_player.user_id
            current_player.has_acted = True
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False
        except UserException as e:
            self._view.send_message(chat_id, f"⚠️ خطا: {e}")
            return
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
        self._process_playing(chat_id, game, context)
    
    def player_action_all_in(self, update: Update, context: CallbackContext, game: Game) -> None:
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        chat_id = update.effective_chat.id
        all_in_amount = player.wallet.value()
        if all_in_amount <= 0:
            self._view.send_message(chat_id, f"👀 {current_player.mention_markdown} موجودی ندارد، چک می‌کند.")
            self.player_action_call_check(update, context, game)
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
            game.trading_end_user_id = current_player.user_id
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
        self._process_playing(chat_id, game, context)
    
    def _go_to_next_street(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """انتقال بازی به Street بعدی یا Showdown."""
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None
    
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            self._showdown(game, chat_id, context)
            return
    
        self._round_rate.collect_bets_for_pot(game)
        for p in game.players:
            p.has_acted = False
    def _go_to_next_street(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        بازی را به مرحله بعدی (street) می‌برد.
        """
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None

        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            self._showdown(game, chat_id, context)
            return

        # بستن این دور شرط‌بندی و آوردن شرط‌ها به پات اصلی
        self._round_rate.collect_bets_for_pot(game)
        for p in game.players:
            p.has_acted = False

        # Street بعدی
        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            self.add_cards_to_table(3, game, chat_id, "🃏 فلاپ (Flop)")
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            self.add_cards_to_table(1, game, chat_id, "🃏 ترن (Turn)")
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            self.add_cards_to_table(1, game, chat_id, "🃏 ریور (River)")
        elif game.state == GameState.ROUND_RIVER:
            self._showdown(game, chat_id, context)
            return

        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            self._go_to_next_street(game, chat_id, context)
            return

        try:
            game.current_player_index = self._get_first_player_index(game)
        except AttributeError:
            # fallback گرفتن اولین بازیکن فعال بعد از دیلر
            start_index = (game.dealer_index + 1) % game.seated_count()
            game.current_player_index = next(
                (idx for idx in range(start_index, start_index + game.seated_count())
                 if game.players[idx % game.seated_count()].state == PlayerState.ACTIVE),
                -1
            )

        if game.current_player_index != -1:
            self._process_playing(chat_id, game, context)
        else:
            self._go_to_next_street(game, chat_id, context)

    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, street_name: str):
        """افزودن کارت‌ها به میز و ارسال تصویر میز"""
        if count > 0:
            for _ in range(count):
                if game.remain_cards:
                    game.cards_table.append(game.remain_cards.pop())

        if not game.cards_table:
            msg_id = self._view.send_message_return_id(chat_id, "هنوز کارتی روی میز نیامده است.")
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
                self._view.remove_message_delayed(chat_id, msg_id, 5)
            return

        cards_str = "  ".join(game.cards_table)
        caption = f"{street_name}\n{cards_str}"

        msg = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=caption,
        )
        if msg:
            game.message_ids_to_delete.append(msg.message_id)

    def _showdown(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        مرحله پایانی دست: تعیین برنده‌ها و تقسیم پات‌ها
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        if not contenders:
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if len(active_players) == 1:
                winner = active_players[0]
                winner.wallet.inc(game.pot)
                self._view.send_message(
                    chat_id,
                    f"🏆 تمام بازیکنان دیگر فولد کردند! {winner.mention_markdown} برنده {game.pot}$ شد."
                )
        else:
            winners_by_pot = self._determine_winners(game, contenders)

            if winners_by_pot:
                for pot in winners_by_pot:
                    pot_amount = pot.get("amount", 0)
                    winners_info = pot.get("winners", [])

                    if pot_amount > 0 and winners_info:
                        win_amount_per_player = pot_amount // len(winners_info)
                        for winner in winners_info:
                            player = winner["player"]
                            player.wallet.inc(win_amount_per_player)
            else:
                self._view.send_message(chat_id, "ℹ️ هیچ برنده‌ای مشخص نشد.")

            self._view.send_showdown_results(chat_id, game, winners_by_pot)

        # پاکسازی پیام‌های این دست
        for msg_id in game.message_ids_to_delete:
            self._view.remove_message(chat_id, msg_id)
        game.message_ids_to_delete.clear()

        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None

        # ذخیره بازیکنانی که هنوز پول دارند برای دست بعد
        remaining_players = [p for p in game.players if p.wallet.value() > 0]
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in remaining_players]

        # ریست کامل بازی
        game.reset()
        self._view.send_new_hand_ready_message(chat_id)

    def _end_hand(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        پاکسازی و ریست دست به شکل کامل
        """
        for message_id in set(game.message_ids_to_delete):
            try:
                context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                print(f"INFO: Could not delete message {message_id} in chat {chat_id}. Reason: {e}")

        if game.turn_message_id:
            try:
                context.bot.delete_message(chat_id=chat_id, message_id=game.turn_message_id)
            except Exception as e:
                print(f"INFO: Could not delete turn message {game.turn_message_id}. Reason: {e}")

        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players if p.wallet.value() > 0]
        context.chat_data[KEY_CHAT_DATA_GAME] = Game()

        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)
        context.bot.send_message(
            chat_id=chat_id,
            text="🎉 دست تمام شد! برای شروع دست بعدی، /ready بزنید یا منتظر بمانید تا کسی /start کند.",
            reply_markup=keyboard
        )

    def _format_cards(self, cards: Cards) -> str:
        """
        کارت‌ها را با فرمت ثابت و زیبای Markdown برمی‌گرداند.
        بین کارت‌ها دو فاصله قرار می‌دهیم تا چینش مرتب باشد.
        """
        if not cards:
            return "??  ??"
        return "  ".join(str(card) for card in cards)
class RoundRateModel:
    def __init__(self, view: PokerBotViewer, kv: redis.Redis, model: 'PokerBotModel'):
        self._view = view
        self._kv = kv
        self._model = model

    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        """پیدا کردن بازیکن فعال بعدی از ایندکس داده شده."""
        n = len(game.players)
        for offset in range(1, n + 1):
            idx = (start_index + offset) % n
            p = game.players[idx]
            if p.state in (PlayerState.ACTIVE, ):
                return idx
        return -1

    def _get_first_player_index(self, game: Game) -> int:
        return self._find_next_active_player_index(game, game.dealer_index + 2)

    def set_blinds(self, game: Game, chat_id: ChatId) -> None:
        """تعیین بلایند کوچک و بزرگ و آپدیت نوبت بازی."""
        if game.seated_count() < 2:
            return

        sb_index = (game.dealer_index + 1) % game.seated_count()
        bb_index = (game.dealer_index + 2) % game.seated_count()

        self._set_player_blind(game, sb_index, SMALL_BLIND)
        self._set_player_blind(game, bb_index, SMALL_BLIND * 2)

        # اولین بازیکن برای شروع اکشن
        game.current_player_index = self._get_first_player_index(game)

        # ارسال پیام آغاز نوبت
        player = game.players[game.current_player_index]
        msg_id = self._model._send_turn_message(game, player, chat_id)
        if msg_id:
            game.message_ids_to_delete.append(msg_id)

    def _set_player_blind(self, game: Game, player_index: int, amount: Money) -> None:
        """برداشت مبلغ بلایند از بازیکن و افزودن به پات."""
        player = game.players[player_index]
        blind_amount = min(amount, player.wallet.value())  # all-in در صورت کمبود
        player.wallet.dec(blind_amount)
        player.round_rate += blind_amount
        player.total_bet += blind_amount
        game.pot += blind_amount
        game.max_round_rate = max(game.max_round_rate, player.round_rate)

    def collect_bets_for_pot(self, game: Game) -> None:
        """انتقال همه شرط‌های جاری به پات و ریست دور."""
        for p in game.players:
            game.pot += p.round_rate
            p.round_rate = 0
class WalletManagerModel(Wallet):
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv = kv
        self._key = f"wallet:{user_id}"
        self._bonus_key = f"wallet_bonus:{user_id}"
        self._auth_key = f"wallet_authorized:{user_id}"

    def value(self) -> Money:
        val = self._kv.get(self._key)
        return int(val) if val else 0

    def inc(self, amount: Money) -> None:
        self._kv.incrby(self._key, amount)

    def dec(self, amount: Money) -> bool:
        """Atomic decrement with Redis script to avoid race conditions."""
        lua = """
        local balance = redis.call('GET', KEYS[1])
        if not balance then return 0 end
        balance = tonumber(balance)
        if balance >= tonumber(ARGV[1]) then
            redis.call('DECRBY', KEYS[1], ARGV[1])
            return 1
        else
            return 0
        end
        """
        ok = self._kv.eval(lua, 1, self._key, amount)
        return bool(ok)

    def has_daily_bonus(self) -> bool:
        return not self._kv.exists(self._bonus_key)

    def add_daily(self, amount: Money) -> bool:
        """Adds daily bonus if not already claimed in last 24h."""
        if self._kv.setnx(self._bonus_key, 1):
            self._kv.expire(self._bonus_key, 86400)
            self.inc(amount)
            return True
        return False

    def authorize(self, amount: Money) -> bool:
        """Reserve money for a pending action."""
        if self.dec(amount):
            self._kv.incrby(self._auth_key, amount)
            return True
        return False

    def approve(self) -> None:
        """Finalize an authorized transaction."""
        self._kv.delete(self._auth_key)

    def cancel(self) -> None:
        """Cancel an authorized transaction and refund."""
        reserved = self._kv.get(self._auth_key)
        if reserved:
            self.inc(int(reserved))
        self._kv.delete(self._auth_key)

