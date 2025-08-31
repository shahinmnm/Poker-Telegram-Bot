# pokerbotmodel.py

#!/usr/bin/env python3

import datetime
import traceback
from threading import Timer
from typing import List, Tuple, Dict

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
        if not game.players: return None
        i = game.current_player_index % len(game.players)
        return game.players[i]

    def ready(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id

        if game.state != GameState.INITIAL:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="⚠️ بازی قبلاً شروع شده! لطفاً تا پایان این دست صبر کنید."
            )
            return

        if len(game.players) >= MAX_PLAYERS:
            self._view.send_message_reply(
                chat_id=chat_id,
                text="🚪 متاسفانه میز پر است!",
                message_id=update.effective_message.message_id,
            )
            return

        user = update.effective_message.from_user

        if user.id in game.ready_users:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="✅ شما از قبل در لیست انتظار هستید.",
            )
            return

        player = Player(
            user_id=user.id,
            mention_markdown=user.mention_markdown(),
            wallet=WalletManagerModel(user.id, self._kv),
            ready_message_id=update.effective_message.message_id,
        )

        if player.wallet.value() < 2 * SMALL_BLIND:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text=f"💸 موجودی شما برای ورود به بازی کافی نیست. حداقل موجودی: *{2*SMALL_BLIND}$*",
            )
            return

        game.ready_users.add(user.id)
        game.players.append(player)
        self._view.send_message_reply(
            chat_id=chat_id,
            message_id=update.effective_message.message_id,
            text=f"👍 {user.mention_markdown()} به بازی اضافه شد. تعداد بازیکنان آماده: {len(game.players)} نفر.",
        )

        members_count = self._bot.get_chat_member_count(chat_id)
        players_active = len(game.players)
        if players_active == members_count - 1 and players_active >= self._min_players:
            self._start_game(context=context, game=game, chat_id=chat_id)

    def stop(self, user_id: UserId) -> None:
        UserPrivateChatModel(user_id=user_id, kv=self._kv).delete()

    def start(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        user_id = update.effective_message.from_user.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            self._view.send_message(
                chat_id=chat_id,
                text="🎮 یک بازی در حال حاضر در جریانه!"
            )
            return

        members_count = self._bot.get_chat_member_count(chat_id) - 1
        if members_count == 1:
            try:
                with open(DESCRIPTION_FILE, 'r', encoding='utf-8') as f:
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
            self._view.send_message(
                chat_id=chat_id,
                text=f"👤 تعداد بازیکنان برای شروع کافی نیست. (حداقل {self._min_players} نفر)"
            )
        return

    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        print(f"New game starting: {game.id}, Players: {len(game.players)}")
        self._view.send_message(
            chat_id=chat_id,
            text='🚀 بازی شروع شد! موفق باشید!',
        )
        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
        old_players_ids = old_players_ids[-1:] + old_players_ids[:-1]

        def index(ln: List, obj) -> int:
            try: return ln.index(obj)
            except ValueError: return -1
        game.players.sort(key=lambda p: index(old_players_ids, p.user_id))

        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game=game, chat_id=chat_id)
        game.current_player_index = 1
        self._round_rate.round_pre_flop_rate_before_first_turn(game)
        self._process_playing(chat_id=chat_id, game=game)
        self._round_rate.round_pre_flop_rate_after_first_turn(game)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]

    def bonus(self, update: Update, context: CallbackContext) -> None:
        wallet = WalletManagerModel(update.effective_message.from_user.id, self._kv)
        money = wallet.value()
        chat_id = update.effective_message.chat_id
        message_id = update.effective_message.message_id

        if wallet.has_daily_bonus():
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=message_id,
                text=f"💰 موجودی فعلی شما: *{money}$*\nشما امروز جایزه روزانه خود را دریافت کرده‌اید.",
            )
            return

        SATURDAY = 5
        if datetime.datetime.today().weekday() == SATURDAY:
            dice_msg = self._view.send_dice_reply(chat_id=chat_id, message_id=message_id, emoji='🎰')
            icon = '🎰'
            bonus = dice_msg.dice.value * 20
        else:
            dice_msg = self._view.send_dice_reply(chat_id=chat_id, message_id=message_id)
            icon = DICES[dice_msg.dice.value - 1]
            bonus = BONUSES[dice_msg.dice.value - 1]

        new_message_id = dice_msg.message_id
        new_money = wallet.add_daily(amount=bonus)

        def print_bonus():
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=new_message_id,
                text=f"🎁 تبریک! جایزه شما: *{bonus}$* {icon}\n💰 موجودی جدید: *{new_money}$*",
            )
        Timer(DICE_DELAY_SEC, print_bonus).start()

    def show_table(self, update: Update, context: CallbackContext) -> None:
        """
        نمایش وضعیت فعلی میز بازی (کارت‌ها و پات) به صورت متنی.
        این متد با کلیک بر روی دکمه "نمایش میز" فراخوانی می‌شود.
        """
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        message_id = update.effective_message.message_id

        if game.state == GameState.INITIAL:
            text = " هنوز بازی شروع نشده است."
        else:
            cards_table_str = " ".join(game.cards_table) if game.cards_table else "🚫 هنوز کارتی رو نشده"
            text = (
                f"📊 *وضعیت فعلی میز:*\n\n"
                f"🎲 *کارت‌های روی میز:* {cards_table_str}\n"
                f"💰 *پات کل:* `{game.pot}`$\n"
                f"📈 *حداکثر شرط دور:* `{game.max_round_rate}`$"
            )

        self._view.send_message_reply(
            chat_id=chat_id,
            message_id=message_id,
            text=text
        )

    def send_cards_to_user(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        current_player = next((p for p in game.players if p.user_id == update.effective_user.id), None)
        if not current_player or not current_player.cards:
            self._view.send_message_reply(chat_id=update.effective_chat.id, message_id=update.effective_message.message_id, text="شما کارتی در این دست ندارید.")
            return
        self._view.send_cards(
            chat_id=update.effective_message.chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=update.effective_message.message_id,
        )

    def _check_access(self, chat_id: ChatId, user_id: UserId) -> bool:
        return any(m.user.id == user_id for m in self._bot.get_chat_administrators(chat_id))

    def _send_cards_private(self, player: Player, cards: Cards) -> None:
        user_chat_model = UserPrivateChatModel(user_id=player.user_id, kv=self._kv)
        private_chat_id = user_chat_model.get_chat_id()
        if private_chat_id is None: raise ValueError("چت خصوصی کاربر یافت نشد.")
        private_chat_id = private_chat_id.decode('utf-8')

        # به جای ارسال عکس، یک پیام با کیبورد جدید ارسال می‌کنیم
        self._view.send_cards(
            chat_id=private_chat_id,
            cards=cards,
            mention_markdown="شما",
            ready_message_id=None
        )

    def _divide_cards(self, game: Game, chat_id: ChatId) -> None:
        for player in game.players:
            player.cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            try:
                self._send_cards_private(player=player, cards=player.cards)
            except Exception as ex:
                print(ex)
                self._view.send_message(chat_id, text=f"⚠️ نتوانستم کارت‌های {player.mention_markdown} را در خصوصی ارسال کنم. لطفاً ربات را در چت خصوصی استارت کنید و از دستور /cards استفاده کنید.")

    def _process_playing(self, chat_id: ChatId, game: Game) -> None:
        if not game.players: return
        game.current_player_index = (game.current_player_index + 1) % len(game.players)
        current_player = self._current_turn_player(game)

        if current_player.user_id == game.trading_end_user_id:
            self._round_rate.to_pot(game)
            self._goto_next_round(game, chat_id)
            if game.state == GameState.INITIAL: return
            game.current_player_index = -1 
            self._process_playing(chat_id, game)
            return

        if game.state == GameState.INITIAL: return
        current_player = self._current_turn_player(game)
        if current_player.wallet.value() <= 0: current_player.state = PlayerState.ALL_IN
        if current_player.state != PlayerState.ACTIVE:
            self._process_playing(chat_id, game)
            return
            
        all_in_active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(all_in_active_players) <= 1:
            self._finish(game, chat_id)
            return

        game.last_turn_time = datetime.datetime.now()
        game.turn_message_id = self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=current_player,
            money=current_player.wallet.value(),
        )

    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId) -> None:
        for _ in range(count): game.cards_table.append(game.remain_cards.pop())
        self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"🃏 کارت‌های روی میز\n💰 پات فعلی: *{game.pot}$*",
        )

    def _finish(self, game: Game, chat_id: ChatId) -> None:
        self._round_rate.to_pot(game)
        print(f"Game finished: {game.id}, Pot: {game.pot}")

        active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        player_scores = self._winner_determine.determinate_scores(players=active_players, cards_table=game.cards_table)
        winners_hand_money = self._round_rate.finish_rate(game=game, player_scores=player_scores)
        
        text = "🏁 بازی با این نتیجه تموم شد:\n\n"
        if not winners_hand_money:
            text += "هیچ برنده‌ای وجود نداشت. احتمالاً همه Fold کرده‌اند."
        else:
            for (player, best_hand, money) in winners_hand_money:
                win_hand_str = " ".join(best_hand)
                text += f"🏆 {player.mention_markdown} مبلغ *{money} $* را با دست `{win_hand_str}` برنده شد!\n"
        
        text += "\nبرای شروع بازی جدید، دستور /ready را ارسال کنید."
        self._view.send_message(chat_id=chat_id, text=text)

        for player in game.players: player.wallet.approve(game.id)
        game.reset()

    def _goto_next_round(self, game: Game, chat_id: ChatId) -> None:
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if len(active_players) <= 1 and len(game.cards_table) == 5:
            self._finish(game, chat_id)
            return

        state_transitions = {
            GameState.ROUND_PRE_FLOP: {"next": GameState.ROUND_FLOP, "cards": 3},
            GameState.ROUND_FLOP: {"next": GameState.ROUND_TURN, "cards": 1},
            GameState.ROUND_TURN: {"next": GameState.ROUND_RIVER, "cards": 1},
            GameState.ROUND_RIVER: {"next": GameState.FINISHED, "cards": 0},
        }
        
        transition = state_transitions.get(game.state)
        if not transition: raise Exception(f"Unexpected game state: {game.state}")

        game.state = transition["next"]
        if transition["cards"] > 0:
            self.add_cards_to_table(count=transition["cards"], game=game, chat_id=chat_id)
        elif game.state == GameState.FINISHED:
            self._finish(game, chat_id)

    def middleware_user_turn(self, fn: Handler) -> Handler:
        def m(update, context):
            game = self._game_from_context(context)
            if game.state == GameState.INITIAL: return
            current_player = self._current_turn_player(game)
            if update.callback_query.from_user.id != current_player.user_id:
                update.callback_query.answer(text="⏳ نوبت شما نیست!", show_alert=True)
                return
            fn(update, context)
            if game.turn_message_id:
                self._view.remove_markup(chat_id=update.effective_chat.id, message_id=game.turn_message_id)
        return m

    def ban_player(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        if game.state in (GameState.INITIAL, GameState.FINISHED): return
        diff = datetime.datetime.now() - game.last_turn_time
        if diff < MAX_TIME_FOR_TURN:
            self._view.send_message(chat_id=chat_id, text=f"⏳ هنوز نمی‌توانید بازیکن را محروم کنید. حداقل زمان برای هر نوبت {MAX_TIME_FOR_TURN.seconds // 60} دقیقه است.")
            return
        self._view.send_message(chat_id=chat_id, text="⏰ زمان بازیکن فعلی تمام شد! به صورت خودکار Fold می‌شود.")
        self.fold(update, context)

    def fold(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        player.state = PlayerState.FOLD
        self._view.send_message(
            chat_id=update.effective_message.chat_id,
            text=f"棄 {player.mention_markdown} از ادامه بازی انصراف داد (Fold)."
        )
        self._process_playing(chat_id=update.effective_message.chat_id, game=game)

    def call_check(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        player = self._current_turn_player(game)
        action = PlayerAction.CALL if player.round_rate < game.max_round_rate else PlayerAction.CHECK
        try:
            amount = game.max_round_rate - player.round_rate
            if player.wallet.value() < amount:
                return self.all_in(update=update, context=context)
            self._view.send_message(chat_id=chat_id, text=f"{'📞' if action == PlayerAction.CALL else '🤝'} {player.mention_markdown} انتخاب کرد: {action.value}")
            self._round_rate.call_check(game, player)
        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=f"خطا: {e}")
            return
        self._process_playing(chat_id=chat_id, game=game)

    def raise_rate_bet(self, update: Update, context: CallbackContext, raise_bet_rate: PlayerAction) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        player = self._current_turn_player(game)
        try:
            action = PlayerAction.RAISE_RATE if game.max_round_rate > 0 else PlayerAction.BET
            amount = self._round_rate.raise_bet(game, player, raise_bet_rate)
            self._view.send_message(chat_id=chat_id, text=f"🔼 {player.mention_markdown} مبلغ را به *{amount}$* افزایش داد ({action.value})")
        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=f"خطا: {e}")
            return
        self._process_playing(chat_id=chat_id, game=game)

    def all_in(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        player = self._current_turn_player(game)
        try:
            amount = self._round_rate.all_in(game, player)
            self._view.send_message(chat_id=chat_id, text=f"🤑 {player.mention_markdown} با *{amount}$* تمام موجودی خود را وارد بازی کرد (All-in)!")
        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=f"خطا: {e}")
            return
        self._process_playing(chat_id=chat_id, game=game)

# ... (WalletManagerModel and RoundRateModel remain the same as the previous correct version)
# The rest of the file is omitted for brevity but should be the same as your last working version.
# Make sure to include WalletManagerModel and RoundRateModel classes from the previous step.

class WalletManagerModel(Wallet):
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv = kv
        self._authorized = {}

    def _key(self, key): return f"user:{self._user_id}:{key}"
    def value(self) -> Money: return int(self._kv.get(self._key("money")) or DEFAULT_MONEY)
    def inc(self, amount: Money) -> Money: return self._kv.incrby(self._key("money"), amount)
    def dec(self, amount: Money) -> Money: return self._kv.decrby(self._key("money"), amount)
    def has_daily_bonus(self) -> bool:
        return self._kv.get(self._key(KEY_LAST_TIME_ADD_MONEY)) == str(datetime.date.today())
    def add_daily(self, amount: Money) -> Money:
        self._kv.set(self._key(KEY_LAST_TIME_ADD_MONEY), str(datetime.date.today()))
        return self.inc(amount)
    def authorized_money(self, game_id) -> Money: return self._authorized.get(game_id, 0)
    def authorize(self, game_id, amount: Money):
        if self.value() < amount: raise UserException("موجودی کافی نیست")
        self.dec(amount)
        self._authorized[game_id] = self._authorized.get(game_id, 0) + amount
    def approve(self, game_id): self._authorized[game_id] = 0

class RoundRateModel:
    def round_pre_flop_rate_before_first_turn(self, game: Game):
        if not game.players or len(game.players) < 2: return
        game.players[0].authorize(game.id, SMALL_BLIND)
        game.players[0].round_rate = SMALL_BLIND
        game.max_round_rate = SMALL_BLIND

    def round_pre_flop_rate_after_first_turn(self, game: Game):
        if not game.players or len(game.players) < 2: return
        big_blind = 2 * SMALL_BLIND
        game.players[1].authorize(game.id, big_blind)
        game.players[1].round_rate = big_blind
        game.max_round_rate = big_blind
        game.trading_end_user_id = game.players[1].user_id

    def call_check(self, game: Game, player: Player):
        amount = game.max_round_rate - player.round_rate
        player.authorize(game.id, amount)
        player.round_rate += amount

    def raise_bet(self, game: Game, player: Player, action: PlayerAction) -> Money:
        amount = game.max_round_rate - player.round_rate + action.value
        player.authorize(game.id, amount)
        player.round_rate += amount
        game.max_round_rate = player.round_rate
        game.trading_end_user_id = player.user_id
        return player.round_rate

    def all_in(self, game: Game, player: Player) -> Money:
        amount = player.wallet.value()
        player.authorize(game.id, amount)
        player.round_rate += amount
        player.state = PlayerState.ALL_IN
        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate
            game.trading_end_user_id = player.user_id
        return player.round_rate

    def _sum_authorized_money(self, game: Game, players: List[Tuple[Player, Cards]]) -> int:
        return sum(p[0].wallet.authorized_money(game_id=game.id) for p in players)

    def finish_rate(self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]) -> List[Tuple[Player, Cards, Money]]:
        sorted_player_scores = sorted(player_scores.items(), reverse=True, key=lambda x: x[0])
        res = []
        for _, win_players in sorted_player_scores:
            if game.pot <= 0: break
            players_authorized = self._sum_authorized_money(game=game, players=win_players)
            if players_authorized <= 0: continue
            
            for win_player, best_hand in win_players:
                if game.pot <= 0: break
                authorized = win_player.wallet.authorized_money(game_id=game.id)
                win_money_real = round(game.pot * (authorized / players_authorized)) if players_authorized > 0 else 0
                win_money_can_get = authorized * len(game.players)
                win_money = min(win_money_real, win_money_can_get)
                win_player.wallet.inc(win_money)
                game.pot -= win_money
                res.append((win_player, best_hand, win_money))
        return res

    def to_pot(self, game):
        for p in game.players:
            game.pot += p.round_rate
            p.round_rate = 0
        game.max_round_rate = 0
        if game.players:
            game.trading_end_user_id = game.players[0].user_id
