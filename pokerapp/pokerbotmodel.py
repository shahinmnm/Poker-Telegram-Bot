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
    MessageId,
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
ONE_DAY = 86400
DEFAULT_MONEY = 1000
MAX_TIME_FOR_TURN = datetime.timedelta(minutes=2)
DESCRIPTION_FILE = "assets/description_bot.md"

# WalletManagerModel and RoundRateModel should be here as per your file structure.
# I'm assuming they are present in your file. I will omit them for brevity
# but you should ensure they remain in your final file.

class WalletManagerModel(Wallet):
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv: redis.Redis = kv
        self._money_key = f"money:{self._user_id}"
        self._daily_bonus_key = f"daily_bonus_time:{self._user_id}"
        # Ensure user has default money if not set
        if self._kv.get(self._money_key) is None:
            self.set(DEFAULT_MONEY)

    def value(self) -> Money:
        money = self._kv.get(self._money_key)
        return int(money)

    def set(self, amount: Money) -> None:
        self._kv.set(self._money_key, amount)

    def inc(self, amount: Money) -> Money:
        # Note: incrby can also handle negative values, so dec is not strictly needed
        return self._kv.incrby(self._money_key, amount)

    def authorized_money(self, game_id: str) -> Money:
        auth_money = self._kv.get(f"auth:{game_id}:{self._user_id}")
        return int(auth_money) if auth_money else 0

    def authorize(self, game_id: str, amount: Money) -> None:
        self._kv.set(f"auth:{game_id}:{self._user_id}", amount)

    def approve(self, game_id: str) -> None:
        self._kv.delete(f"auth:{game_id}:{self._user_id}")

    def add_daily(self, amount: Money) -> Money:
        self._kv.set(self._daily_bonus_key, datetime.datetime.now().timestamp(), ex=ONE_DAY)
        return self.inc(amount)

    def has_daily_bonus(self) -> bool:
        return self._kv.exists(self._daily_bonus_key)


class RoundRateModel:
    def round_pre_flop_rate_before_first_turn(self, game: Game):
        if len(game.players) < 2: return
        
        for p in game.players:
            p.wallet.authorize(game.id, p.wallet.value())

        sb_player_index = 0
        bb_player_index = 1
        
        sb_player = game.players[sb_player_index]
        bb_player = game.players[bb_player_index]
        
        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.round_rate += sb_amount
        sb_player.wallet.inc(-sb_amount)

        bb_amount = min(2 * SMALL_BLIND, bb_player.wallet.value())
        bb_player.round_rate += bb_amount
        bb_player.wallet.inc(-bb_amount)
        
        game.max_round_rate = bb_amount
        game.trading_end_user_id = bb_player.user_id
        
        sb_player.has_acted = True
        bb_player.has_acted = True

    def call_check(self, game: Game, player: Player):
        amount = game.max_round_rate - player.round_rate
        if player.wallet.value() < amount:
            raise UserException("پول کافی برای کال نداری. باید All-in کنی.")
        player.round_rate += amount
        player.wallet.inc(-amount)

    def raise_bet(self, game: Game, player: Player, raise_bet_amount: Money) -> Tuple[Money, Mention]:
        amount_to_call = game.max_round_rate - player.round_rate
        total_bet_amount = amount_to_call + raise_bet_amount

        if player.wallet.value() < total_bet_amount:
            raise UserException("پول کافی برای این رِیز نداری.")

        player.round_rate += total_bet_amount
        player.wallet.inc(-total_bet_amount)
        game.max_round_rate = player.round_rate
        game.trading_end_user_id = player.user_id
        
        for p in game.players:
            if p.user_id != player.user_id:
                p.has_acted = False

        return raise_bet_amount, player.mention_markdown

    def all_in(self, game: Game, player: Player) -> Tuple[Money, Mention]:
        amount = player.wallet.value()
        player.round_rate += amount
        player.wallet.set(0)
        player.state = PlayerState.ALL_IN

        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate
            game.trading_end_user_id = player.user_id
            for p in game.players:
                if p.user_id != player.user_id:
                    p.has_acted = False

        return amount, player.mention_markdown

    def to_pot(self, game: Game) -> None:
        """
        Transfers all player round rates to the main pot and resets round-specific values.
        """
        for p in game.players:
            if p.round_rate > 0:
                game.pot += p.round_rate
                # p.total_bet += p.round_rate # This was in your code, keeping it for side-pot logic
                p.round_rate = 0
        
        # Reset round-specific betting values
        game.max_round_rate = 0
        for p in game.players:
            if p.state == PlayerState.ACTIVE:
                p.has_acted = False

        # Set the starting player for the next round (usually player after the button)
        if game.players:
            game.trading_end_user_id = game.players[0].user_id


    def finish_rate(self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]) -> List[Tuple[Player, Cards, Money]]:
        all_players_in_hand = [p for p in game.players if p.wallet.authorized_money(game.id) > 0]
        if not all_players_in_hand:
            active_winners = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
            if not active_winners: return []
            winner = active_winners[0]
            winner.wallet.inc(game.pot)
            return [(winner, winner.cards, game.pot)]

        total_bets = {p.user_id: p.wallet.authorized_money(game.id) - p.wallet.value() for p in all_players_in_hand}
        sorted_bets = sorted(list(set(total_bets.values())))

        pots = []
        last_bet_level = 0
        for bet_level in sorted_bets:
            pot_amount = 0
            eligible_players = []

            for player in all_players_in_hand:
                player_bet = total_bets.get(player.user_id, 0)
                contribution = min(player_bet, bet_level) - last_bet_level
                if contribution > 0:
                    pot_amount += contribution
                    eligible_players.append(player)

            if pot_amount > 0:
                pots.append({"amount": pot_amount, "eligible_players": eligible_players})
            
            last_bet_level = bet_level

        final_winnings = {}
        for pot in pots:
            eligible_winners = []
            best_score_in_pot = Score(-1)

            sorted_scores = sorted(player_scores.items(), key=lambda item: item[0], reverse=True)

            for score, players_with_score in sorted_scores:
                for player, hand in players_with_score:
                    if player in pot["eligible_players"]:
                        if score > best_score_in_pot:
                            best_score_in_pot = score
                            eligible_winners = [(player, hand)]
                        elif score == best_score_in_pot:
                            eligible_winners.append((player, hand))

            if not eligible_winners: continue

            win_share = round(pot["amount"] / len(eligible_winners))
            for winner, hand in eligible_winners:
                winner.wallet.inc(win_share)
                
                if winner.user_id not in final_winnings:
                    final_winnings[winner.user_id] = {"player": winner, "hand": hand, "money": 0}
                final_winnings[winner.user_id]["money"] += win_share
        
        return [(v["player"], v["hand"], v["money"]) for v in final_winnings.values()]

class PokerBotModel:
    # ===== اصلاح تورفتگی در اینجا =====
    ACTIVE_GAME_STATES = [
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    ]

    def __init__(
        self,
        view: PokerBotViewer,
        bot: Bot,
        cfg: Config,
        kv,
    ):
        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._winner_determine: WinnerDetermination = WinnerDetermination()
        self._kv = kv
        self._cfg: Config = cfg
        self._round_rate: RoundRateModel = RoundRateModel()

    def show_table(self, update: Update, context: CallbackContext) -> None:
        chat_id = update.effective_chat.id
        game = self._game_from_context(context)

        if not game or game.state not in self.ACTIVE_GAME_STATES:
            return

        text = f"💰 پات فعلی: *{game.pot}$*"
        self._view.send_message(chat_id=chat_id, text=text)

        if game.cards_table:
            self._view.send_desk_cards_img(
                chat_id=chat_id,
                cards=game.cards_table,
                caption="کارت‌های روی میز"
            )
        else:
            self._view.send_message(
                chat_id=chat_id,
                text="هنوز کارتی روی میز قرار نگرفته است."
            )

        current_player = self._current_turn_player(game)
        if current_player and current_player.state == PlayerState.ACTIVE:
            current_player_money = current_player.wallet.value()
            # We are recreating the turn message, so we must store the new ID
            msg_id = self._view.send_turn_actions(
                chat_id=chat_id,
                game=game,
                player=current_player,
                money=current_player_money,
            )
            if msg_id:
                # Remove the old turn message if it exists
                if game.turn_message_id:
                    try:
                        self._view.remove_markup(chat_id, game.turn_message_id)
                    except Exception:
                        pass # It might have already been removed
                game.turn_message_id = msg_id
            else:
                self._view.send_message(
                    chat_id,
                    "خطا در نمایش مجدد نوبت. بازی ادامه دارد...",
                )

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
        if not game.players:
            return None
        i = game.current_player_index % len(game.players)
        return game.players[i]

    def ready(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id

        if game.state != GameState.INITIAL:
            msg_id = self._view.send_message_return_id(
                chat_id=chat_id,
                text="⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!"
            )
            # You might want a mechanism to auto-delete these messages later
            # if msg_id and hasattr(game, 'message_ids_to_delete'):
            #     game.message_ids_to_delete.append(msg_id)
            return

        if len(game.players) >= MAX_PLAYERS:
            self._view.send_message_reply(
                chat_id=chat_id,
                text="🚪 اتاق پره!",
                message_id=update.effective_message.message_id,
            )
            return

        user = update.effective_message.from_user
        if user.id in game.ready_users:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="✅ تو از قبل آماده‌ای!",
            )
            return

        player = Player(
            user_id=user.id,
            mention_markdown=user.mention_markdown(),
            wallet=WalletManagerModel(user.id, self._kv),
            ready_message_id=update.effective_message.message_id,
        )

        if player.wallet.value() < 2 * SMALL_BLIND:
            return self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="💸 پولت کمه",
            )

        game.ready_users.add(user.id)
        game.players.append(player)

        try:
            members_count = self._bot.get_chat_member_count(chat_id)
            players_active = len(game.players)
            # One is the bot.
            if players_active >= self._min_players and (players_active == members_count - 1 or self._cfg.DEBUG):
                self._start_game(context=context, game=game, chat_id=chat_id)
        except Exception as e:
            print(f"Error getting member count or starting game: {e}")
            if self._cfg.DEBUG and len(game.players) >= self._min_players:
                print("DEBUG mode: Starting game without member count check.")
                self._start_game(context=context, game=game, chat_id=chat_id)


    def stop(self, user_id: UserId) -> None:
        UserPrivateChatModel(user_id=user_id, kv=self._kv).delete()

    def start(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        user_id = update.effective_message.from_user.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            msg_id = self._view.send_message_return_id(chat_id=chat_id, text="🎮 بازی الان داره اجرا میشه")
            # if msg_id and hasattr(game, 'message_ids_to_delete'):
            #     game.message_ids_to_delete.append(msg_id)
            return
        
        try:
            # One is the bot.
            members_count = self._bot.get_chat_member_count(chat_id) - 1
            if members_count == 1 and not self._cfg.DEBUG:
                try:
                    with open(DESCRIPTION_FILE, 'r') as f:
                        text = f.read()
                    self._view.send_message(chat_id=chat_id, text=text)
                    self._view.send_photo(chat_id=chat_id)
                except FileNotFoundError:
                     self._view.send_message(chat_id=chat_id, text="Welcome to Poker Bot!")

                if update.effective_chat.type == 'private':
                    UserPrivateChatModel(user_id=user_id, kv=self._kv).set_chat_id(chat_id=chat_id)
                return
        except Exception as e:
            print(f"Could not get member count: {e}. Bot might not be admin.")

        players_active = len(game.players)
        if players_active >= self._min_players:
            self._start_game(context=context, game=game, chat_id=chat_id)
        else:
            msg_id = self._view.send_message_return_id(chat_id=chat_id, text="👤 بازیکن کافی نیست")
            # if msg_id and hasattr(game, 'message_ids_to_delete'):
            #     game.message_ids_to_delete.append(msg_id)

    def _start_game(
        self,
        context: CallbackContext,
        game: Game,
        chat_id: ChatId
    ) -> None:
        print(f"INFO: New game starting: {game.id}, players count: {len(game.players)}")
        game.message_ids_to_delete = []

        self._view.send_message(
            chat_id=chat_id,
            text='🚀 !بازی شروع شد!',
            reply_markup=ReplyKeyboardRemove(),
        )

        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS)
        if old_players_ids:
            # Rotate dealer button
            old_players_ids = old_players_ids[1:] + old_players_ids[:1]
            def index(ln: List, user_id: UserId) -> int:
                try:
                    return ln.index(user_id)
                except ValueError:
                    return len(ln) # New players go to the end
            game.players.sort(key=lambda p: index(old_players_ids, p.user_id))

        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game=game, chat_id=chat_id)
        self._round_rate.round_pre_flop_rate_before_first_turn(game)
        
        num_players = len(game.players)
        if num_players == 2:
             # In heads-up, small blind (dealer) acts first before the flop.
            game.current_player_index = -1 
        else:
             # In multi-way pots, player after big blind (UTG) acts first.
            game.current_player_index = 1 

        self._process_playing(chat_id=chat_id, game=game)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]

    def _process_playing(self, chat_id: ChatId, game: Game) -> None:
        print(f"DEBUG: Processing play. Current state: {game.state}")

        if game.state == GameState.INITIAL:
            print("DEBUG: Process playing exited, game state is INITIAL.")
            return

        active_and_all_in_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(active_and_all_in_players) <= 1:
            print(f"DEBUG: Finishing game. Active/All-in players: {len(active_and_all_in_players)}")
            # If players are all-in, no more betting can occur, so show cards immediately.
            if any(p.state == PlayerState.ALL_IN for p in game.players) and len(game.cards_table) < 5:
                 self._fast_forward_to_finish(game, chat_id)
            else:
                 self._finish(game, chat_id)
            return

        # Check if the betting round is over
        round_over = False
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            # All remaining players are All-In
            round_over = True
        else:
            # Round is over if all active players have acted and have put in the same amount of money.
            all_acted = all(p.has_acted for p in active_players)
            all_rates_equal = all(p.round_rate == game.max_round_rate for p in active_players)
            if all_acted and all_rates_equal:
                round_over = True
        
        if round_over:
            print(f"DEBUG: Round is over. Current state: {game.state}. Moving to next.")
            self._round_rate.to_pot(game)
            self._goto_next_round(game, chat_id)
            if game.state == GameState.INITIAL: # Game finished
                return
            
            # Reset for next round
            game.current_player_index = -1 # Start from player after dealer button
            for p in game.players:
                if p.state == PlayerState.ACTIVE:
                    p.has_acted = False
            
            # Recursively call to start the next round's action
            self._process_playing(chat_id, game)
            return

        # Find the next player to act
        tries = 0
        while tries < len(game.players) * 2:
            game.current_player_index = (game.current_player_index + 1) % len(game.players)
            current_player = self._current_turn_player(game)
            if current_player.state == PlayerState.ACTIVE:
                break
            tries += 1
        
        if current_player.state != PlayerState.ACTIVE:
            print("CRITICAL: No active player found to continue the game.")
            game.reset()
            return
        
        print(f"DEBUG: Next player is {current_player.user_id} at index {game.current_player_index}.")
        game.last_turn_time = datetime.datetime.now()
        current_player_money = current_player.wallet.value()

        print(f"DEBUG: Sending turn actions to player {current_player.user_id}.")
        msg_id = self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=current_player,
            money=current_player_money,
        )

        if msg_id:
            if game.turn_message_id: # Remove previous turn message's keyboard
                try:
                    self._view.remove_markup(chat_id, game.turn_message_id)
                except Exception: pass
            game.turn_message_id = msg_id
            print(f"INFO: Turn message sent. New ID: {msg_id}")
        else:
            print(f"CRITICAL: Failed to send turn message for chat {chat_id}.")
            self._view.send_message(chat_id, "خطای جدی در ارسال پیام نوبت رخ داد. بازی متوقف شد.")
            game.reset()

    def _fast_forward_to_finish(self, game: Game, chat_id: ChatId):
        """ When no more betting is possible, reveals all remaining cards and finishes. """
        print("DEBUG: Fast-forwarding to finish.")
        self._round_rate.to_pot(game)
        
        cards_to_deal = 5 - len(game.cards_table)
        if cards_to_deal > 0:
            self.add_cards_to_table(cards_to_deal, game, chat_id)

        # Set game state to a "finished-like" state to prevent further actions
        game.state = GameState.ROUND_RIVER 
        self._finish(game, chat_id)

    def bonus(self, update: Update, context: CallbackContext) -> None:
        wallet = WalletManagerModel(update.effective_message.from_user.id, self._kv)
        money = wallet.value()
        chat_id = update.effective_message.chat_id
        message_id = update.effective_message.message_id

        if wallet.has_daily_bonus():
            return self._view.send_message_reply(
                chat_id=chat_id,
                message_id=message_id,
                text=f"💰 پولت: *{money}$*\n",
            )

        SATURDAY = 5
        if datetime.datetime.today().weekday() == SATURDAY:
            dice_msg = self._view.send_dice_reply(chat_id=chat_id, message_id=message_id, emoji='🎰')
            icon = '🎰'
            bonus = dice_msg.dice.value * 20
        else:
            dice_msg = self._view.send_dice_reply(chat_id=chat_id, message_id=message_id)
            icon = DICES[dice_msg.dice.value - 1]
            bonus = BONUSES[dice_msg.dice.value - 1]

        new_money = wallet.add_daily(amount=bonus)
        
        def print_bonus() -> None:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=dice_msg.message_id,
                text=f"🎁 پاداش: *{bonus}$* {icon}\n💰 پولت: *{new_money}$*\n",
            )
        Timer(DICE_DELAY_SEC, print_bonus).start()


    def send_cards_to_user(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        current_player = next((p for p in game.players if p.user_id == update.effective_user.id), None)
        if not current_player or not current_player.cards:
            return
        self._view.send_cards(
            chat_id=update.effective_message.chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=update.effective_message.message_id,
        )

    def _send_cards_private(self, player: Player, cards: Cards) -> None:
        user_chat_model = UserPrivateChatModel(user_id=player.user_id, kv=self._kv)
        private_chat_id = user_chat_model.get_chat_id()
        if not private_chat_id:
            raise ValueError("private chat not found")

        private_chat_id = private_chat_id.decode('utf-8')
        message = self._view.send_desk_cards_img(
            chat_id=private_chat_id, cards=cards, caption="Your cards", disable_notification=False
        )
        if not message: return

        # Clean up old card messages
        while True:
            rm_msg_id = user_chat_model.pop_message()
            if rm_msg_id is None: break
            try:
                self._view.remove_message(chat_id=private_chat_id, message_id=rm_msg_id.decode('utf-8'))
            except Exception as e:
                print(f"Could not remove old private card message: {e}")
        user_chat_model.push_message(message.message_id)

    def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        for player in game.players:
            if len(game.remain_cards) < 2: 
                print("Error: Not enough cards to deal.")
                return
            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards
            try:
                self._send_cards_private(player=player, cards=cards)
                continue
            except Exception as e:
                print(f"Could not send private cards to {player.user_id}: {e}")

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

        msg = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"💰 پات فعلی: {game.pot}$",
        )
        if msg: game.message_ids_to_delete.append(msg.message_id)

    def _finish(self, game: Game, chat_id: ChatId) -> None:
        # ===== اصلاح منطق to_pot =====
        self._round_rate.to_pot(game)
        print(f"INFO: Game finished: {game.id}, players count: {len(game.players)}, pot: {game.pot}")

        active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        player_scores = self._winner_determine.determinate_scores(active_players, game.cards_table)
        winners_hand_money = self._round_rate.finish_rate(game, player_scores)
        
        text = "🏁 بازی با این نتیجه تموم شد:\n\n"
        if not winners_hand_money and len(active_players) == 1:
            # Handle case where only one player is left (all others folded)
            winner = active_players[0]
            winner.wallet.inc(game.pot)
            text += f"{winner.mention_markdown} برنده شد و *{game.pot}$* گرفت چون بقیه فولد دادند."
        else:
            for (player, best_hand, money) in winners_hand_money:
                text += f"{player.mention_markdown}:\n🏆 گرفتی: *{money} $*\n"
                if best_hand:
                    win_hand = " ".join(best_hand)
                    text += f"🃏 با ترکیب این کارتا:\n{win_hand}\n\n"
        
        text += "\n/ready برای ادامه"
        self._view.send_message(chat_id=chat_id, text=text, reply_markup=ReplyKeyboardRemove())
        
        for player in game.players:
            player.wallet.approve(game.id)
            
        # ===== اصلاح منطق حذف پیام‌ها =====
        if hasattr(game, 'message_ids_to_delete'):
            for msg_id in game.message_ids_to_delete:
                try:
                    self._view.remove_message(chat_id, msg_id)
                except Exception as e:
                    print(f"Could not delete message {msg_id}: {e}")

        game.reset()

    def _goto_next_round(self, game: Game, chat_id: ChatId):
        transitions = {
            GameState.ROUND_PRE_FLOP: ("ROUND_FLOP", lambda: self.add_cards_to_table(3, game, chat_id)),
            GameState.ROUND_FLOP: ("ROUND_TURN", lambda: self.add_cards_to_table(1, game, chat_id)),
            GameState.ROUND_TURN: ("ROUND_RIVER", lambda: self.add_cards_to_table(1, game, chat_id)),
            GameState.ROUND_RIVER: ("INITIAL", lambda: self._finish(game, chat_id))
        }
        next_state_str, processor = transitions.get(game.state, (None, None))
        if next_state_str:
            game.state = GameState(next_state_str)
            processor()

    def middleware_user_turn(self, fn: Handler) -> Handler:
        def m(update, context):
            game = self._game_from_context(context)
            if game.state not in self.ACTIVE_GAME_STATES: return
            
            current_player = self._current_turn_player(game)
            if not current_player or update.callback_query.from_user.id != current_player.user_id: return
            
            fn(update, context) 
            
            # Markup is now removed from within _process_playing or show_table
            # to handle the most current turn_message_id
        return m
    
    def ban_player(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id

        if game.state not in self.ACTIVE_GAME_STATES: return

        if not hasattr(game, 'last_turn_time'): return

        diff = datetime.datetime.now() - game.last_turn_time
        if diff < MAX_TIME_FOR_TURN:
            self._view.send_message(
                chat_id=chat_id,
                text="⏳ نمی‌تونی محروم کنی. حداکثر زمان نوبت ۲ دقیقه‌س",
            )
            return

        self._view.send_message(chat_id=chat_id, text="⏰ وقت تموم شد!")
        self.fold(update, context)

    def _action_handler(self, update: Update, context: CallbackContext, action_logic):
        game = self._game_from_context(context)
        player = self._current_turn_player(game)

        if not player or player.user_id != update.effective_user.id:
            return
            
        try:
            action_logic(game, player)
            player.has_acted = True
        except UserException as e:
            msg_id = self._view.send_message_return_id(chat_id=update.effective_chat.id, text=str(e))
            if msg_id and hasattr(game, 'message_ids_to_delete'): game.message_ids_to_delete.append(msg_id)
            return
        
        self._process_playing(
            chat_id=update.effective_message.chat_id,
            game=game,
        )

    def fold(self, update: Update, context: CallbackContext) -> None:
        def logic(game, player):
            player.state = PlayerState.FOLD
            msg_id = self._view.send_message_return_id(
                chat_id=update.effective_message.chat_id,
                text=f"{player.mention_markdown} {PlayerAction.FOLD.value}"
            )
            if msg_id and hasattr(game, 'message_ids_to_delete'): game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)

    def call_check(self, update: Update, context: CallbackContext) -> None:
        def logic(game, player):
            action_str = PlayerAction.CALL.value if player.round_rate < game.max_round_rate else PlayerAction.CHECK.value
            
            amount_to_call = game.max_round_rate - player.round_rate
            if player.wallet.value() < amount_to_call:
                # Force all-in if cannot call
                all_in_amount, mention = self._round_rate.all_in(game, player)
                msg_text = f"{mention} {PlayerAction.ALL_IN.value} {all_in_amount}$ (به دلیل عدم توانایی در کال)"
            else:
                self._round_rate.call_check(game, player)
                msg_text = f"{player.mention_markdown} {action_str}"
            
            msg_id = self._view.send_message_return_id(chat_id=update.effective_chat.id, text=msg_text)
            if msg_id and hasattr(game, 'message_ids_to_delete'): game.message_ids_to_delete.append(msg_id)

        self._action_handler(update, context, logic)

    def raise_rate_bet(self, update: Update, context: CallbackContext, raise_bet_amount: Money) -> None:
        def logic(game, player):
            action = PlayerAction.RAISE_RATE if game.max_round_rate > 0 else PlayerAction.BET
            amount, mention = self._round_rate.raise_bet(game, player, raise_bet_amount)
            msg_id = self._view.send_message_return_id(
                chat_id=update.effective_chat.id,
                text=f"{mention} {action.value} {amount}$"
            )
            if msg_id and hasattr(game, 'message_ids_to_delete'): game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)

    def all_in(self, update: Update, context: CallbackContext) -> None:
        def logic(game, player):
            amount, mention = self._round_rate.all_in(game, player)
            msg_id = self._view.send_message_return_id(
                chat_id=update.effective_chat.id,
                text=f"{mention} {PlayerAction.ALL_IN.value} {amount}$"
            )
            if msg_id and hasattr(game, 'message_ids_to_delete'): game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)

class WalletManagerModel(Wallet):
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv: redis.Redis = kv
        self._money_key = f"money:{self._user_id}"
        self._daily_bonus_key = f"daily_bonus_time:{self._user_id}"

    def value(self) -> Money:
        money = self._kv.get(self._money_key)
        if money is None:
            self.set(DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(money)

    def set(self, amount: Money) -> None:
        self._kv.set(self._money_key, amount)

    def inc(self, amount: Money) -> Money:
        return self._kv.incrby(self._money_key, amount)

    def authorized_money(self, game_id: str) -> Money:
        auth_money = self._kv.get(f"auth:{game_id}:{self._user_id}")
        return int(auth_money) if auth_money else 0

    def authorize(self, game_id: str, amount: Money) -> None:
        self._kv.set(f"auth:{game_id}:{self._user_id}", amount)

    def approve(self, game_id: str) -> None:
        self._kv.delete(f"auth:{game_id}:{self._user_id}")
        
    def add_daily(self, amount: Money) -> Money:
        self._kv.set(
            self._daily_bonus_key,
            datetime.datetime.now().timestamp()
        )
        return self.inc(amount)

    def has_daily_bonus(self) -> bool:
        last_time_str = self._kv.get(self._daily_bonus_key)
        if last_time_str is None:
            return False

        last_time = datetime.datetime.fromtimestamp(
            float(last_time_str)
        )
        diff = datetime.datetime.now() - last_time
        return diff.total_seconds() < ONE_DAY

class RoundRateModel:
    def round_pre_flop_rate_before_first_turn(self, game: Game):
        if len(game.players) < 2: return
        
        for p in game.players:
            p.wallet.authorize(game.id, p.wallet.value())

        sb_player_index = 0
        bb_player_index = 1
        
        sb_player = game.players[sb_player_index]
        bb_player = game.players[bb_player_index]
        
        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.round_rate = sb_amount
        sb_player.wallet.inc(-sb_amount)

        bb_amount = min(2 * SMALL_BLIND, bb_player.wallet.value())
        bb_player.round_rate = bb_amount
        bb_player.wallet.inc(-bb_amount)
        
        game.max_round_rate = bb_amount
        game.trading_end_user_id = bb_player.user_id
        
        sb_player.has_acted = True
        bb_player.has_acted = True


    def call_check(self, game: Game, player: Player):
        amount = game.max_round_rate - player.round_rate
        if player.wallet.value() < amount:
            raise UserException("پول کافی برای کال نداری. باید All-in کنی.")
        player.round_rate += amount
        player.wallet.inc(-amount)

    def raise_bet(self, game: Game, player: Player, raise_bet_amount: Money) -> Tuple[Money, Mention]:
        amount_to_call = game.max_round_rate - player.round_rate
        total_bet_amount = amount_to_call + raise_bet_amount

        if player.wallet.value() < total_bet_amount:
            raise UserException("پول کافی برای این رِیز نداری.")

        player.round_rate += total_bet_amount
        player.wallet.inc(-total_bet_amount)
        game.max_round_rate = player.round_rate
        game.trading_end_user_id = player.user_id
        
        for p in game.players:
            if p.user_id != player.user_id:
                p.has_acted = False

        return raise_bet_amount, player.mention_markdown

    def all_in(self, game: Game, player: Player) -> Tuple[Money, Mention]:
        amount = player.wallet.value()
        player.round_rate += amount
        player.wallet.set(0)
        player.state = PlayerState.ALL_IN

        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate
            game.trading_end_user_id = player.user_id
            for p in game.players:
                if p.user_id != player.user_id:
                    p.has_acted = False

        return amount, player.mention_markdown

    def finish_rate(self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]) -> List[Tuple[Player, Cards, Money]]:
        all_players_in_hand = [p for p in game.players if p.wallet.authorized_money(game.id) > 0]
        if not all_players_in_hand:
            active_winners = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
            if not active_winners: return []
            winner = active_winners[0]
            winner.wallet.inc(game.pot)
            return [(winner, winner.cards, game.pot)]

        total_bets = {p.user_id: p.wallet.authorized_money(game.id) for p in all_players_in_hand}
        sorted_bets = sorted(list(set(total_bets.values())))
        
        pots = []
        last_bet_level = 0
        for bet_level in sorted_bets:
            pot_amount = 0
            eligible_players = []
            
            for player in all_players_in_hand:
                contribution = min(total_bets[player.user_id], bet_level) - last_bet_level
                if contribution > 0:
                    pot_amount += contribution
                    eligible_players.append(player)
            
            if pot_amount > 0:
                pots.append({"amount": pot_amount, "eligible_players": eligible_players})
            
            last_bet_level = bet_level

        final_winnings = {}
        for pot in pots:
            eligible_winners = []
            best_score_in_pot = -1

            for score, players_with_score in player_scores.items():
                for player, hand in players_with_score:
                    if player in pot["eligible_players"]:
                        if score > best_score_in_pot:
                            best_score_in_pot = score
                            eligible_winners = [(player, hand)]
                        elif score == best_score_in_pot:
                            eligible_winners.append((player, hand))
            
            if not eligible_winners: continue

            win_share = round(pot["amount"] / len(eligible_winners))
            for winner, hand in eligible_winners:
                winner.wallet.inc(win_share)
                
                if winner.user_id not in final_winnings:
                    final_winnings[winner.user_id] = {"player": winner, "hand": hand, "money": 0}
                final_winnings[winner.user_id]["money"] += win_share
        
        return [(v["player"], v["hand"], v["money"]) for v in final_winnings.values()]

