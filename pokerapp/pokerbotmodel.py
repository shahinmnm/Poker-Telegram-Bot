#!/usr/bin/env python3

import datetime
import traceback
from threading import Timer
from typing import List, Tuple, Dict, Optional

import redis
from telegram import Message, ReplyKeyboardMarkup, Update, Bot
from telegram.ext import Handler, CallbackContext

from pokerapp.config import Config
# فرض بر این است که این فایل‌ها در مسیر درست قرار دارند
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
        # کلاس RoundRateModel که قبلا در انتهای فایل بود به اینجا منتقل شد
        # و به عنوان یک property از PokerBotModel نمونه‌سازی می‌شود.
        self._round_rate = RoundRateModel(view=self._view)

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

    # ==================== متد ready (اصلاح شده) ====================
    def ready(self, update: Update, context: CallbackContext) -> None:
        print("DEBUG: Inside model.ready()")
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user = update.effective_message.from_user
        print(f"DEBUG: Game state is {game.state}")

        # جلوگیری اگر بازی شروع شده
        if game.state != GameState.INITIAL:
            print("DEBUG: Condition failed: Game already started.")
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!"
            )
            return
        
        print("DEBUG: Condition passed: Game state is INITIAL.")

        if len(game.players) >= MAX_PLAYERS:
            print("DEBUG: Condition failed: Room is full.")
            self._view.send_message_reply(
                chat_id=chat_id,
                text="🚪 اتاق پر است!",
                message_id=update.effective_message.message_id,
            )
            return

        print(f"DEBUG: Condition passed: Room not full ({len(game.players)}/{MAX_PLAYERS}).")

        # بررسی موجودی
        wallet = WalletManagerModel(user.id, self._kv)
        try:
            user_money = wallet.value()
            print(f"DEBUG: Checking wallet for user {user.id}. Money: {user_money}")
            if user_money < 2 * SMALL_BLIND:
                print("DEBUG: Condition failed: Not enough money.")
                self._view.send_message_reply(
                    chat_id=chat_id,
                    message_id=update.effective_message.message_id,
                    text=f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {2*SMALL_BLIND}$ نیاز است).",
                )
                return
        except Exception as e:
            print(f"CRITICAL ERROR: Failed to get wallet value for user {user.id}.")
            traceback.print_exc() # این خطاها را کامل چاپ می‌کند
            return

        print("DEBUG: Condition passed: User has enough money.")

        # اگر بازیکن از قبل آماده نبوده، اضافه کن
        if user.id not in game.ready_users:
            print(f"DEBUG: User {user.id} is new. Adding to players list.")
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.effective_message.message_id,
            )
            game.ready_users.add(user.id)
            game.players.append(player)
        else:
            print(f"DEBUG: User {user.id} was already in ready_users.")


    # =============================================================

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
        
        if user_id not in game.ready_users:
            self._view.send_message_reply(
                chat_id=chat_id,
                message_id=update.effective_message.message_id,
                text="❌ شما در لیست بازیکنان آماده نیستید! ابتدا /ready را بزنید."
            )
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
            sb_index = (dealer_index + 1) % num_players
            bb_index = (dealer_index + 2) % num_players
            return (bb_index + 1) % num_players
        else:
            return (dealer_index + 1) % num_players

    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        if hasattr(game, 'dealer_index'):
            game.dealer_index = (game.dealer_index + 1) % len(game.players)
        else:
            game.dealer_index = 0
            
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
        
        # پاک کردن پیام لیست بازیکنان آماده
        if game.ready_message_main_id:
            self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None
            
        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game=game, chat_id=chat_id)

        print("DEBUG: Setting up blinds for Pre-Flop.")
        num_players = len(game.players)
        dealer_index = game.dealer_index

        sb_player = game.players[(dealer_index + 1) % num_players]
        bb_player = game.players[(dealer_index + 2) % num_players]

        print(f"DEBUG: Dealer: {game.players[dealer_index].mention_markdown}, SB: {sb_player.mention_markdown}, BB: {bb_player.mention_markdown}")

        sb_amount = min(SMALL_BLIND, sb_player.wallet.value())
        sb_player.wallet.dec(sb_amount)
        sb_player.round_rate = sb_amount
        sb_player.total_bet = sb_amount
        sb_player.has_acted = False 

        bb_amount = min(SMALL_BLIND * 2, bb_player.wallet.value())
        bb_player.wallet.dec(bb_amount)
        bb_player.round_rate = bb_amount
        bb_player.total_bet = bb_amount
        bb_player.has_acted = False

        game.pot = sb_amount + bb_amount
        game.max_round_rate = bb_amount

        print(f"DEBUG: Blinds posted. Pot: {game.pot}, Max Round Rate: {game.max_round_rate}")

        start_player_index = (dealer_index + 3) % num_players
        game.current_player_index = self._find_next_active_player_index(game, start_player_index)

        print(f"DEBUG: Pre-Flop starting player is at index {game.current_player_index}: {self._current_turn_player(game).mention_markdown}")

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
    def _is_round_finished(self, game: Game) -> Tuple[bool, bool]:
        """
        بررسی می‌کند که آیا دور شرط‌بندی تمام شده است یا خیر.

        یک تاپل (bool, bool) برمی‌گرداند:
        - (True, False): دور تمام شده، به مرحله بعد (street) بروید.
        - (True, True): دور تمام شده چون بازیکنان all-in هستند، مستقیم به showdown بروید.
        - (False, False): دور هنوز تمام نشده است.
        """
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        all_in_players = game.players_by(states=(PlayerState.ALL_IN,))

        # اگر فقط یک بازیکن فعال مانده (بقیه فولد کرده‌اند)، بازی باید تمام شود نه فقط دور.
        # این منطق در جای دیگری هندل می‌شود، اینجا فقط دور شرط‌بندی را چک می‌کنیم.
        if len(active_players) < 2:
            # اگر بازیکنان All-in وجود دارند، باید به Showdown برویم
            if len(all_in_players) > 0:
                 # اگر هیچ بازیکنی برای ادامه شرط بندی باقی نمانده، دور تمام است
                 if len(active_players) == 0:
                     return True, True # Showdown
                 # اگر یک بازیکن فعال و چند بازیکن آل-این داریم، باید دید شرط‌ها برابر است یا نه
                 if all(p.total_bet >= max(player.total_bet for player in active_players) for p in all_in_players):
                     return True, True

            # اگر هیچ بازیکنی برای شرط‌بندی نمانده، دور تمام است.
            if len(active_players) <= 1 and not game.all_in_players_are_covered():
                return True, True # Showdown

        # همه بازیکنان فعال باید بازی کرده باشند
        all_acted = all(p.has_acted for p in active_players)
        if not all_acted:
            return False, False  # هنوز بازیکنانی هستند که بازی نکرده‌اند

        # همه بازیکنان فعال باید مبلغ شرط یکسانی گذاشته باشند
        # (مگر اینکه آل-این شده باشند که وضعیتشان ACTIVE نیست)
        if len(active_players) > 0:
            first_player_rate = active_players[0].round_rate
            all_rates_equal = all(p.round_rate == first_player_rate for p in active_players)
            if not all_rates_equal:
                return False, False # شرط‌ها هنوز برابر نشده

        # اگر تمام شرایط بالا برقرار بود، دور تمام شده است.
        return True, False
        
    def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """
        پیام نوبت را برای بازیکن مشخص شده ارسال می‌کند و شناسه پیام را در آبجکت game ذخیره می‌کند.
        """
        print(f"DEBUG: Sending turn message for player {player.mention_markdown}.")

        # حذف دکمه‌های پیام نوبت قبلی، اگر وجود داشته باشد
        if game.turn_message_id:
            print(f"DEBUG: Removing markup from previous turn message: {game.turn_message_id}")
            self._view.remove_markup(
                chat_id=chat_id,
                message_id=game.turn_message_id,
            )
            game.turn_message_id = None # پاک کردن شناسه قدیمی

        # ارسال پیام جدید نوبت و ذخیره شناسه آن
        # این شناسه برای حذف دکمه‌ها در حرکت بعدی استفاده خواهد شد.
        money = player.wallet.value()
        message_id = self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=player,
            money=money,
        )

        if message_id:
            print(f"DEBUG: Turn message sent. New turn_message_id: {message_id}")
            game.turn_message_id = message_id
            game.last_turn_time = datetime.datetime.now()
        else:
            print(f"WARNING: Failed to send turn message or get its ID.")

    
    def _process_playing(self, chat_id: ChatId, game: Game) -> None:
        """
        بازیکن بعدی که باید بازی کند را پیدا کرده و پیام نوبت را برای او ارسال می‌کند.
        این متد دیگر به صورت خودکار برای بازیکنان بازی نمی‌کند.
        """
        print(f"DEBUG: Entering _process_playing for game {game.id}")

        # بررسی سریع: آیا همه بازیکنان فعال، بازی کرده‌اند؟
        round_over, all_in_showdown = self._is_round_finished(game)
        if round_over:
            print(f"DEBUG: Round is finished. Moving to next street.")
            if all_in_showdown:
                self._fast_forward_to_finish(game, chat_id)
            else:
                self._go_to_next_street(game, chat_id)
            return

        # پیدا کردن بازیکن بعدی که نوبتش است
        num_players = len(game.players)
        for i in range(num_players):
            # از بازیکن فعلی شروع کن و در دایره بچرخ
            player_index = (game.current_player_index + i) % num_players
            player = game.players[player_index]

            # این بازیکن باید فعال باشد و در این دور هنوز بازی نکرده باشد
            if player.state == PlayerState.ACTIVE and not player.has_acted:
                print(f"DEBUG: Found next player: {player.mention_markdown} at index {player_index}")
                game.current_player_index = player_index

                # فقط پیام نوبت را ارسال کن و تمام!
                self._send_turn_message(game, player, chat_id)
                return # از متد خارج شو و منتظر حرکت بازیکن بمان

        # اگر بعد از گشتن تمام بازیکنان، کسی برای بازی پیدا نشد،
        # یعنی دور تمام شده است (این حالت نباید زیاد پیش بیاید چون در بالا چک شد)
        print("DEBUG: No player found to act, re-evaluating round finish.")
        self._go_to_next_street(game, chat_id)

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

    def _go_to_next_street(self, game: Game, chat_id: ChatId) -> None:
        """
        بازی را به مرحله بعدی (Street) می‌برد یا در صورت لزوم به پایان می‌رساند.
        از متن‌های فارسی و جذاب برای اعلام وضعیت استفاده می‌کند.
        """
        print(f"Game {game.id}: Moving to the next street from {game.state.name}")

        # ۱. جمع‌آوری شرط‌های این دور و واریز به پات اصلی
        self._round_rate.to_pot(game, chat_id)

        # ۲. ریست کردن وضعیت بازیکنان برای دور جدید شرط‌بندی
        game.max_round_rate = 0
        game.trading_end_user_id = 0
        for p in game.players:
            p.round_rate = 0
            p.has_acted = False

        # ۳. بررسی سریع برای پایان بازی (اگر فقط یک نفر باقی مانده)
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if len(active_players) < 2:
            print(f"Game {game.id}: Not enough active players to continue. Finishing game.")
            self._finish(game, chat_id)
            return

        # ۴. پیشروی به مرحله بعدی بازی (Street)
        street_name_persian = ""
        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            self.add_cards_to_table(3, game, chat_id)
            street_name_persian = "فلاپ (Flop)"
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            self.add_cards_to_table(1, game, chat_id)
            street_name_persian = "تِرن (Turn)"
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            self.add_cards_to_table(1, game, chat_id)
            street_name_persian = "ریوِر (River)"
        elif game.state == GameState.ROUND_RIVER:
            game.state = GameState.FINISHED
        
        # ۵. نمایش میز و شروع دور جدید شرط‌بندی یا پایان بازی
        if game.state != GameState.FINISHED:
            # ساختن کپشن جذاب برای عکس میز
            caption = (
                f"🔥 **مرحله {street_name_persian} رو شد!** 🔥\n\n"
                f"💰 **پات به `{game.pot}$` رسید!**\n"
                f"دور جدید شرط‌بندی شروع می‌شود..."
            )

            # ارسال عکس میز به گروه
            msg = self._view.send_desk_cards_img(
                chat_id=chat_id,
                cards=game.cards_table,
                caption=caption
            )
            if msg:
                game.message_ids_to_delete.append(msg.message_id)

            # تعیین نفر شروع‌کننده برای این دور
            game.current_player_index = self._starting_player_index(game, game.state)
            self._process_playing(chat_id=chat_id, game=game)
        else:
            # اگر تمام کارت‌ها رو شده، بازی به مرحله حساس پایانی (Showdown) می‌رسد
            self._finish(game, chat_id)
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

    def fold(self, update: Update, context: CallbackContext) -> None:
        """Handles a player's FOLD action."""
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        chat_id = update.effective_chat.id

        if not player:
            return

        try:
            player.state = PlayerState.FOLDED
            self._view.send_message(
                chat_id=chat_id,
                text=f"😑 {player.mention_markdown} از ادامه بازی انصراف داد.",
                parse_mode="Markdown"
            )
            
            # بعد از حرکت، نوبت را به نفر بعدی بده
            self._process_playing(chat_id=chat_id, game=game) # <--- این خط را اضافه کنید

        except UserException as e:
            query = update.callback_query
            if query:
                query.answer(text=f"خطا: {e}", show_alert=True)


    def call_check(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        chat_id = update.effective_chat.id
        
        self._round_rate.call_check(game, player)
        self._next_player_or_finish_rate(game, chat_id)

    def fold(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        chat_id = update.effective_chat.id
        
        self._round_rate.fold(player)
        self._next_player_or_finish_rate(game, chat_id)

    def all_in(self, update: Update, context: CallbackContext) -> None:
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        chat_id = update.effective_chat.id

        self._round_rate.all_in(game, player)
        self._next_player_or_finish_rate(game, chat_id)
        
    def raise_rate_bet(self, update: Update, context: CallbackContext, amount: Money) -> None:
        game = self._game_from_context(context)
        player = self._current_turn_player(game)
        chat_id = update.effective_chat.id
        
        self._round_rate.raise_bet(game, player, amount)
        self._next_player_or_finish_rate(game, chat_id)

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
        """
        تمام شرط‌های دور فعلی را به پات اصلی منتقل کرده و مقادیر را برای دور بعد ریست می‌کند.
        این متد در پایان هر مرحله شرط‌بندی (pre-flop, flop, turn, river) فراخوانی می‌شود.
        """
        pot_increase = 0
        for p in game.players:
            # total_bet قبلاً در حین call/raise آپدیت شده، اینجا فقط round_rate را به pot منتقل می‌کنیم.
            pot_increase += p.round_rate
            p.round_rate = 0
            # همه بازیکنان برای دور بعدی شرط‌بندی نیاز به تصمیم‌گیری مجدد دارند.
            if p.state == PlayerState.ACTIVE:
                 p.has_acted = False
        
        game.pot += pot_increase
        game.max_round_rate = 0
        game.last_raise = 0  # مقدار آخرین رِیز برای دور جدید صفر می‌شود.
        
        # فقط اگر پات افزایش یافته، پیام ارسال کن.
        if pot_increase > 0:
            print(f"INFO: Moved {pot_increase}$ to pot. New pot: {game.pot}$")
        
        # نمایش کارت‌های میز و پات جدید
        self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=f"💰 **پات فعلی:** `{game.pot}$`",
        )

    def call_check(self, game: Game, player: Player) -> None:
        """منطق اجرای حرکت Call یا Check."""
        amount_to_add = game.max_round_rate - player.round_rate
        
        # اگر بازیکن پول کافی برای کال کردن ندارد، به صورت خودکار آل-این می‌شود.
        if amount_to_add > player.wallet.value():
            print(f"INFO: Player {player.mention_markdown} doesn't have enough for full call, going all-in.")
            self.all_in(game, player)
            return

        # اگر مبلغی برای اضافه کردن وجود دارد (یعنی حرکت Call است).
        if amount_to_add > 0:
            player.wallet.dec(amount_to_add)
            player.round_rate += amount_to_add
            player.total_bet += amount_to_add
            print(f"DEBUG: Player {player.mention_markdown} calls for {amount_to_add}$.")

        # اگر بازیکن تمام موجودی خود را شرط بسته باشد، وضعیتش به ALL_IN تغییر می‌کند.
        if player.wallet.value() == 0 and player.state != PlayerState.FOLD:
            player.state = PlayerState.ALL_IN
            print(f"DEBUG: Player {player.mention_markdown} is now all-in.")

        player.has_acted = True
        
    def fold(self, player: Player) -> None:
        """منطق مربوط به Fold."""
        player.state = PlayerState.FOLD
        player.has_acted = True
        print(f"DEBUG: Player {player.mention_markdown} folds.")

    def all_in(self, game: Game, player: Player) -> Money:
        """منطق مربوط به All-in."""
        amount_to_add = player.wallet.value()
        player.wallet.dec(amount_to_add)
        player.round_rate += amount_to_add
        player.total_bet += amount_to_add
        
        player.state = PlayerState.ALL_IN
        player.has_acted = True
        
        # اگر بازیکن با آل-این خود، حداکثر شرط را بالا برد، بقیه باید دوباره بازی کنند.
        if player.round_rate > game.max_round_rate:
            game.last_raise = player.round_rate - game.max_round_rate
            game.max_round_rate = player.round_rate
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != player.user_id:
                    p.has_acted = False
        
        print(f"DEBUG: Player {player.mention_markdown} goes all-in for {amount_to_add}$.")
        return player.round_rate

    # نام متد از raise_rate_bet به raise_bet تغییر کرد
    def raise_bet(self, game: Game, player: Player, raise_amount: int) -> Money:
        """
        منطق اجرای حرکت Raise یا Bet.
        نام این متد برای هماهنگی با کنترلر اصلاح شد.
        """
        call_amount = game.max_round_rate - player.round_rate
        total_required = call_amount + raise_amount
        
        # حداقل مبلغ رِیز باید به اندازه آخرین رِیز یا بیگ بلایند باشد.
        min_raise = game.last_raise if game.last_raise > 0 else (2 * SMALL_BLIND)
        if raise_amount < min_raise:
             raise UserException(f"حداقل مبلغ رِیز باید {min_raise}$ باشد.")

        if total_required > player.wallet.value():
             raise UserException("موجودی شما برای این مقدار رِیز کافی نیست!")

        player.wallet.dec(total_required)
        player.round_rate += total_required
        player.total_bet += total_required
        
        game.last_raise = raise_amount # مقدار خود رِیز را ذخیره کن
        game.max_round_rate = player.round_rate # حداکثر شرط جدید
        player.has_acted = True
    
        # بعد از رِیز، بقیه بازیکنان فعال باید دوباره تصمیم بگیرند.
        for p in game.players_by(states=(PlayerState.ACTIVE,)):
            if p.user_id != player.user_id:
                p.has_acted = False

        print(f"DEBUG: Player {player.mention_markdown} raises by {raise_amount}$. New max rate: {game.max_round_rate}$")
        return player.round_rate
        
    # متد finish_rate و _hand_name_from_score بدون تغییر باقی می‌مانند، چون منطق درستی دارند.
    # ... (کد finish_rate و _hand_name_from_score شما در اینجا قرار می‌گیرد) ...
    def finish_rate(self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]) -> Dict[str, List[Tuple[Player, Money]]]:
        # این متد از کد شما بدون تغییر کپی می‌شود
        final_winnings: Dict[str, List[Tuple[Player, Money]]] = {}
        active_and_all_in_players = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(active_and_all_in_players) == 1:
            winner = active_and_all_in_players[0]
            winnings = game.pot
            winner.wallet.inc(winnings)
            final_winnings["Winner by Fold"] = [(winner, winnings)]
            print(f"DEBUG: Player {winner.mention_markdown} won {winnings}$ because all others folded.")
            return final_winnings
        total_bets = {p.user_id: p.total_bet for p in game.players if p.total_bet > 0}
        if not total_bets and game.pot > 0:
            eligible_players = active_and_all_in_players
            if not eligible_players: return {}
            share = game.pot // len(eligible_players)
            remainder = game.pot % len(eligible_players)
            for i, player in enumerate(eligible_players):
                payout = share + (1 if i < remainder else 0)
                if payout > 0:
                    player.wallet.inc(payout)
                    final_winnings["Split Pot"] = final_winnings.get("Split Pot", []) + [(player, payout)]
            return final_winnings
        showdown_players = active_and_all_in_players
        sorted_unique_bets = sorted(list(set(b for b in total_bets.values() if b > 0)))
        side_pots = []
        last_bet_level = 0
        for bet_level in sorted_unique_bets:
            pot_amount = 0
            for player_id, player_bet in total_bets.items():
                contribution = min(player_bet, bet_level) - last_bet_level
                if contribution > 0:
                    pot_amount += contribution
            eligible_players = [p for p in showdown_players if total_bets.get(p.user_id, 0) >= bet_level]
            if pot_amount > 0 and eligible_players:
                side_pots.append({"amount": pot_amount, "eligible_players": eligible_players})
            last_bet_level = bet_level
        sorted_scores = sorted(player_scores.keys(), reverse=True)
        for pot in side_pots:
            best_score_in_pot = -1
            winners_in_pot = []
            for score in sorted_scores:
                for player, hand_cards in player_scores[score]:
                    if player in pot["eligible_players"]:
                        if best_score_in_pot == -1:
                            best_score_in_pot = score
                        if score == best_score_in_pot:
                            winners_in_pot.append(player)
                if best_score_in_pot != -1:
                    break
            if not winners_in_pot:
                continue
            win_share = pot['amount'] // len(winners_in_pot)
            remainder = pot['amount'] % len(winners_in_pot)
            for i, winner in enumerate(winners_in_pot):
                payout = win_share + (1 if i < remainder else 0)
                if payout > 0:
                    winner.wallet.inc(payout)
                    hand_name = self._hand_name_from_score(best_score_in_pot)
                    if hand_name not in final_winnings:
                        final_winnings[hand_name] = []
                    found = False
                    for j, (p, m) in enumerate(final_winnings[hand_name]):
                        if p.user_id == winner.user_id:
                            final_winnings[hand_name][j] = (p, m + payout)
                            found = True
                            break
                    if not found:
                         final_winnings[hand_name].append((winner, payout))
        return final_winnings

    def _hand_name_from_score(self, score: int) -> str:
        base_rank = score // HAND_RANK
        try:
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"

class WalletManagerModel(Wallet):
    """
    این کلاس مسئولیت مدیریت موجودی (Wallet) هر بازیکن را با استفاده از Redis بر عهده دارد.
    تمام عملیات مالی بازیکن مانند افزایش/کاهش موجودی و پاداش روزانه در اینجا مدیریت می‌شود.
    """
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv: redis.Redis = kv
        # کلید برای ذخیره موجودی اصلی کاربر
        self._val_key = f"u_m:{user_id}"
        # کلید برای بررسی اینکه آیا کاربر پاداش روزانه را گرفته است یا خیر
        self._daily_bonus_key = f"u_db:{user_id}"
        
        # کلیدهای زیر برای سیستم تراکنش (hold/approve/cancel) هستند.
        # در منطق فعلی بازی استفاده نمی‌شوند اما برای آینده مفید هستند.
        self._trans_key = f"u_t:{user_id}"
        self._trans_list_key = f"u_tl:{user_id}"

    def value(self) -> Money:
        """موجودی فعلی بازیکن را برمی‌گرداند. اگر بازیکن وجود نداشته باشد، موجودی پیش‌فرض برای او ایجاد می‌کند."""
        val = self._kv.get(self._val_key)
        if val is None:
            # اگر کاربر برای اولین بار وارد می‌شود، موجودی پیش‌فرض به او اختصاص بده.
            self._kv.set(self._val_key, DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(val)

    def inc(self, amount: Money) -> Money:
        """موجودی بازیکن را به مقدار مشخصی افزایش می‌دهد."""
        if amount < 0:
            # جلوگیری از افزایش با مقدار منفی
            return self.dec(abs(amount))
        return self._kv.incrby(self._val_key, amount)

    def dec(self, amount: Money) -> Money:
        """
        موجودی بازیکن را به مقدار مشخصی کاهش می‌دهد.
        اگر موجودی کافی نباشد، خطای UserException ایجاد می‌کند.
        """
        if amount < 0:
            # جلوگیری از کاهش با مقدار منفی
            return self.inc(abs(amount))
            
        # بررسی اتمیک برای جلوگیری از منفی شدن موجودی (Race Condition)
        # این اسکریپت Lua تضمین می‌کند که کاهش فقط در صورت کافی بودن موجودی انجام شود.
        lua_script = """
        local current_val = redis.call('get', KEYS[1])
        if not current_val or tonumber(current_val) < tonumber(ARGV[1]) then
            return nil
        end
        return redis.call('decrby', KEYS[1], ARGV[1])
        """
        decr_script = self._kv.register_script(lua_script)
        result = decr_script(keys=[self._val_key], args=[amount])

        if result is None:
            raise UserException("موجودی شما کافی نیست.")
        
        return int(result)
    
    def has_daily_bonus(self) -> bool:
        """بررسی می‌کند که آیا بازیکن امروز پاداش روزانه خود را دریافت کرده است یا خیر."""
        return self._kv.exists(self._daily_bonus_key) > 0

    def add_daily(self, amount: Money) -> Money:
        """
        پاداش روزانه را به موجودی بازیکن اضافه می‌کند و یک تایمر تا پایان روز برای آن ثبت می‌کند.
        """
        if self.has_daily_bonus():
            raise UserException("شما قبلاً پاداش روزانه خود را دریافت کرده‌اید.")

        # محاسبه تعداد ثانیه‌های باقی‌مانده تا نیمه‌شب
        now = datetime.datetime.now()
        midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ttl = (midnight - now).seconds

        # ثبت کلید با زمان انقضا (Time To Live)
        self._kv.setex(self._daily_bonus_key, ttl, 1)

        return self.inc(amount)
    
    # --- متدهای مربوط به تراکنش که در کلاس انتزاعی تعریف شده بودند ---
    # این متدها برای سیستم‌های پیچیده‌تر که نیاز به نگه‌داشتن پول و تایید یا لغو آن دارند، مفید است.
    # در منطق فعلی بازی پوکر ما، از inc و dec مستقیم استفاده می‌کنیم که کارآمدتر است.

    def hold(self, game_id: str, amount: Money):
        """مبلغی را از حساب کاربر کم کرده و به صورت معلق (hold) نگه می‌دارد."""
        self.dec(amount) # ابتدا از حساب اصلی کم می‌شود
        self._kv.hset(self._trans_key, game_id, amount)
        self._kv.lpush(self._trans_list_key, game_id)

    def approve(self, game_id: str):
        """تراکنش معلق را تایید می‌کند (پول به مقصد رفته و نیازی به بازگشت نیست)."""
        # فقط اطلاعات تراکنش را پاک می‌کنیم، چون پول قبلاً از حساب اصلی کم شده است.
        self._kv.hdel(self._trans_key, game_id)
        self._kv.lrem(self._trans_list_key, 0, game_id)

    def cancel(self, game_id: str):
        """تراکنش معلق را لغو کرده و مبلغ را به حساب کاربر بازمی‌گرداند."""
        amount_to_return = self._kv.hget(self._trans_key, game_id)
        if amount_to_return:
            self.inc(int(amount_to_return)) # بازگرداندن پول به حساب اصلی
            self._kv.hdel(self._trans_key, game_id)
            self._kv.lrem(self._trans_list_key, 0, game_id)

