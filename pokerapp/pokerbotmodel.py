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
from pokerapp.winnerdetermination import WinnerDetermination, HAND_RANK, HandsOfPoker
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
        self._round_rate = RoundRateModel(view=self._view)

    @property
    def _min_players(self):
        if self._cfg.DEBUG:
            return 1
        return MIN_PLAYERS
    @staticmethod
    def _calc_call_amount(game: Game, player: Player) -> int:
        return max(0, self._calc_call_amount(game, player))


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
        user = update.effective_message.from_user
    
        # جلوگیری اگر بازی شروع شده
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
    
        # بررسی موجودی
        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < 2 * SMALL_BLIND:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text=f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {2*SMALL_BLIND}$ نیاز است).",
            )
            return
    
        # اگر بازیکن از قبل آماده نبوده، اضافه کن
        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.effective_message.message_id,
            )
            game.ready_users.add(user.id)
            game.players.append(player)
    
        # متن لیست بازیکنان آماده
        ready_list = "\n".join(
            [f"{i+1}. {p.mention_markdown} 🟢" for i, p in enumerate(game.players)]
        )
        total_ready = len(game.players)
    
        text = (
            f"👥 *لیست بازیکنان آماده*\n\n"
            f"{ready_list}\n\n"
            f"📊 {total_ready}/{MAX_PLAYERS} بازیکن آماده\n\n"
            f"🚀 برای شروع بازی دکمه زیر را بزنید 👇"
        )
    
        from telegram import ReplyKeyboardMarkup
        keyboard = ReplyKeyboardMarkup(
            [["/ready", "/start"]],
            resize_keyboard=True,
            one_time_keyboard=False
        )
    
        # اگر پیام قبلی وجود داشت، ویرایشش کن؛ در غیر اینصورت اولین بار بفرست
        if hasattr(game, "ready_message_main_id") and game.ready_message_main_id:
            try:
                self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=game.ready_message_main_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception as e:
                print(f"Could not edit ready list message: {e}")
        else:
            try:
                msg = self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                game.ready_message_main_id = msg.message_id
            except Exception as e:
                print(f"Error sending ready list message: {e}")
    
        # <- اینجا اضافه کن
        # پاک کردن پیام‌های قدیمی آماده‌سازی
        for msg_id in getattr(game, "message_ids_to_delete", []):
            self._view.remove_message(chat_id, msg_id)
        game.message_ids_to_delete.clear()
    
        try:
            # اگر همه حاضر بودن، خودکار شروع کن
            members_count = self._bot.get_chat_member_count(chat_id)
            players_active = len(game.players)
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
    def _starting_player_index(self, game: Game, street: GameState) -> int:
        num_players = len(game.players)
        dealer_index = getattr(game, "dealer_index", 0)

        if street == GameState.ROUND_PRE_FLOP:
            # Small Blind
            sb_index = (dealer_index + 1) % num_players
            # Big Blind
            bb_index = (dealer_index + 2) % num_players
            # نفر بعد از BB شروع می‌کند
            return (bb_index + 1) % num_players
        else:
            # Flop, Turn, River: نفر سمت چپ Dealer
            return (dealer_index + 1) % num_players

    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        if not hasattr(game, 'dealer_index'):
            game.dealer_index = 0
        else:
            game.dealer_index = (game.dealer_index + 1) % len(game.players)
        print(f"new game: {game.id}, players count: {len(game.players)}")
    
        self._view.send_message(
            chat_id=chat_id,
            text='🚀 !بازی شروع شد!',
            reply_markup=ReplyKeyboardMarkup(keyboard=[["poker"]], resize_keyboard=True),
        )
    
        old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
        old_players_ids = old_players_ids[-1:] + old_players_ids[:-1]
        def index(ln: List, obj) -> int:
            try:
                return ln.index(obj)
            except ValueError:
                return -1
        game.players.sort(key=lambda p: index(old_players_ids, p.user_id))
    
        # تعیین Dealer
        game.dealer_index = 0 if not hasattr(game, "dealer_index") else (game.dealer_index + 1) % len(game.players)
    
        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game=game, chat_id=chat_id)
    
        # ست کردن Blindها
        self._round_rate.round_pre_flop_rate_before_first_turn(game)
        self._round_rate.to_pot(game, chat_id)
    
        # تعیین نفر شروع Pre-Flop
        game.current_player_index = self._starting_player_index(game, GameState.ROUND_PRE_FLOP)
    
        self._process_playing(chat_id=chat_id, game=game)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]
    
    def _fast_forward_to_finish(self, game: Game, chat_id: ChatId):
        """ When no more betting is possible, reveals all remaining cards """
        print("Fast-forwarding to finish...")
        self.to_pot_and_update(chat_id, game)
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
    
            # انتخاب دو کارت برای بازیکن
            cards = player.cards = [
                game.remain_cards.pop(),
                game.remain_cards.pop(),
            ]
    
            try:
                # ارسال کارت‌ها در PV (اگر کاربر /start کرده)
                self._send_cards_private(player=player, cards=cards)
    
                # ارسال همزمان در گروه با کیبورد کارتی
                msg_id_group = self._view.send_cards(
                    chat_id=chat_id,
                    cards=cards,
                    mention_markdown=player.mention_markdown,
                    ready_message_id=player.ready_message_id,
                )
                if msg_id_group:
                    game.message_ids_to_delete.append(msg_id_group)
    
            except Exception as ex:
                # اگر PV شکست خورد، هشدار بده و فقط گروه بفرست
                print(ex)
                msg_id_warn = self._view.send_message_return_id(
                    chat_id,
                    f"⚠️ {player.mention_markdown} ربات را در چت خصوصی استارت نکرده است. "
                    "ارسال کارت‌ها در گروه انجام می‌شود. لطفاً ربات را استارت کنید."
                )
                if msg_id_warn:
                    game.message_ids_to_delete.append(msg_id_warn)
    
                msg_id_group = self._view.send_cards(
                    chat_id=chat_id,
                    cards=cards,
                    mention_markdown=player.mention_markdown,
                    ready_message_id=player.ready_message_id,
                )
                if msg_id_group:
                    game.message_ids_to_delete.append(msg_id_group)
                    
        def _big_blind_last_action(self, game: Game) -> bool:
            bb_index = (game.dealer_index + 2) % len(game.players)
            bb_player = game.players[bb_index]
            return (not bb_player.has_acted and bb_player.state == PlayerState.ACTIVE
                    and game.max_round_rate == (2 * SMALL_BLIND))
    
        def _process_playing(self, chat_id: ChatId, game: Game) -> None:
            if game.state not in self.ACTIVE_GAME_STATES:
                return
        
            active_and_all_in_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
            if len(active_and_all_in_players) <= 1:
                return self._finish(game, chat_id)
        
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
        
            # بررسی پایان Street
            round_over = False
            if active_players:
                all_acted = all(p.has_acted for p in active_players)
                all_matched = len(set(p.round_rate for p in active_players)) == 1
                if all_acted and all_matched:
                    if not (game.state == GameState.ROUND_PRE_FLOP and self._big_blind_last_action(game)):
                        round_over = True
            else:
                round_over = True
        
            if round_over:
                self._round_rate.to_pot(game, chat_id)
                if len(game.players_by(states=(PlayerState.ACTIVE,))) < 2:
                    return self._fast_forward_to_finish(game, chat_id)
                self._goto_next_round(game, chat_id)
                if game.state in self.ACTIVE_GAME_STATES:
                    return self._process_playing(chat_id, game)
                return
        
            # حرکت به بازیکن ACTIVE بعدی
            num_players = len(game.players)
            for _ in range(num_players):
                game.current_player_index = (game.current_player_index + 1) % num_players
                current_player = self._current_turn_player(game)
                if current_player.state == PlayerState.ACTIVE:
                    break
            else:
                print("No active player found in _process_playing.")
                return self._finish(game, chat_id)
        
            # ارسال نوبت بازیکن
            game.last_turn_time = datetime.datetime.now()
            if game.turn_message_id:
                self._view.remove_message(chat_id, game.turn_message_id)
            msg_id = self._view.send_turn_actions(
                chat_id=chat_id, game=game, player=current_player, money=current_player.wallet.value()
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

    def _finish(self, game: Game, chat_id: ChatId) -> None:
        print(f"Game finishing: {game.id}, pot: {game.pot}")
    
        # حذف پیام نوبت
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None
    
        # انتقال چیپ‌های باقیمانده بازیکنان به پات
        for p in game.players:
            p.total_bet += p.round_rate
            game.pot += p.round_rate
            p.round_rate = 0
    
        active_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
    
        # توضیحات و ایموجی‌های دست‌ها
        hand_descriptions = {
            "ROYAL_FLUSH": "رویال فلاش — پنج کارت از ۱۰ تا آس همخال",
            "STRAIGHT_FLUSH": "استریت فلاش — پنج کارت پشت سر هم همخال",
            "FOUR_OF_A_KIND": "چهار کارت هم‌ارزش",
            "FULL_HOUSE": "سه‌تایی + یک جفت",
            "FLUSH": "پنج کارت هم‌خال",
            "STRAIGHTS": "پنج کارت پشت سر هم",
            "THREE_OF_A_KIND": "سه کارت هم‌ارزش",
            "TWO_PAIR": "دو جفت کارت هم‌ارزش",
            "PAIR": "دو کارت هم‌ارزش",
            "HIGH_CARD": "بالاترین کارت",
        }
        emoji_map = {
            "ROYAL_FLUSH": "👑",
            "STRAIGHT_FLUSH": "💎",
            "FOUR_OF_A_KIND": "💥",
            "FULL_HOUSE": "🏠",
            "FLUSH": "🌊",
            "STRAIGHTS": "📏",
            "THREE_OF_A_KIND": "🎯",
            "TWO_PAIR": "✌️",
            "PAIR": "👥",
            "HIGH_CARD": "⭐",
        }
    
        # بدون بازیکن فعال
        if not active_players:
            text = "🏁 این دست بدون برنده پایان یافت."
    
        # تنها یک بازیکن
        elif len(active_players) == 1:
            winner = active_players[0]
            winner.wallet.inc(game.pot)
            text = (
                "🏁 دست پایان یافت\n\n"
                f"🏆 {winner.mention_markdown}\n"
                f"📥 برنده *{game.pot}$* شد (با فولد بقیه)."
            )
    
        # رقابت نهایی (Showdown)
        else:
            while len(game.cards_table) < 5 and game.remain_cards:
                game.cards_table.append(game.remain_cards.pop())
    
            table_msg = self._view.send_desk_cards_img(
                chat_id=chat_id,
                cards=game.cards_table,
                caption=f"🃏 میز نهایی — 💰 پات: {game.pot}$"
            )
            if table_msg:
                game.message_ids_to_delete.append(table_msg.message_id)
    
            scores = self._winner_determine.determinate_scores(active_players, game.cards_table)
            winners_money = self._round_rate.finish_rate(game, scores)
    
            player_best_hand_map: Dict[UserId, list] = {}
            for score, plist in scores.items():
                for player, best_cards in plist:
                    player_best_hand_map[player.user_id] = best_cards
    
            def hand_rank_key(hand_name: str) -> int:
                try:
                    return HandsOfPoker[hand_name.replace(" ", "_").upper()].value
                except KeyError:
                    return 0
    
            def cards_to_emoji(cards: list) -> str:
                return " ".join(str(c) for c in cards)
    
            lines = []
            for hand_name, plist in sorted(
                winners_money.items(),
                key=lambda x: hand_rank_key(x[0]),
                reverse=True
            ):
                hand_key = hand_name.replace(" ", "_").upper()
                desc = hand_descriptions.get(hand_key, "")
                emo = emoji_map.get(hand_key, "")
                if desc:
                    lines.append(f"\n*{hand_key}* - {desc} {emo}")
                else:
                    lines.append(f"\n*{hand_key}* {emo}")
    
                for player, money in plist:
                    cards_str = cards_to_emoji(player_best_hand_map.get(player.user_id, []))
                    lines.append(f"🏆 {player.mention_markdown} ➡️ `{money}$` {cards_str}")
    
            text = "🏁 دست پایان یافت\n" + "\n".join(lines)
    
        # ارسال پیام نتیجه
        self._view.send_message(chat_id=chat_id, text=text)
    
        # حذف پیام‌های موقت
        for mid in getattr(game, "message_ids_to_delete", []):
            self._view.remove_message_delayed(chat_id, mid, delay=1.0)
        game.message_ids_to_delete.clear()
    
        if getattr(game, "ready_message_main_id", None):
            self._view.remove_message_delayed(chat_id, game.ready_message_main_id, delay=1.0)
            game.ready_message_main_id = None
    
        game.state = GameState.FINISHED
    
        # شروع دور بعدی
        if getattr(self._cfg, "MANUAL_READY_MODE", True):
            def reset_game():
                game.reset()
                msg_id_ready = self._view.send_message_return_id(
                    chat_id=chat_id,
                    text="✅ با دستور /ready برای دست بعد آماده شوید."
                )
                if msg_id_ready:
                    Timer(4.0, lambda: self._view.remove_message(chat_id, msg_id_ready)).start()
            Timer(3.0, reset_game).start()
        else:
            Timer(3.0, lambda: self._start_game(context=None, game=game, chat_id=chat_id)).start()

        
        def _goto_next_round(self, game: Game, chat_id: ChatId) -> None:
            if game.state == GameState.ROUND_PRE_FLOP:
                self.add_cards_to_table(3, game, chat_id)
                game.state = GameState.ROUND_FLOP
            elif game.state == GameState.ROUND_FLOP:
                self.add_cards_to_table(1, game, chat_id)
                game.state = GameState.ROUND_TURN
            elif game.state == GameState.ROUND_TURN:
                self.add_cards_to_table(1, game, chat_id)
                game.state = GameState.ROUND_RIVER
            else:
                return self._finish(game, chat_id)
        
            # ریست بازیکنان ACTIVE
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                p.has_acted = False
                p.round_rate = 0
            game.max_round_rate = 0
        
            # تعیین نفر شروع‌کننده Street جدید
            game.current_player_index = self._starting_player_index(game, game.state)
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
        chat_id = update.effective_chat.id
        player = self._current_turn_player(game)

        if not player: return

        player.state = PlayerState.FOLD
        player.has_acted = True

        action_text = "محروم و فولد شد" if is_ban else PlayerAction.FOLD.value
        msg_id = self._view.send_message_return_id(
            chat_id=chat_id,
            text=f"{player.mention_markdown} {action_text}"
        )
        if msg_id: game.message_ids_to_delete.append(msg_id)

        self._process_playing(chat_id=chat_id, game=game)

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
            amount_to_call = self._calc_call_amount(game, player)
            if player.wallet.value() <= amount_to_call:
                return self.all_in(update=update, context=context)
            
            self._round_rate.call_check(game, player)

            msg_id = self._view.send_message_return_id(
                chat_id=chat_id,
                text=f"{player.mention_markdown} {action}"
            )
            if msg_id: game.message_ids_to_delete.append(msg_id)

        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=str(e))
            return

        self._process_playing(chat_id=chat_id, game=game)

    def raise_rate_bet(
        self,
        update: Update,
        context: CallbackContext,
        raise_bet_rate: int
    ) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_message.chat_id
        player = self._current_turn_player(game)
        
        # === START OF CHANGE ===
        # The variable 'raise_bet_rate' is already the integer value (e.g., 10, 25, 50).
        # We no longer need to access '.value'.
        amount_to_raise = raise_bet_rate
        # === END OF CHANGE ===
    
        try:
            # --- START OF NEW, SELF-CONTAINED LOGIC ---
    
            # 1. Determine action name: "BET" if no previous bet, "RAISE" otherwise.
            action = PlayerAction.BET if game.max_round_rate == 0 else PlayerAction.RAISE_RATE
    
            # 2. Calculate amount needed to call.
            call_amount = self._calc_call_amount(game, player)
    
            # 3. Calculate total amount to deduct from wallet (call + raise).
            total_required_from_wallet = call_amount + amount_to_raise
    
            # 4. Check wallet balance.
            if player.wallet.value() < total_required_from_wallet:
                raise UserException("موجودی شما برای این حرکت کافی نیست.")
    
            # 5. Perform transactions.
            player.wallet.dec(total_required_from_wallet)
            player.round_rate += total_required_from_wallet
            
            # 6. Update game state.
            game.max_round_rate = player.round_rate
            game.last_raise = amount_to_raise
    
            # 7. Reset 'has_acted' for other active players for the next turn.
            for p in game.players:
                if p.state == PlayerState.ACTIVE and p.user_id != player.user_id:
                    p.has_acted = False
            
            # --- END OF NEW LOGIC ---
    
            # Send confirmation message to the group
            mention_markdown = player.mention_markdown
            self._view.send_message(
                chat_id=chat_id,
                text=f"{mention_markdown} {action.value} به *{player.round_rate}$*"
            )
    
        except UserException as e:
            self._view.send_message(chat_id=chat_id, text=str(e))
            return
        except Exception as e:
            self._view.send_message(
                chat_id=chat_id, text="یک خطای بحرانی در پردازش حرکت رخ داد. بازی ریست می‌شود.")
            print(f"FATAL: Unhandled exception in raise_rate_bet: {e}")
            traceback.print_exc()
            game.reset()
            return
    
        # If successful, move to the next player.
        self._process_playing(
            chat_id=chat_id,
            game=game,
        )

    def all_in(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        player = self._current_turn_player(game)

        if not player: return

        amount = self._round_rate.all_in(game, player)

        msg_id = self._view.send_message_return_id(
            chat_id=chat_id,
            text=f"{player.mention_markdown} {PlayerAction.ALL_IN.value} ({amount}$)"
        )
        if msg_id: game.message_ids_to_delete.append(msg_id)
        self._process_playing(chat_id=chat_id, game=game)

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
    def __init__(self, view: PokerBotViewer):
        self._view = view
    def to_pot(self, game: Game, chat_id: ChatId) -> None:
        # This function moves money from the current betting round to the main pot
        # and also updates each player's total bet for the hand.
        if game.state == GameState.INITIAL or game.state == GameState.FINISHED:
             return
             
        pot_increase = 0
        for p in game.players:
            pot_increase += p.round_rate
            p.total_bet += p.round_rate
            p.round_rate = 0
        
        game.pot += pot_increase
        game.max_round_rate = 0
        game.last_raise = 0
        
        if pot_increase > 0:
            print(f"Moved {pot_increase} to pot. New pot: {game.pot}")
        self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"💰 پات فعلی: {game.pot}$",
        )

    def call_check(self, game: Game, player: Player) -> None:
        amount_to_add = self._calc_call_amount(game, player)
        if amount_to_add > 0:
            player.wallet.dec(amount_to_add)
            player.round_rate += amount_to_add
        player.has_acted = True

    def all_in(self, game: Game, player: Player) -> Money:
        amount = player.wallet.value()
        player.round_rate += amount
        player.wallet.dec(amount)
        player.state = PlayerState.ALL_IN
        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate
        player.has_acted = True
        return player.round_rate

    def raise_rate_bet(
        self,
        game: Game, player: Player, raise_bet_amount: int
    ) -> Tuple[Money, bool]: # returns amount, is_all_in
        
        # Calculate minimum valid raise amount
        min_raise_value = game.max_round_rate + game.last_raise
        if game.max_round_rate == 0: # This is a bet, not a raise
             min_raise_value = max(raise_bet_amount, 2 * SMALL_BLIND)
        
        final_bet_amount = raise_bet_amount + game.max_round_rate
        
        if final_bet_amount < min_raise_value and player.wallet.value() > (final_bet_amount - player.round_rate):
             raise UserException(f"حداقل رِیز/بِت باید {min_raise_value - game.max_round_rate}$ باشد.")

        money_to_add = final_bet_amount - player.round_rate
        
        is_all_in = False
        if money_to_add >= player.wallet.value():
             final_bet_amount = player.round_rate + player.wallet.value()
             self.all_in(game, player)
             is_all_in = True
        else:
            player.wallet.dec(money_to_add)
            player.round_rate += money_to_add
            game.last_raise = final_bet_amount - game.max_round_rate
            game.max_round_rate = final_bet_amount
            player.has_acted = True
        
            # After a raise, all other active players need to act again
            for p in game.players:
                if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                    p.has_acted = False

        return final_bet_amount, is_all_in


    def round_pre_flop_rate_before_first_turn(self, game: Game) -> None:
        num_players = len(game.players)
        # In 2-player (Heads-Up), player 0 is Dealer and SB, player 1 is BB.
        sb_player = game.players[0 % num_players]
        bb_player = game.players[1 % num_players]

        # Small Blind
        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.wallet.dec(sb_amount)
        sb_player.round_rate = sb_amount
        if sb_amount >= sb_player.wallet.value() + sb_amount: sb_player.state = PlayerState.ALL_IN

        # Big Blind
        bb_amount = min(2 * SMALL_BLIND, bb_player.wallet.value())
        bb_player.wallet.dec(bb_amount)
        bb_player.round_rate = bb_amount
        if bb_amount >= bb_player.wallet.value() + bb_amount: bb_player.state = PlayerState.ALL_IN
        
        game.max_round_rate = 2 * SMALL_BLIND
        game.last_raise = SMALL_BLIND # The difference between BB and SB

    def finish_rate(
        self,
        game: Game,
        player_scores: Dict[Score, List[Tuple[Player, Cards]]],
    ) -> Dict[str, List[Tuple[Player, Money]]]:
        """محاسبه برندگان و مبلغ برد آن‌ها، گروه‌بندی‌شده بر اساس نام دست"""
        
        # همه بازیکنان که هنوز در دست هستند (ACTIVE یا ALL_IN)
        active_or_all_in = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if not active_or_all_in:
            return {}
        
        # مجموع شرط هر بازیکن
        total_bets = {p.user_id: p.total_bet for p in game.players if p.total_bet > 0}
        sorted_unique_bets = sorted(set(total_bets.values()))  # برای تشخیص side pots
        
        side_pots = []
        last_bet_level = 0

        for bet_level in sorted_unique_bets:
            pot_amount = 0
            eligible_players_ids = []

            # محاسبه سهم هر بازیکن برای این سطح pot
            for player_id, player_bet in total_bets.items():
                contribution = min(player_bet, bet_level) - last_bet_level
                if contribution > 0:
                    pot_amount += contribution

            for player in active_or_all_in:
                if total_bets.get(player.user_id, 0) >= bet_level:
                    eligible_players_ids.append(player.user_id)

            if pot_amount > 0:
                side_pots.append({
                    "amount": pot_amount,
                    "eligible_players_ids": eligible_players_ids
                })

            last_bet_level = bet_level

        # دیکشنری نتیجه نهایی: {نام دست: [(بازیکن, مبلغ), ...]}
        final_winnings: Dict[str, List[Tuple[Player, Money]]] = {}

        for pot in side_pots:
            eligible_winners = []
            best_score_in_pot = -1
            sorted_scores = sorted(player_scores.keys(), reverse=True)

            # پیدا کردن بهترین دست‌ها در این pot
            for score in sorted_scores:
                for player, hand_cards in player_scores[score]:
                    if player.user_id in pot["eligible_players_ids"]:
                        if best_score_in_pot == -1:
                            best_score_in_pot = score
                        if score == best_score_in_pot:
                            eligible_winners.append((player, hand_cards))
                if best_score_in_pot != -1:
                    break  # فقط بالاترین امتیاز را نگه داریم

            if not eligible_winners:
                continue

            # تقسیم مبلغ pot بین برندگان
            win_share = pot["amount"] // len(eligible_winners)
            remainder = pot["amount"] % len(eligible_winners)

            for idx, (winner, hand_cards) in enumerate(eligible_winners):
                payout = win_share + (1 if idx < remainder else 0)
                winner.wallet.inc(payout)  # آپدیت موجودی
                
                hand_name = self._hand_name_from_score(best_score_in_pot)

                if hand_name not in final_winnings:
                    final_winnings[hand_name] = []
                final_winnings[hand_name].append((winner, payout))

        return final_winnings

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"


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
