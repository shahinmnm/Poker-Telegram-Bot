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


class PokerBotModel:
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
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
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

        members_count = self._bot.get_chat_member_count(chat_id)
        players_active = len(game.players)
        if players_active >= self._min_players and (players_active == members_count - 1 or self._cfg.DEBUG):
            self._start_game(context=context, game=game, chat_id=chat_id)

    def stop(self, user_id: UserId) -> None:
        UserPrivateChatModel(user_id=user_id, kv=self._kv).delete()

    def start(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        user_id = update.effective_message.from_user.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            msg_id = self._view.send_message_return_id(chat_id=chat_id, text="🎮 بازی الان داره اجرا میشه")
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
            return

        members_count = self._bot.get_chat_member_count(chat_id) - 1
        if members_count == 1 and not self._cfg.DEBUG:
            with open(DESCRIPTION_FILE, 'r') as f:
                text = f.read()
            self._view.send_message(chat_id=chat_id, text=text)
            self._view.send_photo(chat_id=chat_id)
            if update.effective_chat.type == 'private':
                UserPrivateChatModel(user_id=user_id, kv=self._kv).set_chat_id(chat_id=chat_id)
            return

        players_active = len(game.players)
        if players_active >= self._min_players:
            self._start_game(context=context, game=game, chat_id=chat_id)
        else:
            msg_id = self._view.send_message_return_id(chat_id=chat_id, text="👤 بازیکن کافی نیست")
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
        return

    def _start_game(
        self,
        context: CallbackContext,
        game: Game,
        chat_id: ChatId
    ) -> None:
        print(f"new game: {game.id}, players count: {len(game.players)}")

        self._view.send_message(
            chat_id=chat_id,
            text='🚀 !بازی شروع شد!',
            reply_markup=ReplyKeyboardRemove(),
        )

        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS)
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
        
                # ==================== شروع بلوک جایگزینی ====================
                # در دور pre-flop، اولین حرکت با بازیکنی است که بلافاصله بعد از Big Blind قرار دارد.
                # ایندکس Big Blind معمولا 1 است (0 = Dealer/Small Blind, 1 = Big Blind).
                # پس اولین حرکت با بازیکن ایندکس 2 است.
                # در بازی دو نفره (heads-up)، اولین حرکت با Dealer/Small Blind (ایندکس 0) است.
        
                num_players = len(game.players)
                if num_players == 2:
                    # در بازی دو نفره، نوبت اول با دیلر/اسمال بلایند است (ایندکس 0)
                    # ایندکس را -1 میگذاریم تا _process_playing با افزایش آن، به ایندکس 0 برسد.
                    game.current_player_index = -1
                else:
                    # در بازی با بیش از 2 بازیکن، نوبت با نفر بعد از بیگ بلایند است (ایندکس 2)
                    # ایندکس را 1 میگذاریم تا _process_playing با افزایش آن، به ایندکس 2 برسد.
                    game.current_player_index = 1
                
                # فراخوانی برای شروع روند بازی و تعیین نوبت
                self._process_playing(chat_id=chat_id, game=game)
                # ===================== پایان بلوک جایگزینی =====================
        
                context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]
        
    def _process_playing(self, chat_id: ChatId, game: Game) -> None:
        # اگر بازی تمام شده است، خارج شو
        if game.state == GameState.INITIAL:
            return

        # ... (بلوک if برای بررسی تعداد بازیکنان فعال را دست نخورده باقی بگذارید)
        active_and_all_in_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(active_and_all_in_players) <= 1:
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if active_players and game.all_in_players_are_covered():
                self._fast_forward_to_finish(game, chat_id)
            else:
                self._finish(game, chat_id)
            return

        # ==================== شروع بلوک جایگزینی ====================
        # شرط جدید و دقیق‌تر برای تشخیص پایان دور شرط‌بندی
        # یک دور زمانی تمام می‌شود که:
        # 1. همه بازیکنان فعال (نه Fold و نه All-in) حداقل یک بار عمل کرده باشند.
        # 2. مبلغ شرط همه بازیکنان فعال با بیشترین مبلغ شرط در آن دور (max_round_rate) برابر باشد.
        
        round_over = True
        active_players = game.players_by(states=(PlayerState.ACTIVE,))

        if not active_players: # اگر هیچ بازیکن فعالی نمانده
             round_over = True
        else:
            for p in active_players:
                # اگر بازیکنی پیدا شود که هنوز عمل نکرده یا شرطش با ماکزیمم برابر نیست، دور تمام نشده
                if not p.has_acted or p.round_rate < game.max_round_rate:
                    round_over = False
                    break
        
        # حالت خاص برای Big Blind در Pre-flop
        # اگر کسی Raise نکرده باشد و نوبت به Big Blind برسد، او حق انتخاب (check یا raise) دارد
        # در این حالت has_acted او False است اما دور نباید تمام شود.
        if len(game.players) > 1:
            big_blind_player = game.players[1 % len(game.players)]
            if (game.state == GameState.ROUND_PRE_FLOP and
                    not big_blind_player.has_acted and
                    game.max_round_rate == 2 * SMALL_BLIND):
                round_over = False

        if round_over:
            self._round_rate.to_pot(game)
            self._goto_next_round(game, chat_id)
            if game.state == GameState.INITIAL:  # game has finished
                return

            # ریست کردن وضعیت has_acted برای دور جدید و تعیین نفر شروع کننده
            game.current_player_index = -1
            for p in game.players:
                if p.state == PlayerState.ACTIVE:
                    p.has_acted = False

            # فراخوانی بازگشتی برای شروع دور جدید
            self._process_playing(chat_id, game)
            return

        # Find next active player
        while True:
            game.current_player_index = (game.current_player_index + 1) % len(game.players)
            current_player = self._current_turn_player(game)
            if current_player.state == PlayerState.ACTIVE:
                # اگر بازیکنی که نوبت به او رسیده، قبلا حرکت کرده و شرطش با ماکزیمم برابر است
                # یعنی دور کامل شده و باید از حلقه خارج شویم.
                if current_player.has_acted and current_player.round_rate == game.max_round_rate:
                    # این شرط جلوی حلقه‌های بی‌نهایت را می‌گیرد
                    # و باعث می‌شود منطق به بلوک "رفتن به دور بعد" برسد.
                    self._process_playing(chat_id, game) # یک فراخوانی بازگشتی برای ارزیابی مجدد وضعیت
                    return
                break # بازیکن فعال بعدی پیدا شد، از حلقه خارج شو

        game.last_turn_time = datetime.datetime.now()
        current_player_money = current_player.wallet.value()

        msg_id = self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=current_player,
            money=current_player_money,
        )
        
        # ===> شروع بلوک جدید برای مدیریت msg_id <===
        # بررسی می‌کنیم که آیا msg_id معتبر است یا خیر
        if msg_id:
            game.turn_message_id = msg_id
        else:
            # اگر ارسال پیام نوبت با خطا مواجه شد، بازی را متوقف می‌کنیم تا از کرش جلوگیری شود
            print(f"CRITICAL: Failed to send turn message for chat {chat_id}. Aborting turn processing.")
            # می‌توانید یک پیام خطا برای ادمین یا در گروه ارسال کنید
            self._view.send_message(chat_id, "خطای جدی در ارسال پیام نوبت رخ داد. بازی متوقف شد.")
            # بازی را ریست کنید یا وضعیت را به حالت خطا تغییر دهید
            game.reset()
        # ===> پایان بلوک جدید <===

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
        wallet = WalletManagerModel(
            update.effective_message.from_user.id, self._kv)
        money = wallet.value()

        chat_id = update.effective_message.chat_id
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
            chat_id=update.effective_message.chat_id,
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
            raise ValueError("private chat not found")

        private_chat_id = private_chat_id.decode('utf-8')

        message_id = self._view.send_desk_cards_img(
            chat_id=private_chat_id,
            cards=cards,
            caption="Your cards",
            disable_notification=False,
        ).message_id

        try:
            rm_msg_id = user_chat_model.pop_message()
            while rm_msg_id is not None:
                try:
                    rm_msg_id = rm_msg_id.decode('utf-8')
                    self._view.remove_message(
                        chat_id=private_chat_id,
                        message_id=rm_msg_id,
                    )
                except Exception as ex:
                    print("remove_message", ex)
                    traceback.print_exc()
                rm_msg_id = user_chat_model.pop_message()

            user_chat_model.push_message(message_id=message_id)
        except Exception as ex:
            print("bulk_remove_message", ex)
            traceback.print_exc()

    def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        for player in game.players:
            cards = player.cards = [
                game.remain_cards.pop(), game.remain_cards.pop()
            ]
            try:
                self._send_cards_private(player=player, cards=cards)
                continue
            except Exception as ex:
                print(ex)
                pass
            msg_id = self._view.send_cards(
                chat_id=chat_id,
                cards=cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )
            if msg_id:
                game.message_ids_to_delete.append(msg_id)

    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId) -> None:
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

    def _finish(self, game: Game, chat_id: ChatId) -> None:
        if game.pot == 0:
            self._round_rate.to_pot(game)
        print(f"game finished: {game.id}, players count: {len(game.players)}, pot: {game.pot}")

        active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        player_scores = self._winner_determine.determinate_scores(active_players, game.cards_table)
        winners_hand_money = self._round_rate.finish_rate(game, player_scores)
        
        only_one_player = len(active_players) == 1
        text = "🏁 بازی با این نتیجه تموم شد:\n\n"
        for (player, best_hand, money) in winners_hand_money:
            win_hand = " ".join(best_hand)
            text += f"{player.mention_markdown}:\n🏆 گرفتی: *{money} $*\n"
            if not only_one_player and best_hand:
                text += f"🃏 با ترکیب این کارتا:\n{win_hand}\n\n"
        text += "\n/ready برای ادامه"
        self._view.send_message(chat_id=chat_id, text=text, reply_markup=ReplyKeyboardRemove())
        for player in game.players:
            player.wallet.approve(game.id)
            
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

    def middleware_user_turn(self, fn: Handler) -> Handler:
        def m(update, context):
            game = self._game_from_context(context)
            if game.state == GameState.INITIAL: return
            
            current_player = self._current_turn_player(game)
            if not current_player or update.callback_query.from_user.id != current_player.user_id: return
            
            fn(update, context) 
            
            if game.turn_message_id:
                try:
                    self._view.remove_markup(
                        chat_id=update.effective_message.chat_id,
                        message_id=game.turn_message_id,
                    )
                except Exception as e:
                    print(f"Could not remove markup for message {game.turn_message_id}: {e}")
        return m
    
    def ban_player(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id

        if game.state in (GameState.INITIAL, GameState.FINISHED):
            return

        diff = datetime.datetime.now() - game.last_turn_time
        if diff < MAX_TIME_FOR_TURN:
            self._view.send_message(
                chat_id=chat_id,
                text="⏳ نمی‌تونی محروم کنی. حداکثر زمان نوبت ۲ دقیقه‌س",
            )
            return

        self._view.send_message(
            chat_id=chat_id,
            text="⏰ وقت تموم شد!",
        )
        self.fold(update, context)

    def _action_handler(self, update: Update, context: CallbackContext, action_logic):
        """A generic handler for player actions"""
        game = self._game_from_context(context)
        player = self._current_turn_player(game)

        # ===> اطمینان از اینکه بازیکن درست اقدام می‌کند <===
        if not player or player.user_id != update.effective_user.id:
            return # اگر نوبت این بازیکن نیست، هیچ کاری نکن
            
        try:
            action_logic(game, player)
            player.has_acted = True
        except UserException as e:
            msg_id = self._view.send_message_return_id(chat_id=update.effective_chat.id, text=str(e))
            if msg_id: game.message_ids_to_delete.append(msg_id)
            return
        
        # ===> حذف فراخوانی بازگشتی اضافه <===
        # این فراخوانی باعث اجرای مجدد و نمایش دکمه check بعد از call می‌شد.
        # self._process_playing(chat_id, game) <<<< این خط را حذف یا کامنت کنید

        # به جای آن، اجازه دهید که middleware کار را تمام کند و ما فقط
        # نوبت را پردازش کنیم.
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
            if msg_id: game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)

    def call_check(self, update: Update, context: CallbackContext) -> None:
        def logic(game, player):
            action_str = PlayerAction.CALL.value if player.round_rate < game.max_round_rate else PlayerAction.CHECK.value
            
            amount_to_call = game.max_round_rate - player.round_rate
            if player.wallet.value() < amount_to_call:
                all_in_amount, mention = self._round_rate.all_in(game, player)
                msg_id = self._view.send_message_return_id(
                    chat_id=update.effective_chat.id,
                    text=f"{mention} {PlayerAction.ALL_IN.value} {all_in_amount}$ (به دلیل عدم توانایی در کال)"
                )
            else:
                self._round_rate.call_check(game, player)
                msg_id = self._view.send_message_return_id(
                    chat_id=update.effective_chat.id,
                    text=f"{player.mention_markdown} {action_str}"
                )
            
            if msg_id: game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)

    def raise_rate_bet(self, update: Update, context: CallbackContext, raise_bet_amount: Money) -> None:
        def logic(game, player):
            action = PlayerAction.RAISE_RATE if game.max_round_rate > 0 else PlayerAction.BET
            amount, mention = self._round_rate.raise_bet(game, player, raise_bet_amount)
            msg_id = self._view.send_message_return_id(
                chat_id=update.effective_chat.id,
                text=f"{mention} {action.value} {amount}$"
            )
            if msg_id: game.message_ids_to_delete.append(msg_id)
        self._action_handler(update, context, logic)

    def all_in(self, update: Update, context: CallbackContext) -> None:
        def logic(game, player):
            amount, mention = self._round_rate.all_in(game, player)
            msg_id = self._view.send_message_return_id(
                chat_id=update.effective_chat.id,
                text=f"{mention} {PlayerAction.ALL_IN.value} {amount}$"
            )
            if msg_id: game.message_ids_to_delete.append(msg_id)
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


    def to_pot(self, game) -> None:
        for p in game.players:
            game.pot += p.round_rate
            p.round_rate = 0
        game.max_round_rate = 0
        if game.players:
            game.trading_end_user_id = game.players[0].user_id
