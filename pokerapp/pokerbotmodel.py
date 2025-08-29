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
)
from pokerapp.pokerbotview import PokerBotViewer

# ... (سایر ثابت‌ها مثل DICE_MULT و ... بدون تغییر باقی می‌مانند) ...
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

    # ... (متدهای _min_players, _game_from_context, _current_turn_player بدون تغییر) ...
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
        i = game.current_player_index % len(game.players)
        return game.players[i]

    # ... (متد ready بدون تغییر) ...
    def ready(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id

        if game.state != GameState.INITIAL:
            msg_id = self._view.send_message_return_id(
                chat_id=chat_id,
                text="⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!"
            )
            # این پیام موقتی است و باید حذف شود
            # در عمل، بهتر است این پیام‌ها را به لیست اضافه نکنیم یا راهی برای حذفشان پیدا کنیم
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

        if player.wallet.value() < 2*SMALL_BLIND:
            return self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="💸 پولت کمه",
            )
        
        game.ready_users.add(user.id)
        game.players.append(player)
        
        # ... (بقیه متد ready بدون تغییر) ...
        members_count = self._bot.get_chat_member_count(chat_id)
        players_active = len(game.players)
        # One is the bot.
        if players_active == members_count - 1 and players_active >= self._min_players:
            self._start_game(context=context, game=game, chat_id=chat_id)

    def stop(self, user_id: UserId) -> None:
        UserPrivateChatModel(user_id=user_id, kv=self._kv).delete()

    def start(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        user_id = update.effective_message.from_user.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            msg_id = self._view.send_message_return_id(chat_id=chat_id, text="🎮 بازی الان داره اجرا میشه")
            if msg_id: # <<<< شرط اضافه شود
                game.message_ids_to_delete.append(msg_id)
            return

        members_count = self._bot.get_chat_member_count(chat_id) - 1
        if members_count == 1:
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
            if msg_id: # <<<< شرط اضافه شود
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
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[["poker"]],
                resize_keyboard=True,
            ),
        )

        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS)
        
        # فقط در صورتی که بازی قبلی وجود داشته باشد، ترتیب بازیکنان را بچرخان
        if old_players_ids:
            # دیلر را به نفر بعدی منتقل کن
            old_players_ids = old_players_ids[1:] + old_players_ids[:1]

            def index(ln: List, obj) -> int:
                try:
                    return ln.index(obj)
                except ValueError:
                    return -1 # بازیکنان جدید در انتهای لیست قرار می‌گیرند

            game.players.sort(key=lambda p: index(old_players_ids, p.user_id))

        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game=game, chat_id=chat_id)

        # 1. بلایندها را پرداخت کن (این متد trading_end_user_id را هم تنظیم می‌کند)
        self._round_rate.round_pre_flop_rate_before_first_turn(game)
        
        # 2. نفر اول برای بازی را مشخص کن
        num_players = len(game.players)
        if num_players == 2:
            # در بازی دو نفره (Heads-Up)، دیلر/اسمال بلایند (اندیس 0) اول حرکت می‌کند
            game.current_player_index = 0
        else:
            # در بازی با 3+ بازیکن، نفر بعد از بیگ بلایند (Under the Gun) اول حرکت می‌کند
            # SB اندیس 0، BB اندیس 1، پس UTG اندیس 2 است.
            game.current_player_index = 2

        # 3. به صورت دستی نوبت را برای بازیکن اول ارسال کن
        # به جای فراخوانی _process_playing که همه چیز را به هم می‌ریخت
        current_player = self._current_turn_player(game)
        
        # اطمینان حاصل کن که بازیکن فعال است
        if current_player.state != PlayerState.ACTIVE:
            # اگر به هر دلیلی بازیکن اول فعال نبود، حلقه را برای پیدا کردن نفر بعدی اجرا کن
            return self._process_playing(chat_id=chat_id, game=game)

        # زمان نوبت را ثبت کن
        game.last_turn_time = datetime.datetime.now()

        # دکمه‌های نوبت را برای بازیکن صحیح نمایش بده
        self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=current_player,
            money=current_player.wallet.value(),
        )

        context.chat_data[KEY_OLD_PLAYERS] = list(
            map(lambda p: p.user_id, game.players),
        )
    # ... (متد bonus بدون تغییر) ...
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
        current_player = None # <<<< این را به None مقداردهی اولیه کنید

        for player in game.players:
            if player.user_id == update.effective_user.id:
                current_player = player
                break

        if current_player is None or not current_player.cards:
            return

        # <<<< شروع تغییر >>>>
        # دیگر نیازی به دریافت message_id نیست
        self._view.send_cards(
            chat_id=update.effective_message.chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=update.effective_message.message_id,
        )

    # ... (متدهای _check_access, _send_cards_private, _divide_cards بدون تغییر عمده) ...
    def _check_access(self, chat_id: ChatId, user_id: UserId) -> bool:
        chat_admins = self._bot.get_chat_administrators(chat_id)
        for m in chat_admins:
            if m.user.id == user_id:
                return True
        return False

    def _send_cards_private(self, player: Player, cards: Cards) -> None:
        user_chat_model = UserPrivateChatModel(
            user_id=player.user_id, kv=self._kv
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
        )
        try:
            rm_msg_id = user_chat_model.pop_message()
            while rm_msg_id is not None:
                try:
                    rm_msg_id = rm_msg_id.decode('utf-8')
                    self._view.remove_message(
                        chat_id=private_chat_id, message_id=rm_msg_id
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
            game.message_ids_to_delete.append(msg_id)
            
    def _process_playing(self, chat_id: ChatId, game: Game) -> None:
        # <<<< شروع بلوک جدید >>>>
        # حذف پیام‌های اعلام وضعیت از دور قبلی
        for msg_id in game.message_ids_to_delete:
            try:
                self._view.remove_message(chat_id=chat_id, message_id=msg_id)
            except Exception as e:
                print(f"Could not delete status message {msg_id}: {e}")
        game.message_ids_to_delete.clear()  # لیست را برای دور بعد خالی می‌کنیم
        # <<<< پایان بلوک جدید >>>>

        game.current_player_index += 1
        game.current_player_index %= len(game.players)

        current_player = self._current_turn_player(game)
        if current_player.user_id == game.trading_end_user_id:
            self._round_rate.to_pot(game)
            self._goto_next_round(game, chat_id)
            game.current_player_index = 0

        if game.state == GameState.INITIAL: return
        current_player = self._current_turn_player(game)
        current_player_money = current_player.wallet.value()
        if current_player_money <= 0:
            current_player.state = PlayerState.ALL_IN

        if current_player.state != PlayerState.ACTIVE:
            self._process_playing(chat_id, game)
            return

        all_in_active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(all_in_active_players) == 1:
            self._finish(game, chat_id)
            return

        game.last_turn_time = datetime.datetime.now()
        msg_id = self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=current_player,
            money=current_player_money,
        )
        if msg_id:
            game.turn_message_id = msg_id # از این ID برای حذف markup استفاده می‌شود
        # <<<< پایان تغییر >>>>

    def add_cards_to_table(
        self,
        count: int,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        for _ in range(count):
            game.cards_table.append(game.remain_cards.pop())

        # <<<< شروع تغییر >>>>
        # چون به ID این پیام برای حذف نیاز داریم، آن را بررسی و اضافه می‌کنیم
        msg_id = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"💰 پات فعلی: {game.pot}$",
        )
        if msg_id:
            game.message_ids_to_delete.append(msg_id)
        # <<<< پایان تغییر >>>>
    def _finish(self, game: Game, chat_id: ChatId) -> None:
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
            if not only_one_player:
                text += f"🃏 با ترکیب این کارتا:\n{win_hand}\n\n"
        text += "\n/ready برای ادامه"
        self._view.send_message(chat_id=chat_id, text=text, reply_markup=ReplyKeyboardRemove())
        for player in game.players:
            player.wallet.approve(game.id)
            
        # <<<< شروع تغییر: پاکسازی پیام ها بعد از اعلام نتیجه >>>>
        # پیام نتیجه باقی می ماند، بقیه حذف می شوند
        self._view.remove_game_messages(chat_id, game.message_ids_to_delete)
        game.message_ids_to_delete.clear()
        # <<<< پایان تغییر >>>>
        game.reset()

    # ... (متد _goto_next_round بدون تغییر) ...
    def _goto_next_round(self, game: Game, chat_id: ChatId) -> bool:
        active_players = game.players_by(
            states=(PlayerState.ACTIVE,)
        )
        if len(active_players) == 1:
            active_players[0].state = PlayerState.ALL_IN
            if len(game.cards_table) == 5:
                self._finish(game, chat_id)
                return

        def add_cards(cards_count):
            return self.add_cards_to_table(
                count=cards_count,
                game=game,
                chat_id=chat_id
            )

        state_transitions = {
            GameState.ROUND_PRE_FLOP: {"next_state": GameState.ROUND_FLOP, "processor": lambda: add_cards(3)},
            GameState.ROUND_TURN: {"next_state": GameState.ROUND_RIVER, "processor": lambda: add_cards(1)},
            GameState.ROUND_FLOP: {"next_state": GameState.ROUND_TURN, "processor": lambda: add_cards(1)},
            GameState.ROUND_RIVER: {"next_state": GameState.FINISHED, "processor": lambda: self._finish(game, chat_id)}
        }

        if game.state not in state_transitions:
            raise Exception("unexpected state: " + game.state.value)
        transation = state_transitions[game.state]
        game.state = transation["next_state"]
        transation["processor"]()
        
    def middleware_user_turn(self, fn: Handler) -> Handler:
        def m(update, context):
            game = self._game_from_context(context)
            if game.state == GameState.INITIAL: return
            current_player = self._current_turn_player(game)
            current_user_id = update.callback_query.from_user.id
            if current_user_id != current_player.user_id: return
            fn(update, context)
            
            if game.turn_message_id:
                try:
                    self._view.remove_markup(
                        chat_id=update.effective_message.chat_id,
                        message_id=game.turn_message_id,
                    )
                except Exception as e:
                    print(f"Could not remove markup for message {game.turn_message_id}: {e}")
            # <<<< پایان تغییر >>>>
        return m

    def ban_player(self, update: Update, context: CallbackContext) -> None:
        # ... (بدون تغییر) ...
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        if game.state in (GameState.INITIAL, GameState.FINISHED): return
        diff = datetime.datetime.now() - game.last_turn_time
        if diff < MAX_TIME_FOR_TURN:
            msg_id = self._view.send_message_return_id(
                chat_id=chat_id, text="⏳ نمی‌تونی محروم کنی. حداکثر زمان نوبت ۲ دقیقه‌س"
            )
            game.message_ids_to_delete.append(msg_id)
            return
        msg_id = self._view.send_message_return_id(chat_id=chat_id, text="⏰ وقت تموم شد!")
        game.message_ids_to_delete.append(msg_id)
        self.fold(update, context)

    def fold(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        player = self._current_turn_player(game)

        player.state = PlayerState.FOLD

        # <<<< شروع بلوک اصلاح شده >>>>
        msg_id = self._view.send_message(
            chat_id=update.effective_message.chat_id,
            text=f"{player.mention_markdown} {PlayerAction.FOLD.value}"
        )
        if msg_id:
            game.message_ids_to_delete.append(msg_id)
        # <<<< پایان بلوک اصلاح شده >>>>

        self._process_playing(
            chat_id=update.effective_message.chat_id,
            game=game,
        )
    def call_check(
        self,
        update: Update,
        context: CallbackContext,
    ) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        player = self._current_turn_player(game)

        action = PlayerAction.CALL.value
        if player.round_rate == game.max_round_rate:
            action = PlayerAction.CHECK.value

        try:
            amount = game.max_round_rate - player.round_rate
            if player.wallet.value() <= amount:
                return self.all_in(update=update, context=context)

            mention_markdown = self._current_turn_player(game).mention_markdown

            # <<<< شروع بلوک اصلاح شده >>>>
            msg_id = self._view.send_message(
                chat_id=chat_id,
                text=f"{mention_markdown} {action}"
            )
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
            # <<<< پایان بلوک اصلاح شده >>>>

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
        chat_id = update.effective_message.chat_id
        player = self._current_turn_player(game)

        try:
            action = PlayerAction.RAISE_RATE
            if player.round_rate == game.max_round_rate:
                action = PlayerAction.BET

            # <<<< این بلوک را اصلاح کنید >>>>
            amount, mention_markdown = self._round_rate.raise_bet(
                game,
                player,
                raise_bet_rate.value,
            )

            msg_id = self._view.send_message(
                chat_id=chat_id,
                text=f"{mention_markdown} {action.value} {amount}$"
            )
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
            # <<<< پایان اصلاح >>>>

        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=str(e))
            return

        self._process_playing(
            chat_id=chat_id,
            game=game,
        )

    def all_in(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        player = self._current_turn_player(game)

        try:
            amount, mention_markdown = self._round_rate.all_in(game, player)

            # <<<< این بلوک را اصلاح کنید >>>>
            msg_id = self._view.send_message(
                chat_id=chat_id,
                text=f"{mention_markdown} {PlayerAction.ALL_IN.value} {amount}$"
            )
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
            # <<<< پایان اصلاح >>>>

        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=str(e))
            return

        self._process_playing(
            chat_id=chat_id,
            game=game,
        )

    # <<<< شروع متدهای جدید: Show/Hide Cards >>>>
    def hide_cards(self, update: Update, context: CallbackContext) -> None:
        user = update.effective_user
        chat_id = update.effective_chat.id
        game = self._game_from_context(context)
        
        self._view.show_reopen_keyboard(
            chat_id=chat_id,
            player_mention=user.mention_markdown()
        )
        # پیام قبلی نمایش کارت ها را حذف می کنیم
        msg_id_to_remove = update.message.reply_to_message.message_id
        if msg_id_to_remove in game.message_ids_to_delete:
            game.message_ids_to_delete.remove(msg_id_to_remove)
            self._view.remove_message(chat_id, msg_id_to_remove)


    def show_table(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if not game.cards_table:
            msg_id = self._view.send_message_return_id(
                chat_id=chat_id,
                text="هنوز کارتی روی میز قرار نگرفته است."
            )
            game.message_ids_to_delete.append(msg_id)
            return

        msg_id = self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"میز بازی\nظرف فعلی (Pot): {game.pot}$",
        )
        game.message_ids_to_delete.append(msg_id)
    # <<<< پایان متدهای جدید >>>>

# ... (کلاس WalletManagerModel و RoundRateModel بدون تغییر باقی می‌مانند) ...
class WalletManagerModel(Wallet):
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv: redis.Redis = kv

    def value(self) -> Money:
        money = self._kv.get(f"money:{self._user_id}")
        if money is None:
            self.set(DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(money)

    def set(self, amount: Money) -> None:
        self._kv.set(f"money:{self._user_id}", amount)

    def inc(self, amount: Money) -> Money:
        return self._kv.incrby(f"money:{self._user_id}", amount)

    def add_daily(self, amount: Money) -> Money:
        now_time = int(datetime.datetime.now().timestamp())
        self._kv.set(f"last_time:{self._user_id}", now_time)
        return self.inc(amount)

    def has_daily_bonus(self) -> bool:
        last_time = self._kv.get(f"last_time:{self._user_id}")
        if last_time is None:
            return False
        last_time = int(last_time)
        now_time = int(datetime.datetime.now().timestamp())
        return now_time - last_time < ONE_DAY

    def authorized_money(self, game_id: str) -> Money:
        return int(self._kv.get(f"auth:{game_id}:{self._user_id}"))

    def authorize(self, game_id: str, amount: Money) -> None:
        self._kv.set(f"auth:{game_id}:{self._user_id}", amount)

    def approve(self, game_id) -> None:
        self._kv.delete(f"auth:{game_id}:{self._user_id}")

class RoundRateModel:
    def round_pre_flop_rate_before_first_turn(self, game: Game):
        players = game.players
        sb_player = players[0]
        sb_player.wallet.authorize(game.id, sb_player.wallet.value())
        bb_player = players[1]
        bb_player.wallet.authorize(game.id, bb_player.wallet.value())

        sb_player.round_rate = SMALL_BLIND
        sb_player.wallet.inc(-SMALL_BLIND)
        bb_player.round_rate = 2*SMALL_BLIND
        bb_player.wallet.inc(-2*SMALL_BLIND)
        game.max_round_rate = 2*SMALL_BLIND
        game.trading_end_user_id = bb_player.user_id

    def round_pre_flop_rate_after_first_turn(self, game: Game):
        for p in game.players[2:]:
            p.wallet.authorize(game_id=game.id, amount=p.wallet.value())

    def call_check(self, game: Game, player: Player):
        amount = game.max_round_rate - player.round_rate
        if player.wallet.value() < amount:
            raise UserException("Not enough money")
        player.round_rate += amount
        player.wallet.inc(-amount)

    def raise_bet(self, game: Game, player: Player, raise_bet_rate: PlayerAction):
        amount = raise_bet_rate.value
        if player.wallet.value() < amount:
            raise UserException("Not enough money")
        player.round_rate += amount
        player.wallet.inc(-amount)
        game.max_round_rate = player.round_rate
        game.trading_end_user_id = player.user_id
        return amount

    def all_in(self, game: Game, player: Player):
        amount = player.wallet.value()
        player.round_rate += amount
        player.wallet.set(0)
        player.state = PlayerState.ALL_IN
        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate
            game.trading_end_user_id = player.user_id

    def _sum_authorized_money(self, game: Game, players: List[Tuple[Player, Cards]]) -> int:
        sum_authorized_money = 0
        for player in players:
            sum_authorized_money += player[0].wallet.authorized_money(game_id=game.id)
        return sum_authorized_money

    def finish_rate(
        self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]
    ) -> List[Tuple[Player, Cards, Money]]:
        sorted_player_scores_items = sorted(
            player_scores.items(), reverse=True, key=lambda x: x[0]
        )
        player_scores_values = list(map(lambda x: x[1], sorted_player_scores_items))
        res = []
        for win_players in player_scores_values:
            players_authorized = self._sum_authorized_money(game=game, players=win_players)
            if players_authorized <= 0: continue
            game_pot = game.pot
            for win_player, best_hand in win_players:
                if game.pot <= 0: break
                authorized = win_player.wallet.authorized_money(game_id=game.id)
                win_money_real = game_pot * (authorized / players_authorized)
                win_money_real = round(win_money_real)
                win_money_can_get = authorized * len(game.players)
                win_money = min(win_money_real, win_money_can_get)
                win_player.wallet.inc(win_money)
                game.pot -= win_money
                res.append((win_player, best_hand, win_money))
        return res

    def to_pot(self, game) -> None:
        for p in game.players:
            game.pot += p.round_rate
            p.round_rate = 0
        game.max_round_rate = 0
        game.trading_end_user_id = game.players[0].user_id
