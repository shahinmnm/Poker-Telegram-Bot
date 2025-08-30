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

    ACTIVE_GAME_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }

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
        message = self._view.send_desk_cards_img(
            chat_id=private_chat_id,
            cards=cards,
            caption="کارت‌های شما",
            disable_notification=True,
        )
        if message:
            user_chat_model.push_message(message_id=message.message_id)


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

        # Condition 1: Only one or zero non-folded players left
        active_and_all_in_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(active_and_all_in_players) <= 1:
            self._finish(game, chat_id)
            return

        # Condition 2: Betting round is over
        round_over = False
        active_players = game.players_by(states=(PlayerState.ACTIVE,))

        # If there are active players, check if betting is complete
        if active_players:
            # All active players must have acted AND have the same amount bet in this round
            all_acted = all(p.has_acted for p in active_players)
            all_matched = len(set(p.round_rate for p in active_players)) == 1

            if all_acted and all_matched:
                # Special check for pre-flop big blind option
                is_bb_option = False
                if len(game.players) > 1 and game.state == GameState.ROUND_PRE_FLOP:
                    bb_player_index = 1 % len(game.players) # Assuming player 0 is dealer
                    bb_player = game.players[bb_player_index]
                    if (not bb_player.has_acted and 
                        bb_player.state == PlayerState.ACTIVE and
                        game.max_round_rate == (2 * SMALL_BLIND)):
                        is_bb_option = True

                if not is_bb_option:
                    round_over = True
        else:
            # If no active players are left (all are ALL_IN or FOLD), the betting round is over.
            round_over = True

        if round_over:
            self._round_rate.to_pot(game)
            # Check for fast-forward scenario
            active_players_after_pot = game.players_by(states=(PlayerState.ACTIVE,))
            if len(active_players_after_pot) < 2:
                 self._fast_forward_to_finish(game, chat_id)
            else:
                 self._goto_next_round(game, chat_id)
                 if game.state in self.ACTIVE_GAME_STATES:
                     self._process_playing(chat_id, game)
            return

        # Find the next player whose turn it is
        start_index = game.current_player_index
        num_players = len(game.players)
        while True:
            game.current_player_index = (game.current_player_index + 1) % num_players
            current_player = self._current_turn_player(game)
            if current_player.state == PlayerState.ACTIVE:
                break
            if game.current_player_index == start_index:
                print("Error: Full circle without finding an active player in _process_playing.")
                self._finish(game, chat_id)
                return

        # It's this player's turn.
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

        message = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"💰 پات فعلی: {game.pot}$",
        )
        if message:
            game.message_ids_to_delete.append(message.message_id)

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
            text = "بازی بدون برنده تمام شد."
        elif len(active_players) == 1:
            winner = active_players[0]
            winner.wallet.inc(game.pot)
            text = f"🏁 بازی تمام شد!\n\n{winner.mention_markdown} با فولد بقیه، برنده *{game.pot}$* شد!\n\n"
        else:
            # Showdown: ensure all community cards are dealt
            while len(game.cards_table) < 5 and game.remain_cards:
                game.cards_table.append(game.remain_cards.pop())
            
            if game.state != GameState.ROUND_RIVER and game.state != GameState.FINISHED:
                 message = self._view.send_desk_cards_img(
                    chat_id=chat_id,
                    cards=game.cards_table,
                    caption=f"میز نهایی - پات: {game.pot}$",
                )
                 if message:
                    game.message_ids_to_delete.append(message.message_id)

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

    def _goto_next_round(self, game: Game, chat_id: ChatId) -> None:
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

        if game.state in self.ACTIVE_GAME_STATES:
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                p.has_acted = False
            
            first_active_player_index = -1
            num_players = len(game.players)
            # Start searching from the player after the dealer (index 0)
            for i in range(1, num_players + 1):
                player_index = i % num_players
                player = game.players[player_index]
                if player.state == PlayerState.ACTIVE:
                    first_active_player_index = player_index
                    break
            
            if first_active_player_index != -1:
                game.current_player_index = first_active_player_index - 1
            else:
                print("Error: No active players found to start the new round.")
                self._fast_forward_to_finish(game, chat_id)

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

            if game.turn_message_id:
                self._view.remove_markup(
                    chat_id=chat_id,
                    message_id=game.turn_message_id,
                )
                game.turn_message_id = None

            query.answer() 
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

        amount = self._round_rate.all_in(game, player)

        mention_markdown = player.mention_markdown
        self._view.send_message(
            chat_id=chat_id,
            text=f"{mention_markdown} {PlayerAction.ALL_IN.value} ({amount}$)"
        )
        self._process_playing(
            chat_id=chat_id,
            game=game,
        )

    def money(self, update: Update, context: CallbackContext):
        wallet = WalletManagerModel(
            update.effective_message.from_user.id, self._kv)
        money = wallet.value()
        self._view.send_message_reply(
            chat_id=update.effective_message.chat_id,
            message_id=update.effective_message.message_id,
            text=f"💰 موجودی فعلی شما: *{money}$*",
        )

class RoundRateModel:
    def to_pot(self, game: Game) -> None:
        for p in game.players:
            game.pot += p.round_rate
            p.round_rate = 0
        game.max_round_rate = 0

    def call_check(self, game: Game, player: Player) -> None:
        player.wallet.dec(game.max_round_rate - player.round_rate)
        player.round_rate = game.max_round_rate
        player.has_acted = True

    def all_in(self, game: Game, player: Player) -> Money:
        amount = player.wallet.value()
        player.state = PlayerState.ALL_IN
        player.round_rate += amount
        player.wallet.dec(amount)
        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate
        player.has_acted = True
        return amount

    def raise_rate_bet(
        self,
        game: Game, player: Player, raise_bet_rate: PlayerAction
    ) -> Money:
        min_raise = game.max_round_rate + (game.max_round_rate - game.last_raise) if game.last_raise > 0 else 2 * SMALL_BLIND
        
        amount = 0
        if raise_bet_rate == PlayerAction.SMALL.value:
            amount = min_raise
        elif raise_bet_rate == PlayerAction.NORMAL.value:
            amount = round(game.pot * 0.75) if game.pot > 0 else min_raise
        elif raise_bet_rate == PlayerAction.BIG.value:
            amount = game.pot if game.pot > 0 else min_raise
        
        # Ensure raise is at least the minimum allowed
        amount = max(amount, min_raise)
        
        # Player cannot raise more than they have
        if amount > player.wallet.value() + player.round_rate:
            amount = player.wallet.value() + player.round_rate

        # The actual money to put in is the difference
        money_to_add = amount - player.round_rate
        
        if money_to_add >= player.wallet.value():
             # This becomes an all-in
            self.all_in(game, player)
            return player.round_rate

        player.wallet.dec(money_to_add)
        game.last_raise = amount - game.max_round_rate
        game.max_round_rate = amount
        player.round_rate = amount
        player.has_acted = True
        
        # After a raise, all other active players need to act again
        for p in game.players:
            if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                p.has_acted = False

        return amount

    def round_pre_flop_rate_before_first_turn(self, game: Game) -> None:
        num_players = len(game.players)
        sb_player = game.players[0 % num_players]
        bb_player = game.players[1 % num_players]

        # Small Blind
        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.wallet.dec(sb_amount)
        sb_player.round_rate = sb_amount
        if sb_amount >= sb_player.wallet.value(): sb_player.state = PlayerState.ALL_IN

        # Big Blind
        bb_amount = min(2 * SMALL_BLIND, bb_player.wallet.value())
        bb_player.wallet.dec(bb_amount)
        bb_player.round_rate = bb_amount
        if bb_amount >= bb_player.wallet.value(): bb_player.state = PlayerState.ALL_IN
        
        game.max_round_rate = 2 * SMALL_BLIND
        game.last_raise = SMALL_BLIND # The difference between BB and SB

    def finish_rate(
        self,
        game: Game,
        player_scores: Dict[Score, List[Tuple[Player, Cards]]],
    ) -> List[Tuple[Player, Cards, Money]]:
        
        all_players_in_hand = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        
        total_bets = {}
        for p in all_players_in_hand:
            total_bets[p.user_id] = p.total_bet + p.round_rate

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
                    if player not in eligible_players:
                        eligible_players.append(player)

            if pot_amount > 0:
                pots.append({"amount": pot_amount, "eligible_players": eligible_players})

            last_bet_level = bet_level

        final_winnings = {}
        for pot in pots:
            eligible_winners = []
            best_score_in_pot = -1

            sorted_scores = sorted(player_scores.keys(), reverse=True)

            for score in sorted_scores:
                for player, hand in player_scores[score]:
                    if player in pot["eligible_players"]:
                        if best_score_in_pot == -1:
                            best_score_in_pot = score
                        if score == best_score_in_pot:
                             eligible_winners.append((player, hand))
                if best_score_in_pot != -1: # Found winners for this pot
                    break

            if not eligible_winners: continue

            win_share = pot["amount"] // len(eligible_winners)
            for winner, hand in eligible_winners:
                winner.wallet.inc(win_share)

                if winner.user_id not in final_winnings:
                    final_winnings[winner.user_id] = {"player": winner, "hand": hand, "money": 0}
                final_winnings[winner.user_id]["money"] += win_share
        
        # Distribute any remaining chips from rounding
        total_distributed = sum(v['money'] for v in final_winnings.values())
        remainder = game.pot - total_distributed
        if remainder > 0 and final_winnings:
             # Give remainder to the first winner in the list, simple but effective
            first_winner_id = list(final_winnings.keys())[0]
            final_winnings[first_winner_id]['player'].wallet.inc(remainder)
            final_winnings[first_winner_id]['money'] += remainder

        return [(v["player"], v["hand"], v["money"]) for v in final_winnings.values()]


class WalletManagerModel(Wallet):
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv: redis.Redis = kv
        self._val_key = f"u_m:{user_id}"
        self._daily_bonus_key = f"u_db:{user_id}"
        self._trans_key = f"u_t:{user_id}"
        self._trans_list_key = f"u_tl:{user_id}"

    def value(self) -> Money:
        val = self._kv.get(self._val_key)
        if val is None:
            self._kv.set(self._val_key, DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(val)

    def inc(self, amount: Money) -> Money:
        return self._kv.incrby(self._val_key, amount)

    def dec(self, amount: Money):
        v = self.value()
        if v < amount:
            raise UserException("موجودی شما کافی نیست.")
        return self._kv.decrby(self._val_key, amount)
    
    def has_daily_bonus(self) -> bool:
        return self._kv.exists(self._daily_bonus_key) > 0

    def add_daily(self, amount: Money) -> Money:
        if self.has_daily_bonus():
            raise UserException("شما قبلاً پاداش روزانه خود را دریافت کرده‌اید.")

        ttl = (datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) +
               datetime.timedelta(days=1) -
               datetime.datetime.now()).seconds

        self._kv.setex(self._daily_bonus_key, ttl, 1)

        return self.inc(amount)
    
    def hold(self, game_id: str, amount: Money):
        self.dec(amount)
        self._kv.hset(self._trans_key, game_id, amount)
        self._kv.lpush(self._trans_list_key, game_id)

    def approve(self, game_id: str):
        self._kv.hdel(self._trans_key, game_id)
        self._kv.lrem(self._trans_list_key, 0, game_id)
