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

MAX_PLAYERS = 8
MIN_PLAYERS = 2
SMALL_BLIND = 5
DEFAULT_MONEY = 1000
MAX_TIME_FOR_TURN = datetime.timedelta(minutes=2)
DESCRIPTION_FILE = "assets/description_bot.md"

class PokerBotModel:
    
    # --- ویژگی اضافه شده برای رفع خطا ---
    ACTIVE_GAME_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }
    # -----------------------------------

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
    def _current_turn_player(game: Game) -> Optional[Player]:
        if not game.players or game.current_player_index < 0 or game.current_player_index >= len(game.players):
            return None
        i = game.current_player_index
        return game.players[i]

    def ready(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state != GameState.INITIAL:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!"
            )
            return

        if len(game.players) >= MAX_PLAYERS:
            self._view.send_message_reply(
                chat_id=chat_id,
                text="🚪 اتاق پر است!",
                message_id=update.effective_message.message_id,
            )
            return

        user = update.effective_message.from_user
        if user.id in game.ready_users:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="✅ شما از قبل آماده‌اید.",
            )
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < 2 * SMALL_BLIND:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text=f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {2*SMALL_BLIND}$ نیاز است).",
            )
            return

        player = Player(
            user_id=user.id,
            mention_markdown=user.mention_markdown(),
            wallet=wallet,
            ready_message_id=update.effective_message.message_id,
        )

        game.ready_users.add(user.id)
        game.players.append(player)
        
        self._view.send_message(
            chat_id=chat_id,
            text=(f"{player.mention_markdown} اعلام آمادگی کرد. \n"
                  f"بازیکنان آماده: {len(game.players)}/{MAX_PLAYERS}")
        )

        try:
             members_count = self._bot.get_chat_member_count(chat_id)
             players_active = len(game.players)
             # One is the bot.
             if players_active >= self._min_players and (players_active == members_count - 1 or self._cfg.DEBUG):
                 self._start_game(context=context, game=game, chat_id=chat_id)
        except Exception as e:
            print(f"Error checking member count or starting game: {e}")


    def stop(self, user_id: UserId) -> None:
        UserPrivateChatModel(user_id=user_id, kv=self._kv).delete()

    def start(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user_id = update.effective_message.from_user.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            self._view.send_message(
                chat_id=chat_id,
                text="🎮 یک بازی در حال حاضر در جریان است."
            )
            return
            
        if game.state == GameState.FINISHED:
            game.reset()

        if update.effective_chat.type == 'private':
            with open(DESCRIPTION_FILE, 'r', encoding='utf-8') as f:
                text = f.read()
            self._view.send_message(chat_id=chat_id, text=text)
            self._view.send_photo(chat_id=chat_id)
            UserPrivateChatModel(user_id=user_id, kv=self._kv).set_chat_id(chat_id=chat_id)
            return

        players_active = len(game.players)
        if players_active >= self._min_players:
            self._start_game(context=context, game=game, chat_id=chat_id)
        else:
            self._view.send_message(
                chat_id=chat_id,
                text=f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر)."
            )

    def _start_game(
        self,
        context: CallbackContext,
        game: Game,
        chat_id: ChatId
    ) -> None:
        print(f"New game: {game.id}, players count: {len(game.players)}")

        for msg_id in game.message_ids_to_delete:
            self._view.remove_message(chat_id, msg_id)
        game.message_ids_to_delete.clear()

        # Clear ready messages
        for p in game.players:
            self._view.remove_message(chat_id, p.ready_message_id)

        msg_id = self._view.send_message(
            chat_id=chat_id,
            text='🚀 !بازی شروع شد!',
        )
        if msg_id: game.message_ids_to_delete.append(msg_id)

        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
        if old_players_ids:
            old_players_ids = old_players_ids[1:] + old_players_ids[:1]
            
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
        # In Heads-Up (2 players), Small Blind acts first before the flop.
        # Dealer (button) is player 0, SB is player 0, BB is player 1. SB acts first.
        if num_players == 2:
            game.current_player_index = -1 # will be incremented to 0
        else:
            # In 3+ player games, player after Big Blind (UTG) acts first.
            # Dealer is 0, SB is 1, BB is 2. UTG is 3 (or 0 if 3 players).
            game.current_player_index = 1 # will be incremented to 2

        self._process_playing(chat_id=chat_id, game=game)

        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]

    def _fast_forward_to_finish(self, game: Game, chat_id: ChatId):
        """ When no more betting is possible, reveals all remaining cards """
        print("Fast-forwarding to finish...")
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
        wallet = WalletManagerModel(
            update.effective_message.from_user.id, self._kv)
        money = wallet.value()

        chat_id = update.effective_chat.id
        message_id = update.effective_message.message_id

        if wallet.has_daily_bonus():
            return self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text=f"💰 پولت: *{money}$*\n",
            )

        icon: str
        dice_msg: Message
        bonus: Money

        SATURDAY = 5
        if datetime.datetime.today().weekday() == SATURDAY:
            dice_msg = self._view.send_dice_reply(
                chat_id=chat_id,
                message_id=message_id,
                emoji='🎰'
            )
            icon = '🎰'
            bonus = dice_msg.dice.value * 20
        else:
            dice_msg = self._view.send_dice_reply(
                chat_id=chat_id,
                message_id=message_id,
            )
            icon = DICES[dice_msg.dice.value-1]
            bonus = BONUSES[dice_msg.dice.value - 1]

        message_id = dice_msg.message_id
        money = wallet.add_daily(amount=bonus)

        def print_bonus() -> None:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=message_id,
                text=f"🎁 پاداش: *{bonus}$* {icon}\n" +
                f"💰 پولت: *{money}$*\n",
            )

        Timer(DICE_DELAY_SEC, print_bonus).start()

    def send_cards_to_user(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        game = self._game_from_context(context)

        current_player = None
        for player in game.players:
            if player.user_id == update.effective_user.id:
                current_player = player
                break

        if current_player is None or not current_player.cards:
            return

        self._view.send_cards(
            chat_id=update.effective_chat.id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=update.effective_message.message_id,
        )

    def _check_access(self, chat_id: ChatId, user_id: UserId) -> bool:
        chat_admins = self._bot.get_chat_administrators(chat_id)
        for m in chat_admins:
            if m.user.id == user_id:
                return True
        return False

    def _send_cards_private(self, player: Player, cards: Cards) -> None:
        user_chat_model = UserPrivateChatModel(
            user_id=player.user_id,
            kv=self._kv,
        )
        private_chat_id = user_chat_model.get_chat_id()

        if private_chat_id is None:
            raise ValueError(f"private chat not found for user {player.user_id}")

        private_chat_id = private_chat_id.decode('utf-8')

        # Clean up old card messages
        try:
            rm_msg_id = user_chat_model.pop_message()
            while rm_msg_id is not None:
                try:
                    rm_msg_id = rm_msg_id.decode('utf-8')
                    self._view.remove_message(
                        chat_id=private_chat_id,
                        message_id=rm_msg_id,
                    )
                except Exception:
                    pass
                rm_msg_id = user_chat_model.pop_message()
        except Exception as ex:
            print(f"Error cleaning private messages: {ex}")

        # Send new cards
        message_id = self._view.send_desk_cards_img(
            chat_id=private_chat_id,
            cards=cards,
            caption="کارت‌های شما",
            disable_notification=True,
        ).message_id
        
        user_chat_model.push_message(message_id=message_id)

    def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        for player in game.players:
            if len(game.remain_cards) < 2:
                self._view.send_message(chat_id, "کارت‌های کافی در دسته وجود ندارد!")
                game.reset()
                return
            cards = player.cards = [
                game.remain_cards.pop(),
                game.remain_cards.pop(),
            ]

            try:
                self._send_cards_private(player=player, cards=cards)
            except Exception as ex:
                print(ex)
                self._view.send_message(
                    chat_id,
                    f"⚠️ {player.mention_markdown} ربات را در چت خصوصی استارت نکرده است. "
                    "ارسال کارت‌ها در گروه انجام می‌شود. لطفاً ربات را استارت کنید."
                )
                msg_id = self._view.send_cards(
                    chat_id=chat_id,
                    cards=cards,
                    mention_markdown=player.mention_markdown,
                    ready_message_id=player.ready_message_id,
                )
                if msg_id: game.message_ids_to_delete.append(msg_id)

    def _process_playing(self, chat_id: ChatId, game: Game) -> None:
        if game.state not in self.ACTIVE_GAME_STATES:
            return

        active_and_all_in_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(active_and_all_in_players) <= 1:
            self._finish(game, chat_id)
            return

        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        
        # Condition to end betting round
        round_over = False
        if active_players:
            # Check if all active players have acted and contributed the same amount.
            all_acted = all(p.has_acted for p in active_players)
            all_matched = len(set(p.round_rate for p in active_players)) == 1
            if all_acted and all_matched and game.max_round_rate > 0:
                round_over = True
        else:
            # No active players left, only ALL_IN players
            round_over = True
        
        # Special case for Pre-flop: Big Blind can still act if no one raised.
        big_blind_player = game.players[1 % len(game.players)] if len(game.players) > 1 else None
        if (game.state == GameState.ROUND_PRE_FLOP and
            big_blind_player and
            not big_blind_player.has_acted and
            game.max_round_rate == 2 * SMALL_BLIND and
            all(p.round_rate == game.max_round_rate for p in active_players if p.has_acted)):
             round_over = False

        if round_over:
            if game.all_in_players_are_covered() and len(active_players) > 0:
                self._fast_forward_to_finish(game, chat_id)
            else:
                self._round_rate.to_pot(game)
                self._goto_next_round(game, chat_id)
                if game.state in (GameState.INITIAL, GameState.FINISHED):
                    return
                # Start next round
                game.current_player_index = -1 # Start from player after dealer (index 0)
                for p in game.players:
                    if p.state == PlayerState.ACTIVE:
                        p.has_acted = False
                self._process_playing(chat_id, game)
            return

        # Find next active player
        start_index = game.current_player_index
        while True:
            game.current_player_index = (game.current_player_index + 1) % len(game.players)
            current_player = self._current_turn_player(game)
            if current_player.state == PlayerState.ACTIVE:
                break
            if game.current_player_index == start_index:
                # Full circle without finding an active player, should not happen if checked before
                self._finish(game, chat_id)
                return

        game.last_turn_time = datetime.datetime.now()
        current_player_money = current_player.wallet.value()

        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)

        msg_id = self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=current_player,
            money=current_player_money,
        )
        game.turn_message_id = msg_id

    def add_cards_to_table(
        self,
        count: int,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        for _ in range(count):
            if not game.remain_cards: break
            game.cards_table.append(game.remain_cards.pop())

        msg_id = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"💰 پات فعلی: {game.pot}$",
        )
        if msg_id:
            game.message_ids_to_delete.append(msg_id)

    def _finish(
        self,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        self._round_rate.to_pot(game)
        print(f"Game finished: {game.id}, pot: {game.pot}")

        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None

        active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        
        if not active_players:
            # Should not happen, but handle it
            text = "بازی بدون برنده تمام شد."
        elif len(active_players) == 1:
            winner = active_players[0]
            winner.wallet.inc(game.pot)
            text = f"🏁 بازی تمام شد!\n\n{winner.mention_markdown} با فولد بقیه، برنده *{game.pot}$* شد!\n\n"
        else:
            player_scores = self._winner_determine.determinate_scores(active_players, game.cards_table)
            winners_hand_money = self._round_rate.finish_rate(game, player_scores)
            text = "🏁 بازی با این نتیجه تمام شد:\n\n"
            for (player, best_hand, money) in winners_hand_money:
                win_hand = " ".join(map(str, best_hand))
                text += f"{player.mention_markdown}:\n🏆 برنده *{money}$* شد\n"
                text += f"🃏 با ترکیب: {win_hand}\n\n"
        
        text += "برای شروع بازی جدید /ready را بزنید یا با /start همه را وادار به شروع کنید."
        self._view.send_message(chat_id=chat_id, text=text)

        for player in game.players:
            player.wallet.approve(game.id)

        game.state = GameState.FINISHED

    def _goto_next_round(self, game: Game, chat_id: ChatId) -> bool:
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if len(active_players) < 2 and len(game.players_by(states=(PlayerState.ALL_IN,))) > 0:
            if game.all_in_players_are_covered():
                 self._fast_forward_to_finish(game, chat_id)
                 return False
        
        state_transitions = {
            GameState.ROUND_PRE_FLOP: {"next_state": GameState.ROUND_FLOP, "processor": lambda: self.add_cards_to_table(3, game, chat_id)},
            GameState.ROUND_FLOP: {"next_state": GameState.ROUND_TURN, "processor": lambda: self.add_cards_to_table(1, game, chat_id)},
            GameState.ROUND_TURN: {"next_state": GameState.ROUND_RIVER, "processor": lambda: self.add_cards_to_table(1, game, chat_id)},
            GameState.ROUND_RIVER: {"next_state": GameState.FINISHED, "processor": lambda: self._finish(game, chat_id)}
        }

        if game.state not in state_transitions:
            raise Exception("Unexpected game state: " + str(game.state))

        transition = state_transitions[game.state]
        game.state = transition["next_state"]
        transition["processor"]()
        return game.state != GameState.FINISHED

    def middleware_user_turn(self, fn: Handler) -> Handler:
        def m(update: Update, context: CallbackContext):
            query = update.callback_query
            user_id = query.from_user.id
            chat_id = query.message.chat_id
            
            game = self._game_from_context(context)
            if game.state not in self.ACTIVE_GAME_STATES:
                query.answer(text="بازی فعال نیست.", show_alert=True)
                return

            current_player = self._current_turn_player(game)
            if not current_player or user_id != current_player.user_id:
                query.answer(text="نوبت شما نیست!", show_alert=False)
                return

            # Remove buttons after click
            if game.turn_message_id:
                self._view.remove_markup(
                    chat_id=chat_id,
                    message_id=game.turn_message_id,
                )
                game.turn_message_id = None
            
            query.answer() # Acknowledge the button press
            fn(update, context)

        return m

    def ban_player(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state not in self.ACTIVE_GAME_STATES:
            return

        current_player = self._current_turn_player(game)
        if not current_player: return

        diff = datetime.datetime.now() - game.last_turn_time
        if diff < MAX_TIME_FOR_TURN:
            remaining = (MAX_TIME_FOR_TURN - diff).seconds
            self._view.send_message(
                chat_id=chat_id,
                text=f"⏳ نمی‌توانید محروم کنید. هنوز {remaining} ثانیه از زمان بازیکن ({current_player.mention_markdown}) باقی مانده است.",
            )
            return

        self._view.send_message(
            chat_id=chat_id,
            text=f"⏰ وقت بازیکن {current_player.mention_markdown} تمام شد!",
        )
        self.fold(update, context, is_ban=True)

    def fold(self, update: Update, context: CallbackContext, is_ban: bool = False) -> None:
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        
        if not player: return

        player.state = PlayerState.FOLD
        player.has_acted = True

        action_text = "محروم و فولد شد" if is_ban else PlayerAction.FOLD.value
        self._view.send_message(
            chat_id=update.effective_chat.id,
            text=f"{player.mention_markdown} {action_text}"
        )

        self._process_playing(
            chat_id=update.effective_chat.id,
            game=game,
        )

    def call_check(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        player = self._current_turn_player(game)

        if not player: return

        action = PlayerAction.CALL.value if player.round_rate < game.max_round_rate else PlayerAction.CHECK.value

        try:
            amount_to_call = game.max_round_rate - player.round_rate
            if player.wallet.value() <= amount_to_call:
                return self.all_in(update=update, context=context)

            mention_markdown = player.mention_markdown
            self._view.send_message(
                chat_id=chat_id,
                text=f"{mention_markdown} {action}"
            )

            self._round_rate.call_check(game, player)
        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=str(e))
            return

        self._process_playing(
            chat_id=chat_id,
            game=game,
        )

    def raise_rate_bet(
        self,
        update: Update,
        context: CallbackContext,
        raise_bet_rate: PlayerAction
    ) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        player = self._current_turn_player(game)
        
        if not player: return

        try:
            action = PlayerAction.RAISE_RATE if game.max_round_rate > 0 else PlayerAction.BET
            
            amount = self._round_rate.raise_rate_bet(
                game, player, raise_bet_rate
            )

            if amount > player.wallet.value():
                self._view.send_message(chat_id, "موجودی شما برای این مقدار رِیز کافی نیست.")
                return

            mention_markdown = player.mention_markdown
            self._view.send_message(
                chat_id=chat_id,
                text=f"{mention_markdown} {action.value} {amount}$"
            )

        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=str(e))
            return

        self._process_playing(chat_id, game)

    def all_in(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        player = self._current_turn_player(game)

        if not player: return
        
        try:
            amount = self._round_rate.all_in(game, player)
            mention_markdown = player.mention_markdown
            self._view.send_message(
                chat_id=chat_id,
                text=f"{mention_markdown} {PlayerAction.ALL_IN.value} با {amount}$"
            )
        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=str(e))
            return

        self._process_playing(
            chat_id=chat_id,
            game=game,
        )

class WalletManagerModel(Wallet):
    _kv: redis.Redis
    _user_id: UserId

    def __init__(self, user_id, kv):
        self._user_id = user_id
        self._kv = kv

        if not self._kv.exists(self._prefix(user_id)):
            self._kv.set(self._prefix(user_id), DEFAULT_MONEY)

    @staticmethod
    def _prefix(user_id: int, suffix: str = ""):
        return f"poker:wallet:{user_id}:{suffix}"

    def add_daily(self, amount: Money) -> Money:
        self._kv.set(
            self._prefix(self._user_id, "last_time"),
            datetime.datetime.now().timestamp(),
        )
        self.inc(amount)
        return self.value()

    def has_daily_bonus(self):
        last_time_b = self._kv.get(
            self._prefix(self._user_id, "last_time"))
        if last_time_b is None:
            return False
        last_time = datetime.datetime.fromtimestamp(float(last_time_b))
        now_time = datetime.datetime.now()
        return now_time.date() == last_time.date()

    def inc(self, amount: Money = 0) -> None:
        self._kv.incr(self._prefix(self._user_id), amount)

    def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        self._kv.incr(
            self._prefix(self._user_id, f"auth:{game_id}"),
            amount,
        )

    def authorized_money(self, game_id: str) -> Money:
        authorized_money = self._kv.get(
            self._prefix(self._user_id, f"auth:{game_id}"))
        if authorized_money is None:
            return 0
        return int(authorized_money)

    def authorize(self, game_id: str, amount: Money) -> None:
        authorized_money = self.authorized_money(game_id)
        if amount + authorized_money > self.value():
            raise UserException("Not enough money")

        self.inc_authorized_money(game_id, amount)

    def authorize_all(self, game_id: str) -> Money:
        amount = self.value()
        self.inc_authorized_money(game_id, amount)
        return amount

    def value(self) -> Money:
        return int(self._kv.get(self._prefix(self._user_id)))

    def approve(self, game_id: str) -> None:
        # This function seems to be for reconciliation after a game
        # It deducts the authorized money (total bet) from the wallet
        authorized = self.authorized_money(game_id)
        # self.inc(-authorized) # This seems wrong logic, money is already deducted
        self._kv.delete(self._prefix(self._user_id, f"auth:{game_id}"))

    def cancel(self, game_id: str) -> None:
        self._kv.delete(self._prefix(self._user_id, f"auth:{game_id}"))

class RoundRateModel:
    def round_pre_flop_rate_before_first_turn(self, game: Game) -> None:
        for p in game.players:
            p.wallet.authorize(game.id, p.wallet.value())

        num_players = len(game.players)
        if num_players < 2: return

        # Dealer is at index 0
        sb_player = game.players[0]
        bb_player = game.players[1]
        
        if num_players == 2: # Heads-up case
             sb_player = game.players[0] # Dealer is SB
             bb_player = game.players[1] # Other player is BB
        else: # 3+ players
             sb_player = game.players[1 % num_players] # Player after dealer
             bb_player = game.players[2 % num_players] # Player after SB

        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.round_rate += sb_amount
        sb_player.total_bet += sb_amount
        sb_player.wallet.inc(-sb_amount)
        print(f"{sb_player.mention_markdown} posts Small Blind: {sb_amount}")

        bb_amount = min(2 * SMALL_BLIND, bb_player.wallet.value())
        bb_player.round_rate += bb_amount
        bb_player.total_bet += bb_amount
        bb_player.wallet.inc(-bb_amount)
        print(f"{bb_player.mention_markdown} posts Big Blind: {bb_amount}")

        if sb_player.wallet.value() == 0: sb_player.state = PlayerState.ALL_IN
        if bb_player.wallet.value() == 0: bb_player.state = PlayerState.ALL_IN
        
        sb_player.has_acted = False
        bb_player.has_acted = False

        game.max_round_rate = 2 * SMALL_BLIND

    def call_check(self, game: Game, player: Player) -> None:
        amount = game.max_round_rate - player.round_rate
        if player.wallet.value() < amount:
            raise UserException("Not enough money for call")

        player.round_rate += amount
        player.total_bet += amount
        player.wallet.inc(-amount)
        player.has_acted = True

        if player.wallet.value() == 0:
            player.state = PlayerState.ALL_IN
    
    def raise_rate_bet(
        self, game: Game, player: Player, raise_bet_rate: PlayerAction
    ) -> Money:
        
        last_raise_size = game.max_round_rate - (game.last_raiser_bet if hasattr(game, 'last_raiser_bet') else 0)
        min_raise_amount = max(last_raise_size, 2*SMALL_BLIND)

        raise_amount = 0
        if raise_bet_rate == PlayerAction.SMALL:
            raise_amount = min_raise_amount
        elif raise_bet_rate == PlayerAction.NORMAL:
            raise_amount = max(game.pot // 2, min_raise_amount)
        elif raise_bet_rate == PlayerAction.BIG:
            raise_amount = max(game.pot, min_raise_amount)
        
        raise_amount = min(raise_amount, player.wallet.value())

        amount_to_call = game.max_round_rate - player.round_rate
        total_bet_this_turn = amount_to_call + raise_amount

        if player.wallet.value() < total_bet_this_turn:
            raise UserException("Not enough money for this raise")

        player.round_rate += total_bet_this_turn
        player.total_bet += total_bet_this_turn
        player.wallet.inc(-total_bet_this_turn)
        player.has_acted = True
        
        game.last_raiser_bet = game.max_round_rate
        game.max_round_rate = player.round_rate

        for p in game.players:
            if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                p.has_acted = False

        if player.wallet.value() == 0:
            player.state = PlayerState.ALL_IN
        
        return raise_amount

    def all_in(self, game: Game, player: Player) -> Money:
        amount = player.wallet.value()
        player.round_rate += amount
        player.total_bet += amount
        player.wallet.inc(-amount)
        player.state = PlayerState.ALL_IN
        player.has_acted = True
        
        if player.total_bet > game.max_round_rate:
             game.last_raiser_bet = game.max_round_rate
             game.max_round_rate = player.total_bet
             for p in game.players:
                 if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                     p.has_acted = False
        return amount

    def finish_rate(
        self,
        game: Game,
        player_scores: Dict[Score, List[Tuple[Player, Cards]]],
    ) -> List[Tuple[Player, Cards, Money]]:
        
        all_players_in_hand = sorted([p for p in game.players if p.total_bet > 0], key=lambda p: p.total_bet)
        pots = []

        last_bet = 0
        while any(p.total_bet > last_bet for p in all_players_in_hand):
            active_players_in_pot = [p for p in all_players_in_hand if p.total_bet > last_bet]
            if not active_players_in_pot: break

            min_bet_this_pot = min(p.total_bet for p in active_players_in_pot)
            pot_size = 0
            eligible_player_ids = set()

            for p in all_players_in_hand:
                contribution = min(max(0, p.total_bet - last_bet), min_bet_this_pot - last_bet)
                if contribution > 0:
                    pot_size += contribution
                    eligible_player_ids.add(p.user_id)
            
            if pot_size > 0:
                pots.append({'size': pot_size, 'eligible': eligible_player_ids})
            
            last_bet = min_bet_this_pot
        
        winners_summary = {} # user_id -> {player, hand, money}
        sorted_scores = sorted(player_scores.items(), key=lambda item: item[0], reverse=True)
        
        for pot in pots:
            pot_size = pot['size']
            eligible_ids = pot['eligible']
            
            for score, players_with_score in sorted_scores:
                if pot_size <= 0: break
                
                pot_winners = [p for p, hand in players_with_score if p.user_id in eligible_ids]

                if pot_winners:
                    best_hand = next((hand for p, hand in players_with_score if p.user_id in eligible_ids), None)
                    win_amount_per_player = pot_size // len(pot_winners)
                    
                    for winner in pot_winners:
                        winner.wallet.inc(win_amount_per_player)
                        
                        if winner.user_id in winners_summary:
                            winners_summary[winner.user_id]['money'] += win_amount_per_player
                        else:
                            winners_summary[winner.user_id] = {
                                'player': winner,
                                'hand': best_hand,
                                'money': win_amount_per_player
                            }
                    pot_size = 0 # Pot distributed
        
        return [(d['player'], d['hand'], d['money']) for d in winners_summary.values()]

    def to_pot(self, game: Game) -> None:
        for p in game.players:
            game.pot += p.round_rate
            p.round_rate = 0
        game.max_round_rate = 0
        # Reset has_acted for all active players for the next round
        for p in game.players_by(states=(PlayerState.ACTIVE,)):
            p.has_acted = False
