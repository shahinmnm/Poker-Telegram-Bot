#!/usr/bin/env python3

import datetime

import traceback

from threading import Timer

from typing import List, Tuple, Dict

import redis

from telegram import Message, ReplyKeyboardMarkup, Update, Bot, ReplyKeyboardRemove

from telegram.ext import Handler, CallbackContext

from pokerapp.config import Config

from pokerapp.privatechatmodel import UserPrivateChatModel

from pokerapp.winnerdetermination import WinnerDetermination

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
)

from pokerapp.pokerbotview import PokerBotViewer

DICE_MULT = 10

DICE_DELAY_SEC = 5

BONUSES = (5, 20, 40, 80, 160, 320)

DICES = "⚀⚁⚂⚃⚄⚅"
KEY_CHAT_DATA_GAME = "game"

KEY_OLD_PLAYERS = "old_players"

KEY_LAST_TIME_ADD_MONEY = "last_time"

KEY_NOW_TIME_ADD_MONEY = "now_time"

MAX_PLAYERS = 8

MIN_PLAYERS = 2

SMALL_BLIND = 5

BIG_BLIND = 2 * SMALL_BLIND

ONE_DAY = 86400

DEFAULT_MONEY = 1000

MAX_TIME_FOR_TURN = datetime.timedelta(minutes=2)

DESCRIPTION_FILE = "assets/description_bot.md"

class WalletManagerModel:

    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv = kv
        self._authorized = {}
    
    def _key(self) -> str:
        return f"money:{self._user_id}"
    
    def _daily_bonus_key(self) -> str:
        return f"daily_bonus_taken:{self._user_id}"
    
    def value(self) -> Money:
        money = self._kv.get(self._key())
        if money is None:
            self.set(DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(money)
    
    def set(self, amount: Money) -> None:
        self._kv.set(self._key(), amount)
    
    def add_daily(self, amount: Money) -> Money:
        today = datetime.date.today().isoformat()
        self._kv.set(self._daily_bonus_key(), today)
        new_value = self.value() + amount
        self.set(new_value)
        return new_value
        
    def has_daily_bonus(self) -> bool:
        last_bonus_date = self._kv.get(self._daily_bonus_key())
        if last_bonus_date is None:
            return False
        return last_bonus_date.decode('utf-8') == datetime.date.today().isoformat()
    
    def authorize(self, game_id: int, amount: Money) -> None:
        if self.value() < amount:
            raise UserException(f"Not enough money for tx, need: {amount}")
        self._authorized[game_id] = self._authorized.get(game_id, 0) + amount
        self.set(self.value() - amount)
    
    def inc(self, amount: Money):
        self.set(self.value() + amount)
    
    def authorized_money(self, game_id: int) -> Money:
        return self._authorized.get(game_id, 0)
    
    def approve(self, game_id: int) -> None:
        if game_id in self._authorized:
            del self._authorized[game_id]
    
    def cancel(self, game_id: int) -> None:
        if game_id in self._authorized:
            self.set(self.value() + self._authorized[game_id])
            del self._authorized[game_id]
            
class RoundRateModel:
    
    def round_pre_flop_rate_before_first_turn(self, game: Game) -> None:
        num_players = len(game.players)
        
        sb_player = game.players[0 % num_players]
        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.wallet.authorize(game.id, sb_amount)
        sb_player.round_rate = sb_amount
        sb_player.total_bet += sb_amount
    
        if num_players > 1:
            bb_player = game.players[1 % num_players]
            bb_amount = min(BIG_BLIND, bb_player.wallet.value())
            bb_player.wallet.authorize(game.id, bb_amount)
            bb_player.round_rate = bb_amount
            bb_player.total_bet += bb_amount
    
        game.max_round_rate = BIG_BLIND
    
    def call_check(self, game: Game, player: Player) -> Tuple[Money, Mention]:
        amount = game.max_round_rate - player.round_rate
        if player.wallet.value() < amount:
            raise UserException(
                "You don't have enough money to Call, you can only All-In or Fold")
                
        player.wallet.authorize(game.id, amount)
        player.round_rate += amount
        player.total_bet += amount
        
        return amount, player.mention_markdown
    
    def raise_bet(self, game: Game, player: Player, raise_bet_rate: PlayerAction) -> Tuple[Money, Mention, PlayerAction]:
        action = PlayerAction.RAISE_RATE
        if player.round_rate == game.max_round_rate:
            action = PlayerAction.BET
    
        call_amount = game.max_round_rate - player.round_rate
        raise_amount = 0
        pot_total = game.pot + sum(p.round_rate for p in game.players)
    
        if raise_bet_rate == PlayerAction.SMALL:
            raise_amount = max(BIG_BLIND, round(pot_total * 0.25))
        elif raise_bet_rate == PlayerAction.NORMAL:
            raise_amount = max(BIG_BLIND, round(pot_total * 0.5))
        elif raise_bet_rate == PlayerAction.BIG:
            raise_amount = max(BIG_BLIND, round(pot_total * 0.75))
    
        total_amount = call_amount + raise_amount
        if player.wallet.value() < total_amount:
            raise UserException(
                f"You don't have enough money to Raise, you need {total_amount}$")
    
        player.wallet.authorize(game.id, total_amount)
        player.round_rate += total_amount
        player.total_bet += total_amount
        game.max_round_rate = player.round_rate
    
        for p in game.players:
            if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                p.has_acted = False
    
        return raise_amount, player.mention_markdown, action
    
    def all_in(self, game: Game, player: Player) -> Tuple[Money, Mention]:
        amount = player.wallet.value()
        player.wallet.authorize(game.id, amount)
        player.round_rate += amount
        player.total_bet += amount
        player.state = PlayerState.ALL_IN
    
        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate
            for p in game.players:
                 if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                    p.has_acted = False
        
        return amount, player.mention_markdown
    
    def to_pot(self, game: Game) -> None:
        """
        Transfers all player round rates to the main pot and resets round-specific values.
        """
        for p in game.players:
            if p.round_rate > 0:
                game.pot += p.round_rate
                p.round_rate = 0
        
        game.max_round_rate = 0
        for p in game.players:
            if p.state == PlayerState.ACTIVE:
                p.has_acted = False
        
        if game.players:
             game.trading_end_user_id = game.players[0].user_id
    
    def _sum_authorized_money(self, game: Game, players: List[Tuple[Player, List[str]]]) -> int:
        sum_authorized_money = 0
        for player, _ in players:
            sum_authorized_money += player.wallet.authorized_money(game_id=game.id)
        return sum_authorized_money
    
    def finish_rate(self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]) -> List[Tuple[Player, Cards, Money]]:
        sorted_player_scores_items = sorted(player_scores.items(), reverse=True, key=lambda x: x[0])
        player_scores_values = [item[1] for item in sorted_player_scores_items]
        
        res = []
        for win_players_with_hands in player_scores_values:
            if not win_players_with_hands: continue
    
            pots = self._calculate_side_pots(game)
    
            for pot in sorted(pots, key=lambda p: p['max_bet']):
                eligible_players = [p for p in win_players_with_hands if p[0].total_bet >= pot['max_bet'] and p[0].user_id in pot['contributors']]
    
                if not eligible_players:
                    # If winners are not eligible, give pot to contributors
                    eligible_players = [(p, []) for p in game.players if p.user_id in pot['contributors']]
    
                if not eligible_players: continue
    
                pot_amount = pot['amount']
                per_player_share = pot_amount / len(eligible_players)
                
                for player, best_hand in eligible_players:
                    win_money = round(per_player_share)
                    player.wallet.inc(win_money)
                    
                    found = False
                    for r_player, r_hand, r_money in res:
                        if r_player.user_id == player.user_id:
                            res[res.index((r_player, r_hand, r_money))] = (r_player, best_hand, r_money + win_money)
                            found = True
                            break
                    if not found:
                        res.append((player, best_hand, win_money))
    
        # Handle any remaining pot for single winner case
        if len(player_scores_values) == 1 and len(player_scores_values[0]) == 1:
            winner, hand = player_scores_values[0][0]
            remaining_pot = game.pot
            if any(r[0].user_id == winner.user_id for r in res):
                 # Already has a record, just add money
                 for i, (p, h, m) in enumerate(res):
                     if p.user_id == winner.user_id:
                         res[i] = (p, h, m + remaining_pot)
                         break
            else:
                # New winner record
                winner.wallet.inc(remaining_pot)
                res.append((winner, hand, remaining_pot))
    
        return res
    
    def _calculate_side_pots(self, game: Game) -> list:
        pots = []
        players = sorted([p for p in game.players if p.total_bet > 0], key=lambda p: p.total_bet)
        last_bet = 0
        
        for player in players:
            side_pot_amount = 0
            pot_contributors = set()
            
            bet_increment = player.total_bet - last_bet
            if bet_increment > 0:
                for p_contrib in game.players:
                    contribution = min(bet_increment, max(0, p_contrib.total_bet - last_bet))
                    if contribution > 0:
                        side_pot_amount += contribution
                        pot_contributors.add(p_contrib.user_id)
    
                if side_pot_amount > 0:
                    pots.append({'max_bet': player.total_bet, 'amount': side_pot_amount, 'contributors': pot_contributors})
                last_bet = player.total_bet
        return pots
class PokerBotModel:

    def __init__(self, view: PokerBotViewer, bot: Bot, cfg: Config, kv):
        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._winner_determine: WinnerDetermination = WinnerDetermination()
        self._kv = kv
        self._cfg: Config = cfg
        self._round_rate: RoundRateModel = RoundRateModel()
        self._readyMessages = {}
    
    @property
    def _min_players(self):
        if self._cfg.DEBUG:
            return 1
        return MIN_PLAYERS
    
    @staticmethod
    def _game_from_context(context: CallbackContext) -> Game:
        if KEY_CHAT_DATA_GAME not in context.chat_data:
            context.chat_data[KEY_CHAT_DATA_GAME] = Game()
        return context.chat_data[KEY_CHAT_DATA_GAME]
    
    @staticmethod
    def _current_turn_player(game: Game) -> Player:
        if game.current_player_index < 0 or game.current_player_index >= len(game.players):
            return None
        return game.players[game.current_player_index]
    
    def ready(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
    
        if game.state != GameState.INITIAL:
            self._view.send_message_reply(chat_id=chat_id, message_id=update.effective_message.message_id, text="⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return
    
        if len(game.players) >= MAX_PLAYERS:
            self._view.send_message_reply(chat_id=chat_id, text="🚪 اتاق پر است!", message_id=update.effective_message.message_id)
            return
    
        user = update.effective_message.from_user
        if user.id in game.ready_users:
            self._view.send_message_reply(chat_id=chat_id, message_id=update.effective_message.message_id, text="✅ شما از قبل آماده‌اید.")
            return
    
        player = Player(
            user_id=user.id,
            mention_markdown=user.mention_markdown(),
            wallet=WalletManagerModel(user.id, self._kv),
            ready_message_id=update.effective_message.message_id,
        )
    
        if player.wallet.value() < BIG_BLIND:
            return self._view.send_message_reply(chat_id=chat_id, message_id=update.effective_message.message_id, text="💸 پول شما برای ورود به بازی کافی نیست.")
    
        game.ready_users.add(user.id)
        game.players.append(player)
        
        self._view.send_message(chat_id=chat_id, text=f"{user.mention_markdown()} برای بازی اعلام آمادگی کرد. بازیکنان فعلی: {len(game.players)}")
    
        members_count = self._bot.get_chat_member_count(chat_id)
        players_active = len(game.players)
        # One is the bot.
        if players_active >= self._min_players and (players_active == members_count - 1 or self._cfg.DEBUG):
             self._start_game(context=context, game=game, chat_id=chat_id)
    
    def stop(self, user_id: UserId) -> None:
        UserPrivateChatModel(user_id=user_id, kv=self._kv).delete()
    
    def start(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        user_id = update.effective_message.from_user.id
    
        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            self._view.send_message(chat_id=chat_id, text="🎮 یک بازی در حال حاضر در جریان است.")
            return
        
        # Reset game if it was finished
        if game.state == GameState.FINISHED:
            game.reset()
            context.chat_data[KEY_CHAT_DATA_GAME] = game
    
        # One is the bot.
        members_count = self._bot.get_chat_member_count(chat_id) - 1
        if members_count == 1:
            try:
                with open(DESCRIPTION_FILE, 'r') as f:
                    text = f.read()
                self._view.send_message(chat_id=chat_id, text=text)
                self._view.send_photo(chat_id=chat_id)
            except FileNotFoundError:
                self._view.send_message(chat_id=chat_id, text="به ربات پوکر خوش آمدید!")
    
    
            if update.effective_chat.type == 'private':
                UserPrivateChatModel(user_id=user_id, kv=self._kv).set_chat_id(chat_id=chat_id)
            return
    
        players_active = len(game.players)
        if players_active >= self._min_players:
            self._start_game(context=context, game=game, chat_id=chat_id)
        else:
            self._view.send_message(chat_id=chat_id, text=f"👤 برای شروع بازی حداقل به {self._min_players} بازیکن نیاز است. منتظر بازیکنان بیشتر...")
    
    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        print(f"new game: {game.id}, players count: {len(game.players)}")
    
        self._view.send_message(chat_id=chat_id, text='🚀 !بازی شروع شد!', reply_markup=ReplyKeyboardMarkup(keyboard=[["/table"]], resize_keyboard=True))
    
        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
        # Rotate dealer button
        if old_players_ids:
            old_players_ids.append(old_players_ids.pop(0))
        else:
            old_players_ids = [p.user_id for p in game.players]
            
        def index(ln: List, user_id: UserId) -> int:
                try:
                    return ln.index(user_id)
                except ValueError:
                    return len(ln)
        game.players.sort(key=lambda p: index(old_players_ids, p.user_id))
    
        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game=game, chat_id=chat_id)
    
        self._round_rate.round_pre_flop_rate_before_first_turn(game)
    
        num_players = len(game.players)
        start_index = 0
        if game.state == GameState.ROUND_PRE_FLOP:
            if num_players > 2:
                start_index = 2
            else: # Heads-up, player with small blind (button) acts first
                start_index = 0
    
        game.current_player_index = start_index - 1
        self._process_playing(chat_id=chat_id, game=game)
    
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]
    
    def _process_playing(self, chat_id: ChatId, game: Game) -> None:
        active_and_all_in_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(active_and_all_in_players) <= 1:
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if active_players and game.all_in_players_are_covered():
                self._fast_forward_to_finish(game, chat_id)
            else:
                self._finish(game, chat_id)
            return
    
        all_players_acted = all(p.has_acted or p.state != PlayerState.ACTIVE for p in game.players)
        rates_equalized = all(p.round_rate == game.max_round_rate or p.state != PlayerState.ACTIVE for p in game.players)
        round_over = all_players_acted and rates_equalized
    
        if round_over:
            self._round_rate.to_pot(game)
            self._goto_next_round(game, chat_id)
            if game.state == GameState.INITIAL: # game has finished and reset
                return
            game.current_player_index = -1 # start from first player after dealer
            self._process_playing(chat_id, game)
            return
    
        # Find next active player
        start_index = game.current_player_index
        while True:
            game.current_player_index = (game.current_player_index + 1) % len(game.players)
            current_player = self._current_turn_player(game)
            if current_player.state == PlayerState.ACTIVE:
                break
            # Avoid infinite loop if no active players left
            if game.current_player_index == start_index:
                 self._finish(game, chat_id)
                 return
    
        game.last_turn_time = datetime.datetime.now()
        current_player_money = current_player.wallet.value()
    
        msg_id = self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=current_player,
            money=current_player_money,
        )
        game.turn_message_id = msg_id
    
    def _fast_forward_to_finish(self, game: Game, chat_id: ChatId):
        """ When no more betting is possible, reveals all remaining cards """
        self._round_rate.to_pot(game)
        if game.state == GameState.ROUND_PRE_FLOP:
            self.add_cards_to_table(3, game, chat_id)
            game.state = GameState.ROUND_FLOP
        if game.state == GameState.ROUND_FLOP:
            self.add_cards_to_table(1, game, chat_id)
            game.state = GameState.ROUND_TURN
        if game.state == GameState.ROUND_TURN:
            self.add_cards_to_table(1, game, chat_id)
            game.state = GameState.ROUND_RIVER
        self._finish(game, chat_id)
    
    def bonus(self, update: Update, context: CallbackContext) -> None:
        wallet = WalletManagerModel(update.effective_message.from_user.id, self._kv)
        money = wallet.value()
    
        chat_id = update.effective_message.chat_id
        message_id = update.effective_message.message_id
    
        if wallet.has_daily_bonus():
            return self._view.send_message_reply(chat_id=chat_id, message_id=update.effective_message.message_id, text=f"💰 پول شما: *{money}$*\nشما امروز جایزه خود را دریافت کرده‌اید.")
    
        icon: str
        dice_msg: Message
        bonus: Money
    
        SATURDAY = 5
        if datetime.datetime.today().weekday() == SATURDAY:
            dice_msg = self._view.send_dice_reply(chat_id=chat_id, message_id=message_id, emoji='🎰')
            icon = '🎰'
            bonus = dice_msg.dice.value * 20
        else:
            dice_msg = self._view.send_dice_reply(chat_id=chat_id, message_id=message_id)
            icon = DICES[dice_msg.dice.value-1]
            bonus = BONUSES[dice_msg.dice.value - 1]
    
        message_id = dice_msg.message_id
        
        def print_bonus() -> None:
            new_money = wallet.add_daily(amount=bonus)
            self._view.send_message_reply(chat_id=chat_id, message_id=message_id, text=f"🎁 پاداش: *{bonus}$* {icon}\n💰 پول جدید شما: *{new_money}$*\n")
    
        Timer(DICE_DELAY_SEC, print_bonus).start()
    
    def send_cards_to_user(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
    
        current_player = None
        for player in game.players:
            if player.user_id == update.effective_user.id:
                current_player = player
                break
    
        if current_player is None or not current_player.cards:
            self._view.send_message_reply(chat_id=update.effective_chat.id, message_id=update.effective_message.message_id, text="شما در بازی فعالی نیستید یا کارتی ندارید.")
            return
        
        try:
            self._send_cards_private(current_player, current_player.cards)
        except Exception as e:
            print(f"Failed to send private cards to {current_player.user_id}: {e}")
            self._view.send_message_reply(chat_id=update.effective_chat.id, message_id=update.effective_message.message_id, text="لطفا ربات را استارت کنید تا بتوانم کارت‌ها را در چت خصوصی برایتان ارسال کنم.")
    
    
    def _check_access(self, chat_id: ChatId, user_id: UserId) -> bool:
        try:
            chat_admins = self._bot.get_chat_administrators(chat_id)
            for m in chat_admins:
                if m.user.id == user_id:
                    return True
        except Exception as e:
            print(f"Could not get chat admins for {chat_id}: {e}")
        return False
    
    def _send_cards_private(self, player: Player, cards: Cards) -> None:
        user_chat_model = UserPrivateChatModel(user_id=player.user_id, kv=self._kv)
        private_chat_id = user_chat_model.get_chat_id()
    
        if private_chat_id is None:
            raise ValueError("private chat not found for user")
    
        private_chat_id = private_chat_id.decode('utf-8')
    
        # Clean up old card messages
        try:
            rm_msg_id = user_chat_model.pop_message()
            while rm_msg_id is not None:
                try:
                    rm_msg_id = rm_msg_id.decode('utf-8')
                    self._view.remove_message(chat_id=private_chat_id, message_id=rm_msg_id)
                except Exception:
                    pass # Ignore if message not found
                rm_msg_id = user_chat_model.pop_message()
        except Exception as ex:
            print(f"Error cleaning up old private messages for {player.user_id}: {ex}")
    
        # Send new cards and store message ID
        message_id = self._view.send_desk_cards_img(
            chat_id=private_chat_id,
            cards=cards,
            caption="کارت‌های شما در این دست",
            disable_notification=False,
        ).message_id
        user_chat_model.push_message(message_id=message_id)
    
    def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        for player in game.players:
            cards = player.cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            try:
                self._send_cards_private(player=player, cards=cards)
                self._view.send_message(chat_id, f"کارت‌های {player.mention_markdown} به صورت خصوصی ارسال شد.")
            except Exception as ex:
                print(f"Could not send cards privately to {player.user_id}, sending to group. Error: {ex}")
                msg_id = self._view.send_cards(
                    chat_id=chat_id,
                    cards=cards,
                    mention_markdown=player.mention_markdown,
                    ready_message_id=player.ready_message_id,
                )
                if msg_id: game.message_ids_to_delete.append(msg_id)
    
    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId) -> None:
        for _ in range(count):
            if not game.remain_cards: break
            game.cards_table.append(game.remain_cards.pop())
    
        msg_id = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"💰 پات فعلی: {game.pot}$",
        )
        if msg_id: game.message_ids_to_delete.append(msg_id)
    
    def _finish(self, game: Game, chat_id: ChatId) -> None:
        try:
            if game.pot == 0 and any(p.round_rate > 0 for p in game.players):
                self._round_rate.to_pot(game)
            print(f"game finished: {game.id}, players count: {len(game.players)}, pot: {game.pot}")
    
            active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
            
            # If only one player left, they win the pot
            if len(active_players) == 1:
                winner = active_players[0]
                win_money = game.pot
                winner.wallet.inc(win_money)
                text = f"🏁 بازی تمام شد!\n\n{winner.mention_markdown} تمام حریفان را کنار زد و برنده *{win_money}$* شد!"
            else:
                player_scores = self._winner_determine.determinate_scores(active_players, game.cards_table)
                winners_hand_money = self._round_rate.finish_rate(game, player_scores)
    
                text = "🏁 بازی با این نتیجه تموم شد:\n\n"
                if not winners_hand_money:
                     text += "هیچ برنده‌ای مشخص نشد. پول به بازیکنان بازگردانده می‌شود."
                     for p in active_players:
                         p.wallet.cancel(game.id)
                else:
                    for (player, best_hand, money) in winners_hand_money:
                        win_hand = " ".join(best_hand)
                        text += f"{player.mention_markdown}:\n🏆 برنده شد: *{money} $*\n"
                        if best_hand:
                            text += f"🃏 با ترکیب: {win_hand}\n\n"
    
            text += "\n/ready برای شروع بازی جدید"
            self._view.send_message(chat_id=chat_id, text=text, reply_markup=ReplyKeyboardRemove())
            
            for player in game.players:
                player.wallet.approve(game.id)
    
        except Exception as e:
            print(f"CRITICAL ERROR in _finish: {e}")
            traceback.print_exc()
            self._view.send_message(chat_id=chat_id, text="یک خطای بحرانی در پایان بازی رخ داد. بازی ریست می‌شود.")
        finally:
             self._view.remove_game_messages(chat_id, game.message_ids_to_delete)
             game.reset()
    
    def _goto_next_round(self, game: Game, chat_id: ChatId):
        state_transitions = {
            GameState.ROUND_PRE_FLOP: {"next_state": GameState.ROUND_FLOP, "processor": lambda: self.add_cards_to_table(3, game, chat_id)},
            GameState.ROUND_FLOP: {"next_state": GameState.ROUND_TURN, "processor": lambda: self.add_cards_to_table(1, game, chat_id)},
            GameState.ROUND_TURN: {"next_state": GameState.ROUND_RIVER, "processor": lambda: self.add_cards_to_table(1, game, chat_id)},
            GameState.ROUND_RIVER: {"next_state": GameState.INITIAL, "processor": lambda: self._finish(game, chat_id)}
        }
    
        transition = state_transitions.get(game.state)
        if transition:
            game.state = transition["next_state"]
            transition["processor"]()
        else: # Should not happen
            print(f"Error: No next round defined for state {game.state}")
            self._finish(game, chat_id)
    
    
    def middleware_user_turn(self, fn: Handler) -> Handler:
        def m(update, context):
            try:
                game = self._game_from_context(context)
                if game.state == GameState.INITIAL: return
    
                current_player = self._current_turn_player(game)
                if not current_player or update.callback_query.from_user.id != current_player.user_id: 
                    self._view.answer_callback_query(update.callback_query.id, "⏳ نوبت شما نیست.")
                    return
    
                fn(update, context)
    
                if game.turn_message_id:
                    try:
                        self._view.remove_markup(chat_id=update.effective_message.chat_id, message_id=game.turn_message_id)
                    except Exception as e:
                        print(f"Could not remove markup for message {game.turn_message_id}: {e}")
            except Exception as e:
                print(f"CRITICAL ERROR in middleware: {e}")
                traceback.print_exc()
                self._view.send_message(update.effective_chat.id, "یک خطای بحرانی در پردازش حرکت رخ داد. بازی ریست می‌شود.")
                game.reset()
    
        return m
    
    def ban_player(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
    
        if game.state in (GameState.INITIAL, GameState.FINISHED): return
        
        current_player = self._current_turn_player(game)
        if not self._check_access(chat_id, update.effective_user.id) and update.effective_user.id != current_player.user_id:
            diff = datetime.datetime.now() - game.last_turn_time
            if diff < MAX_TIME_FOR_TURN:
                self._view.send_message(chat_id=chat_id, text=f"⏳ هنوز نمی‌توانید بازیکن را محروم کنید. زمان نوبت {current_player.mention_markdown} تمام نشده است.")
                return
    
        self._view.send_message(chat_id=chat_id, text=f"⏰ وقت {current_player.mention_markdown} تمام شد! حرکت به صورت Fold ثبت شد.")
        self.fold(update, context, is_ban=True)
    
    
    def _action_handler(self, update: Update, context: CallbackContext, action_logic, is_ban=False):
        """A generic handler for player actions"""
        game = self._game_from_context(context)
        
        player = self._current_turn_player(game)
        if not player: return # Game might have ended
    
        # In case of a ban, the action is on the current player, but initiated by another
        if not is_ban and update.callback_query.from_user.id != player.user_id:
            return
    
        try:
            action_logic(game, player)
            player.has_acted = True
        except UserException as e:
            self._view.answer_callback_query(update.callback_query.id, str(e))
            return
        
        # Only answer query if it's not from a ban
        if not is_ban:
            self._view.answer_callback_query(update.callback_query.id)
    
        self._process_playing(chat_id=update.effective_message.chat_id, game=game)
    
    def fold(self, update: Update, context: CallbackContext, is_ban=False) -> None:
        def logic(game, player):
            player.state = PlayerState.FOLD
            msg_id = self._view.send_message_return_id(
                chat_id=update.effective_message.chat_id,
                text=f"{player.mention_markdown} {PlayerAction.FOLD.value}"
            )
            if msg_id: game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic, is_ban)
    
    def call_check(self, update: Update, context: CallbackContext) -> None:
        def logic(game, player):
            action = PlayerAction.CHECK if player.round_rate == game.max_round_rate else PlayerAction.CALL
            
            if action == PlayerAction.CALL and player.wallet.value() <= (game.max_round_rate - player.round_rate):
                amount, mention = self._round_rate.all_in(game, player)
                text = f"{mention} {PlayerAction.ALL_IN.value} ({amount}$)"
            else:
                amount, mention = self._round_rate.call_check(game, player)
                text = f"{mention} {action.value}"
                if action == PlayerAction.CALL:
                    text += f" ({amount}$)"
    
            msg_id = self._view.send_message_return_id(chat_id=update.effective_message.chat_id, text=text)
            if msg_id: game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)
    
    def raise_rate_bet(self, update: Update, context: CallbackContext, raise_bet_rate: PlayerAction) -> None:
        def logic(game, player):
            amount, mention, action = self._round_rate.raise_bet(game, player, raise_bet_rate)
            text = f"{mention} {action.value} به {amount}$ (مجموع: {player.round_rate}$)"
            msg_id = self._view.send_message_return_id(chat_id=update.effective_message.chat_id, text=text)
            if msg_id: game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)
    
    def all_in(self, update: Update, context: CallbackContext) -> None:
        def logic(game, player):
            amount, mention = self._round_rate.all_in(game, player)
            text = f"{mention} {PlayerAction.ALL_IN.value} ({amount}$)"
            msg_id = self._view.send_message_return_id(chat_id=update.effective_message.chat_id, text=text)
            if msg_id: game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)
    
    def show_table(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        
        if game.state == GameState.INITIAL:
            self._view.send_message(chat_id, "هنوز بازی شروع نشده است.")
            return
            
        text = "وضعیت فعلی میز:\n"
        text += f"💰 **پات اصلی:** {game.pot}$\n"
        
        if game.cards_table:
            text += f"🃏 **کارت‌های روی میز:** {' '.join(game.cards_table)}\n"
        else:
            text += "🃏 **کارت‌های روی میز:** هنوز کارتی رو نشده.\n"
            
        text += "\n👥 **بازیکنان:**\n"
        for p in game.players:
            state_emoji = {
                PlayerState.ACTIVE: "✅",
                PlayerState.FOLD: "❌",
                PlayerState.ALL_IN: "💰"
            }.get(p.state, "")
            
            text += f"{state_emoji} {p.mention_markdown}: {p.wallet.value()}$"
            if p.round_rate > 0:
                text += f" (شرط: {p.round_rate}$)"
            text += "\n"
        
        current_player = self._current_turn_player(game)
        if current_player:
            text += f"\n🔄 **نوبت:** {current_player.mention_markdown}\n"
            
        self._view.send_message(chat_id, text)
