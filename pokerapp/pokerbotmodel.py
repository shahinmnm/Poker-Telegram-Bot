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
        i = game.current_player_index % len(game.players)
        return game.players[i]

    def ready(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id

        if game.state != GameState.INITIAL:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!"
            )
            return

        if len(game.players) > MAX_PLAYERS:
            self._view.send_message_reply(
                chat_id=chat_id,
                text="🚫 ظرفیت اتاق پر است!",
                message_id=update.effective_message.message_id,
            )
            return

        user = update.effective_message.from_user

        if user.id in game.ready_users:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="✅ شما قبلاً آماده شده‌اید.",
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
                text="💸 موجودی شما کافی نیست.",
            )

        game.ready_users.add(user.id)
        game.players.append(player)

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
                text="🎯 بازی در حال انجام است!"
            )
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
            self._view.send_message(
                chat_id=chat_id,
                text="👥 تعداد بازیکنان کافی نیست!"
            )

    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        print(f"new game: {game.id}, players count: {len(game.players)}")

        self._view.send_message(
            chat_id=chat_id,
            text='🚀 بازی شروع شد! 🃏',
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[["♠ پوکر"]],
                resize_keyboard=True,
            ),
        )

        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
        old_players_ids = old_players_ids[-1:] + old_players_ids[:-1]

        def index(ln: List, obj) -> int:
            try:
                return ln.index(obj)
            except ValueError:
                return -1

        game.players.sort(key=lambda p: index(old_players_ids, p.user_id))
        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game=game, chat_id=chat_id)

        game.current_player_index = 1
        self._round_rate.round_pre_flop_rate_before_first_turn(game)
        self._process_playing(chat_id=chat_id, game=game)
        self._round_rate.round_pre_flop_rate_after_first_turn(game)
        context.chat_data[KEY_OLD_PLAYERS] = list(map(lambda p: p.user_id, game.players))

    def bonus(self, update: Update, context: CallbackContext) -> None:
        wallet = WalletManagerModel(update.effective_message.from_user.id, self._kv)
        money = wallet.value()
        chat_id = update.effective_message.chat_id
        message_id = update.effective_message.message_id

        if wallet.has_daily_bonus():
            return self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text=f"💰 موجودی شما: *{money}$*",
            )

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
        money = wallet.add_daily(amount=bonus)

        def print_bonus() -> None:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=message_id,
                text=f"🎁 جایزه: *{bonus}$* {icon}" +
                     f"💰 موجودی شما: *{money}$*",
            )

        Timer(DICE_DELAY_SEC, print_bonus).start()

    def _send_cards_private(self, player: Player, cards: Cards) -> None:
        user_chat_model = UserPrivateChatModel(user_id=player.user_id, kv=self._kv)
        private_chat_id = user_chat_model.get_chat_id()
        if private_chat_id is None:
            raise ValueError("چت خصوصی یافت نشد")

        private_chat_id = private_chat_id.decode('utf-8')
        message_id = self._view.send_desk_cards_img(
            chat_id=private_chat_id,
            cards=cards,
            caption="🎴 کارت‌های شما",
            disable_notification=False,
        ).message_id

        try:
            rm_msg_id = user_chat_model.pop_message()
            while rm_msg_id is not None:
                try:
                    rm_msg_id = rm_msg_id.decode('utf-8')
                    self._view.remove_message(chat_id=private_chat_id, message_id=rm_msg_id)
                except Exception as ex:
                    print("remove_message", ex)
                    traceback.print_exc()
                rm_msg_id = user_chat_model.pop_message()

            user_chat_model.push_message(message_id=message_id)
        except Exception as ex:
            print("bulk_remove_message", ex)
            traceback.print_exc()

    def _finish(self, game: Game, chat_id: ChatId) -> None:
        self._round_rate.to_pot(game)
        active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        player_scores = self._winner_determine.determinate_scores(players=active_players, cards_table=game.cards_table)
        winners_hand_money = self._round_rate.finish_rate(game=game, player_scores=player_scores)
        only_one_player = len(active_players) == 1

        text = "🏁 **بازی به پایان رسید!**"
        for (player, best_hand, money) in winners_hand_money:
            win_hand = " ".join(best_hand)
            text += f"{player.mention_markdown}:💰 برنده: *{money}$*"
            if not only_one_player:
                text += f"با ترکیب کارت‌ها:{win_hand}"
        text += "برای ادامه `/ready` را بزنید"
        self._view.send_message(chat_id=chat_id, text=text)

        for player in game.players:
            player.wallet.approve(game.id)
        game.reset()
