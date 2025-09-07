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

    def __init__(self, view: PokerBotViewer, bot: Bot, cfg: Config, kv: redis.Redis, mdm=None):

        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._cfg: Config = cfg
        self._kv: redis.Redis = kv
        self._mdm = mdm  # ← اضافه شد
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
        """کیبورد مخصوص نمایش کارت‌های بازیکن و دکمه‌های کنترلی را می‌سازد."""
        # این دکمه‌ها برای مدیریت کیبورد توسط بازیکن استفاده می‌شوند
        hide_cards_button_text = "🙈 پنهان کردن کارت‌ها"
        show_table_button_text = "👁️ نمایش میز" # این دکمه را هم اضافه می‌کنیم
        return ReplyKeyboardMarkup(
            keyboard=[
                cards, # <-- ردیف اول: خود کارت‌ها
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
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        current_player = None
        for p in game.players:
            if p.user_id == user_id:
                current_player = p
                break
        
        if not current_player or not current_player.cards:
            self._view.send_message(chat_id, "شما در بازی فعلی حضور ندارید یا کارتی ندارید.")
            return
        cards_message_id = self._view.send_cards(
            chat_id=chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=None, # <-- چون این یک نمایش مجدد است، ریپلای نمی‌زنیم.
        )
        if cards_message_id:
            game.message_ids_to_delete.append(cards_message_id)
        
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)
        
    def show_table(self, update: Update, context: CallbackContext):
        """کارت‌های روی میز را به درخواست بازیکن با فرمت جدید نمایش می‌دهد."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

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
            self._view.send_message_reply(chat_id, update.message.message_id, f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).")
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.effective_message.message_id, # <-- کد صحیح
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
            except Exception: # اگر ویرایش نشد، یک پیام جدید بفرست
                msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
                if msg: game.ready_message_main_id = msg
        else:
            msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
            if msg: game.ready_message_main_id = msg

        if game.seated_count() >= self._min_players and (game.seated_count() == self._bot.get_chat_member_count(chat_id) - 1 or self._cfg.DEBUG):
            self._start_game(context, game, chat_id)

    def start(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
    
        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            self._view.send_message(chat_id, "🎮 یک بازی در حال حاضر در جریان است.")
            return
    
        if game.state == GameState.FINISHED:
            try:
                if self._mdm:
                    logging.debug("[MDM] delete_by_tag start_next_hand")
                    self._mdm.delete_by_tag(
                        game_id=game.id,
                        hand_id=None,
                        tag="start_next_hand",
                        include_protected=True,
                        reason="start_new_hand"
                    )
                    logging.debug("[MDM] purge_context prev hand")
                    self._mdm.purge_context(
                        game_id=game.id,
                        hand_id=None,
                        include_protected=False,
                        reason="start_new_hand_purge_prev"
                    )
            except Exception as e:
                logging.info(f"[MDM] cleanup prev hand failed: {e}")
    
            game.reset()
            old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
    
        if game.seated_count() >= self._min_players:
            self._start_game(context, game, chat_id)
        else:
            self._view.send_message(chat_id, f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).")


    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        if game.ready_message_main_id:
            self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None
    
        if not hasattr(game, 'dealer_index'):
            game.dealer_index = -1
        game.dealer_index = (game.dealer_index + 1) % game.seated_count()
    
        self._view.send_message(chat_id, '🚀 بازی شروع شد!')
    
        game.state = GameState.ROUND_PRE_FLOP
        self._round_rate.set_blinds(game, chat_id)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]


    def _divide_cards(self, game: Game, chat_id: ChatId):
        for player in game.seated_players():
            if len(game.remain_cards) < 2:
                self._view.send_message(chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود.")
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

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

            cards_message_id = self._view.send_cards(
                chat_id=chat_id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )
            if cards_message_id:
                game.message_ids_to_delete.append(cards_message_id)
            
    def _is_betting_round_over(self, game: Game) -> bool:
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
    
        if not active_players:
            return True

        if not all(p.has_acted for p in active_players):
            return False

        reference_rate = active_players[0].round_rate
        if not all(p.round_rate == reference_rate for p in active_players):
            return False
    
        return True

    def _determine_winners(self, game: Game, contenders: list[Player]):

        if not contenders or game.pot == 0:
            return []

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

        bet_tiers = sorted(list(set(p['total_bet'] for p in contender_details if p['total_bet'] > 0)))

        winners_by_pot = []
        last_bet_tier = 0
        calculated_pot_total = 0 # برای پیگیری مجموع پات محاسبه شده

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

            last_bet_tier = tier

        discrepancy = game.pot - calculated_pot_total
        if discrepancy > 0 and winners_by_pot:
            winners_by_pot[0]['amount'] += discrepancy
        elif discrepancy < 0:
            print(f"[ERROR] Pot calculation mismatch! Game pot: {game.pot}, Calculated: {calculated_pot_total}")

        if len(bet_tiers) == 1 and len(winners_by_pot) > 1:
            print("[INFO] Merging unnecessary side pots into a single main pot.")
            main_pot = {"amount": game.pot, "winners": winners_by_pot[0]['winners']}
            return [main_pot]
            
        return winners_by_pot

    def _process_playing(self, chat_id: ChatId, game: Game, context: CallbackContext) -> None:

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

        next_player_index = self._round_rate._find_next_active_player_index(game, game.current_player_index)
    
        if next_player_index != -1:
            game.current_player_index = next_player_index
            player = game.players[next_player_index]
    
            self._send_turn_message(game, player, chat_id)
        else:
            self._go_to_next_street(game, chat_id, context)

    def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)

        money = player.wallet.value()
        
        msg_id = self._view.send_turn_actions(chat_id, game, player, money)
        
        if msg_id:
            game.turn_message_id = msg_id
        game.last_turn_time = datetime.datetime.now()
    
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
        """بازیکن کال (پرداخت) یا چک (عبور) را انجام می‌دهد."""
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
            self._view.send_message(chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}")
            return 
    
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
    
        self._process_playing(chat_id, game, context)
    
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
    
            game.max_round_rate = current_player.round_rate
            action_text = "بِت" if call_amount == 0 else "رِیز"
            self._view.send_message(chat_id, f"💹 {current_player.mention_markdown} {action_text} زد و شرط رو به {current_player.round_rate}$ رسوند.")
    

            game.trading_end_user_id = current_player.user_id
            current_player.has_acted = True
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False
    
        except UserException as e:
            self._view.send_message(chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}")
            return
    
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)
    
        self._process_playing(chat_id, game, context)
    
    def player_action_all_in(self, update: Update, context: CallbackContext, game: Game) -> None:
        current_player = self._current_turn_player(game)
        if not current_player:
            return
    
        chat_id = update.effective_chat.id
        all_in_amount = current_player.wallet.value()
    
        if all_in_amount <= 0:
            self._view.send_message(chat_id, f"👀 {current_player.mention_markdown} موجودی برای آل-این ندارد و چک می‌کند.")
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
            print("WARNING: _get_first_player_index() not found. Using fallback logic.")
            first_player_index = -1
            start_index = (game.dealer_index + 1) % game.seated_count()
            for i in range(game.seated_count()):
                idx = (start_index + i) % game.seated_count()
                if game.players[idx].state == PlayerState.ACTIVE:
                    first_player_index = idx
                    break
            game.current_player_index = first_player_index
    
        if game.current_player_index != -1:
            self._process_playing(chat_id, game, context)
        else:
            self._go_to_next_street(game, chat_id, context)

    def _determine_all_scores(self, game: Game) -> List[Dict]:
        player_scores = []
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        
        if not contenders:
            return []

        for player in contenders:
            if not player.cards:
                continue
            try:
                score, best_hand, hand_type = self._winner_determine.get_hand_value_and_type(player.cards, game.cards_table)
            except AttributeError:

                print("WARNING: 'get_hand_value_and_type' not found in WinnerDetermination. Update winnerdetermination.py")
                score, best_hand = self._winner_determine.get_hand_value(player.cards, game.cards_table)
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

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            # Replacing underscore with space and title-casing the output
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"
            
    def _clear_game_messages(self, game: Game, chat_id: ChatId) -> None:
        """
        تمام پیام‌های مربوط به این دست از بازی، از جمله پیام نوبت فعلی
        و سایر پیام‌های ثبت‌شده را پاک می‌کند تا چت برای نمایش نتایج تمیز شود.
        """
        print(f"DEBUG: Clearing game messages...")
    
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None
    
        for message_id in list(game.message_ids_to_delete):
            self._view.remove_message(chat_id, message_id)
    
        game.message_ids_to_delete.clear()
        
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None

        
    def _showdown(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        فرآیند پایان دست را با استفاده از خروجی دقیق _determine_winners مدیریت می‌کند.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        if not contenders:
            # سناریوی نادر که همه قبل از showdown فولد کرده‌اند
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if len(active_players) == 1:
                winner = active_players[0]
                winner.wallet.inc(game.pot)
                self._view.send_message(
                    chat_id,
                    f"🏆 تمام بازیکنان دیگر فولد کردند! {winner.mention_markdown} برنده {game.pot}$ شد."
                )
        else:
            # ۱. تعیین برندگان و تقسیم تمام پات‌ها (اصلی و فرعی)
            winners_by_pot = self._determine_winners(game, contenders)

            if winners_by_pot:
                # این حلقه روی تمام پات‌های ساخته شده (اصلی و فرعی) حرکت می‌کند
                for pot in winners_by_pot:
                    pot_amount = pot.get("amount", 0)
                    winners_info = pot.get("winners", [])
                    
                    if pot_amount > 0 and winners_info:
                        win_amount_per_player = pot_amount // len(winners_info)
                        for winner in winners_info:
                            player = winner["player"]
                            player.wallet.inc(win_amount_per_player)
            else:
                 self._view.send_message(chat_id, "ℹ️ هیچ برنده‌ای در این دست مشخص نشد. مشکلی در منطق بازی رخ داده است.")


            # ۲. فراخوانی View برای نمایش نتایج
            # View باید آپدیت شود تا این ساختار داده جدید را به زیبایی نمایش دهد
            self._view.send_showdown_results(chat_id, game, winners_by_pot)

        # ۳. پاکسازی و ریست کردن بازی برای دست بعدی (بدون تغییر)
        for msg_id in game.message_ids_to_delete:
            self._view.remove_message(chat_id, msg_id)
        game.message_ids_to_delete.clear()

        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None

        remaining_players = [p for p in game.players if p.wallet.value() > 0]
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in remaining_players]

        game.reset()

        self._view.send_new_hand_ready_message(chat_id)
    def send_hand_result(self, chat_id, result_text, *, game):
        result_text = self._sanitize_text(result_text)
        try:
            msg = self._bot.send_message(
                chat_id=chat_id,
                text=result_text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            if msg and self._mdm:
                self._mdm.register(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    game_id=game.id,
                    hand_id=game.hand_id,
                    tag="RESULT",  # برای پیام نتایج
                    protected=True,  # پیام نتایج محافظت شده است
                    ttl=None
                )
            return msg.message_id if msg else None
        except Exception as e:
            logging.error(f"Error sending hand result: {e}")
        return None

    def purge_hand_messages(self, *, game):
        try:
            if not self._mdm:
                return 0
            return self._mdm.purge_context(
                game_id=game.id,
                hand_id=game.hand_id,
                include_protected=False  # تنها پیام‌های غیرprotected پاک شوند
            )
        except Exception as e:
            logging.error(f"Error purging hand messages: {e}")
        return 0

    def _end_hand(self, game, chat_id, context):
        try:
            if hasattr(self._view, "purge_hand_messages"):
                logging.debug("[MDM] purge_hand_messages called")
                self._view.purge_hand_messages(game=game)
            elif self._mdm:
                logging.debug("[MDM] purge_context called")
                self._mdm.purge_context(game_id=game.id, hand_id=game.hand_id, include_protected=False, reason="purge_ctx")
        except Exception as e:
            logging.info(f"[MDM] purge failed: {e}")
        
        try:
            # Purge the chat for all non-protected messages
            if self._mdm:
                self._mdm.purge_chat(chat_id=chat_id, include_protected=False, reason="purge_chat_end_hand")
        except Exception as e:
            logging.info(f"[MDM] purge chat failed: {e}")
        
        try:
            context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players if p.wallet.value() > 0]
        except Exception as e:
            logging.info(f"[MDM] save old players failed: {e}")
        
        try:
            start_next_ttl = getattr(self._cfg, "START_NEXT_TTL_SECONDS", None)
            if hasattr(self._view, "send_start_next_hand"):
                logging.debug("[MDM] send_start_next_hand")
                self._view.send_start_next_hand(chat_id=chat_id, game=game, ttl=start_next_ttl)
        except Exception as e:
            logging.info(f"[MDM] send start-next failed: {e}")
        
        context.chat_data[KEY_CHAT_DATA_GAME] = Game()



    def _format_cards(self, cards: Cards) -> str:
        if not cards:
            return "??  ??"
        return "  ".join(str(card) for card in cards)

class RoundRateModel:
    def __init__(self, view: PokerBotViewer, kv: redis.Redis, model: "PokerBotModel"):
        self._view = view
        self._kv = kv
        self._model = model 
        
    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        num_players = game.seated_count()
        for i in range(1, num_players + 1):
            next_index = (start_index + i) % num_players
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index
        return -1
        
    def _get_first_player_index(self, game: Game) -> int:
        return self._find_next_active_player_index(game, game.dealer_index)

    def set_blinds(self, game: Game, chat_id: ChatId) -> None:
        """
        Determine small/big blinds (using seat indices) and debit the players.
        Works for heads-up (2-player) and multiplayer by walking occupied seats.
        """
        num_players = game.seated_count()
        if num_players < 2:
            return

        if num_players == 2:
            small_blind_index = game.dealer_index
            big_blind_index = game.next_occupied_seat(small_blind_index)
            first_action_index = small_blind_index
        else:
            small_blind_index = game.next_occupied_seat(game.dealer_index)
            big_blind_index = game.next_occupied_seat(small_blind_index)
            first_action_index = game.next_occupied_seat(big_blind_index)

        game.small_blind_index = small_blind_index
        game.big_blind_index = big_blind_index

        small_blind_player = game.get_player_by_seat(small_blind_index)
        big_blind_player = game.get_player_by_seat(big_blind_index)

        if small_blind_player is None or big_blind_player is None:
            return

        self._set_player_blind(game, small_blind_player, SMALL_BLIND, "کوچک", chat_id)
        self._set_player_blind(game, big_blind_player, SMALL_BLIND * 2, "بزرگ", chat_id)

        game.max_round_rate = SMALL_BLIND * 2
        game.current_player_index = first_action_index
        game.trading_end_user_id = big_blind_player.user_id

        player_turn = game.get_player_by_seat(game.current_player_index)
        if player_turn:
            self._view.send_turn_actions(
                chat_id=chat_id,
                game=game,
                player=player_turn,
                money=player_turn.wallet.value()
            )
    

    def _set_player_blind(self, game: Game, player: Player, amount: Money, blind_type: str, chat_id: ChatId):
        try:
            player.wallet.authorize(game_id=str(chat_id), amount=amount)
            player.round_rate += amount
            player.total_bet += amount 
            game.pot += amount
            self._view.send_message(
                chat_id,
                f"💸 {player.mention_markdown} بلایند {blind_type} به مبلغ {amount}$ را پرداخت کرد."
            )
        except UserException as e:
            available_money = player.wallet.value()
            player.wallet.authorize(game_id=str(chat_id), amount=available_money)
            player.round_rate += available_money
            player.total_bet += available_money 
            game.pot += available_money
            player.state = PlayerState.ALL_IN
            self._view.send_message(
                chat_id,
                f"⚠️ {player.mention_markdown} موجودی کافی برای بلایند نداشت و All-in شد ({available_money}$)."
            )

    def collect_bets_for_pot(self, game: Game):
        for player in game.seated_players():
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
        self._authorized_money_key = f"u_am:{user_id}" 
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
        val = self._kv.get(self._val_key)
        if val is None:
            self._kv.set(self._val_key, DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(val)

    def inc(self, amount: Money = 0) -> Money:
        return self._kv.incrby(self._val_key, amount)

    def dec(self, amount: Money) -> Money:
        if amount < 0:
            raise ValueError("Amount to decrease cannot be negative.")
        if amount == 0:
            return self.value()

        result = self._LUA_DECR_IF_GE(keys=[self._val_key], args=[amount, DEFAULT_MONEY])
        if result == -1:
            raise UserException("موجودی شما کافی نیست.")
        return int(result)

    def has_daily_bonus(self) -> bool:
        return self._kv.exists(self._daily_bonus_key) > 0

    def add_daily(self, amount: Money) -> Money:
        if self.has_daily_bonus():
            raise UserException("شما قبلاً پاداش روزانه خود را دریافت کرده‌اید.")

        now = datetime.datetime.now()
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        ttl = int((tomorrow - now).total_seconds())

        self._kv.setex(self._daily_bonus_key, ttl, "1")
        return self.inc(amount)

    def authorize(self, game_id: str, amount: Money) -> None:
        self.dec(amount)
        self._kv.hincrby(self._authorized_money_key, game_id, amount)

    def approve(self, game_id: str) -> None:
        self._kv.hdel(self._authorized_money_key, game_id)

    def cancel(self, game_id: str) -> None:
        amount_to_return_bytes = self._kv.hget(self._authorized_money_key, game_id)
        if amount_to_return_bytes:
            amount_to_return = int(amount_to_return_bytes)
            if amount_to_return > 0:
                self.inc(amount_to_return)
                self._kv.hdel(self._authorized_money_key, game_id)
