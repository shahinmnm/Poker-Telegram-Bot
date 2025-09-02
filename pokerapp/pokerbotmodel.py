#!/usr/bin/env python3

import datetime
import traceback
from threading import Timer
from typing import List, Tuple, Dict, Optional

import redis
from telegram import Message, ReplyKeyboardMarkup, Update, Bot, ParseMode
from telegram.ext import Handler, CallbackContext

from pokerapp.config import Config
from pokerapp.privatechatmodel import UserPrivateChatModel
from pokerapp.winnerdetermination import WinnerDetermination, HAND_NAMES_TRANSLATIONS, HandsOfPoker
from pokerapp.cards import Card, Cards
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    ChatId,
    UserId,
    MessageId,
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

# MAX_PLAYERS = 8 (Defined in entities)
# MIN_PLAYERS = 2 (Defined in entities)
# SMALL_BLIND = 5 (Defined in entities)
# DEFAULT_MONEY = 1000 (Defined in entities)
MAX_TIME_FOR_TURN = datetime.timedelta(minutes=2)
DESCRIPTION_FILE = "assets/description_bot.md"

class PokerBotModel:
    ACTIVE_GAME_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }

    def __init__(self, view: PokerBotViewer, bot: Bot, cfg: Config, kv: redis.Redis):
        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._cfg: Config = cfg
        self._kv = kv
        self._winner_determine: WinnerDetermination = WinnerDetermination()
        self._round_rate = RoundRateModel(view=self._view, kv=self._kv, model=self)
    @property
    def _min_players(self):
        return 1 if self._cfg.DEBUG else MIN_PLAYERS

    @staticmethod
    def _game_from_context(context: CallbackContext) -> Game:
        if KEY_CHAT_DATA_GAME not in context.chat_data:
            context.chat_data[KEY_CHAT_DATA_GAME] = Game()
        return context.chat_data[KEY_CHAT_DATA_GAME]

    @staticmethod
    def _current_turn_player(game: Game) -> Optional[Player]:
        if not game.players or game.current_player_index < 0:
            return None
        # Add boundary check to prevent IndexError
        if game.current_player_index >= len(game.players):
            return None
        return game.players[game.current_player_index]
    @staticmethod
    def _get_cards_markup(cards: Cards) -> ReplyKeyboardMarkup:
        """کیبورد مخصوص نمایش کارت‌های بازیکن و دکمه‌های کنترلی را می‌سازد."""
        # این دکمه‌ها برای مدیریت کیبورد توسط بازیکن استفاده می‌شوند
        hide_cards_button_text = "🙈 پنهان کردن کارت‌ها"
        show_table_button_text = "👁️ نمایش میز" # این دکمه را هم اضافه می‌کنیم
        return ReplyKeyboardMarkup(
            keyboard=[
                cards, # <-- ردیف اول: خود کارت‌ها
                [hide_cards_button_text, show_table_button_text]
            ],
            selective=True,  # <-- کیبورد فقط برای بازیکن مورد نظر نمایش داده می‌شود
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    def show_reopen_keyboard(self, chat_id: ChatId, player_mention: Mention) -> None:
        """کیبورد جایگزین را بعد از پنهان کردن کارت‌ها نمایش می‌دهد."""
        show_cards_button_text = "🃏 نمایش کارت‌ها"
        show_table_button_text = "👁️ نمایش میز"
        reopen_keyboard = ReplyKeyboardMarkup(
            keyboard=[[show_cards_button_text, show_table_button_text]],
            selective=True,
            resize_keyboard=True,
            one_time_keyboard=False
        )
        self.send_message(
            chat_id=chat_id,
            text=f"کارت‌های {player_mention} پنهان شد. برای مشاهده دوباره، از دکمه زیر استفاده کن.",
            reply_markup=reopen_keyboard,
        )

    def send_cards(
            self,
            chat_id: ChatId,
            cards: Cards,
            mention_markdown: Mention,
            ready_message_id: MessageId,
    ) -> Optional[MessageId]:
        """
        یک پیام در گروه با کیبورد حاوی کارت‌های بازیکن ارسال می‌کند و به پیام /ready ریپلای می‌زند.
        """
        markup = self._get_cards_markup(cards)
        try:
            # اینجا ما به جای محتوای کارت‌ها، یک متن عمومی می‌فرستیم
            # و خود کارت‌ها را در کیبورد ReplyKeyboardMarkup قرار می‌دهیم.
            message = self._bot.send_message(
                chat_id=chat_id,
                text="کارت‌های شما " + mention_markdown,
                reply_markup=markup,
                reply_to_message_id=ready_message_id,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
            )
            if isinstance(message, Message):
                return message.message_id
        except Exception as e:
            # اگر ریپلای شکست خورد (پیام /ready حذف شده)، بدون ریپلای تلاش می‌کنیم
            if 'message to be replied not found' in str(e).lower():
                print(f"INFO: ready_message_id {ready_message_id} not found. Sending cards without reply.")
                try:
                    message = self._bot.send_message(
                        chat_id=chat_id,
                        text="کارت‌های شما " + mention_markdown,
                        reply_markup=markup,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_notification=True,
                    )
                    if isinstance(message, Message):
                        return message.message_id
                except Exception as inner_e:
                     print(f"Error sending cards (second attempt): {inner_e}")
            else:
                 print(f"Error sending cards: {e}")
        return None
    def hide_cards(self, update: Update, context: CallbackContext) -> None:
        """
        کیبورد کارتی را پنهان کرده و کیبورد "نمایش مجدد" را نشان می‌دهد.
        """
        chat_id = update.effective_chat.id
        user = update.effective_user
        self._view.show_reopen_keyboard(chat_id, user.mention_markdown())
        # پیام "کارت‌ها پنهان شد" را پس از چند ثانیه حذف می‌کنیم تا چت شلوغ نشود.
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=5)


    def send_cards_to_user(self, update: Update, context: CallbackContext) -> None:
        """
        کارت‌های بازیکن را با کیبورد مخصوص در گروه دوباره ارسال می‌کند.
        این متد زمانی فراخوانی می‌شود که بازیکن دکمه "نمایش کارت‌ها" را می‌زند.
        """
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # پیدا کردن بازیکن در لیست بازیکنان بازی فعلی
        current_player = None
        for p in game.players:
            if p.user_id == user_id:
                current_player = p
                break
        
        if not current_player or not current_player.cards:
            self._view.send_message(chat_id, "شما در بازی فعلی حضور ندارید یا کارتی ندارید.")
            return

        # ارسال پیام با کیبورد کارتی
        # اینجا دیگر نیازی به ریپلای نیست.
        cards_message_id = self._view.send_cards(
            chat_id=chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=None, # <-- چون این یک نمایش مجدد است، ریپلای نمی‌زنیم.
        )
        if cards_message_id:
            game.message_ids_to_delete.append(cards_message_id)
        
        # حذف پیام "/نمایش کارت‌ها" که بازیکن فرستاده
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)


    def show_table(self, update: Update, context: CallbackContext):
        """کارت‌های روی میز را به درخواست بازیکن نمایش می‌دهد."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        if game.state in self.ACTIVE_GAME_STATES:
            self.add_cards_to_table(0, game, chat_id) # فراخوانی با count=0 فقط میز را نمایش می‌دهد
        else:
            self._view.send_message(chat_id, "هنوز بازی شروع نشده است.")
        self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)

    def ready(self, update: Update, context: CallbackContext) -> None:
        """بازیکن برای شروع بازی اعلام آمادگی می‌کند."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id
        user = update.effective_message.from_user

        if game.state != GameState.INITIAL:
            self._view.send_message_reply(chat_id, update.message.message_id, "⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return

        if len(game.players) >= MAX_PLAYERS:
            self._view.send_message_reply(chat_id, update.message.message_id, "🚪 اتاق پر است!")
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < SMALL_BLIND * 2:
            self._view.send_message_reply(chat_id, update.message.message_id, f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).")
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.effective_message.message_id, # <-- کد صحیح
            )
            game.ready_users.add(user.id)
            game.players.append(player)

        ready_list = "\n".join([f"{i+1}. {p.mention_markdown} 🟢" for i, p in enumerate(game.players)])
        text = (
            f"👥 *لیست بازیکنان آماده*\n\n{ready_list}\n\n"
            f"📊 {len(game.players)}/{MAX_PLAYERS} بازیکن آماده\n\n"
            f"🚀 برای شروع بازی /start را بزنید یا منتظر بمانید."
        )

        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)

        if game.ready_message_main_id:
            try:
                self._bot.edit_message_text(chat_id=chat_id, message_id=game.ready_message_main_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception: # اگر ویرایش نشد، یک پیام جدید بفرست
                msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
                if msg: game.ready_message_main_id = msg
        else:
            msg = self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
            if msg: game.ready_message_main_id = msg

        # بررسی برای شروع خودکار
        if len(game.players) >= self._min_players and (len(game.players) == self._bot.get_chat_member_count(chat_id) - 1 or self._cfg.DEBUG):
            self._start_game(context, game, chat_id)

    def start(self, update: Update, context: CallbackContext) -> None:
        """بازی را به صورت دستی شروع می‌کند."""
        game = self._game_from_context(context)
        chat_id = update.effective_chat.id

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            self._view.send_message(chat_id, "🎮 یک بازی در حال حاضر در جریان است.")
            return

        if game.state == GameState.FINISHED:
            game.reset()
            # بازیکنان قبلی را برای دور جدید نگه دار
            old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
            # Re-add players logic would go here if needed.
            # For now, just resetting allows new players to join.

        if len(game.players) >= self._min_players:
            self._start_game(context, game, chat_id)
        else:
            self._view.send_message(chat_id, f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).")

    def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""
        if game.ready_message_main_id:
            self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None
    
        # Ensure dealer_index is initialized before use
        if not hasattr(game, 'dealer_index'):
             game.dealer_index = -1
        game.dealer_index = (game.dealer_index + 1) % len(game.players)
    
        self._view.send_message(chat_id, '🚀 !بازی شروع شد!')
    
        game.state = GameState.ROUND_PRE_FLOP
        self._divide_cards(game, chat_id)
    
        # این متد به تنهایی تمام کارهای لازم برای شروع راند را انجام می‌دهد.
        # از جمله تعیین بلایندها، تعیین نوبت اول و ارسال پیام نوبت.
        self._round_rate.set_blinds(game, chat_id)
    
        # نیازی به هیچ کد دیگری در اینجا نیست.
        # کدهای اضافی حذف شدند.
        
        # ذخیره بازیکنان برای دست بعدی (این خط می‌تواند بماند)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]


    def _divide_cards(self, game: Game, chat_id: ChatId):
        """
        کارت‌ها را بین بازیکنان پخش می‌کند:
        ۱. کارت‌ها را در PV بازیکن ارسال می‌کند.
        ۲. یک پیام در گروه با کیبورد حاوی کارت‌های بازیکن ارسال می‌کند.
        """
        for player in game.players:
            if len(game.remain_cards) < 2:
                self._view.send_message(chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود.")
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

            # --- شروع بلوک اصلاح شده ---

            # ۱. ارسال کارت‌ها به چت خصوصی (برای سابقه و دسترسی آسان)
            try:
                self._view.send_desk_cards_img(
                    chat_id=player.user_id,
                    cards=cards,
                    caption="🃏 کارت‌های شما برای این دست."
                )
            except Exception as e:
                print(f"WARNING: Could not send cards to private chat for user {player.user_id}. Error: {e}")
                self._view.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {player.mention_markdown}، نتوانستم کارت‌ها را در PV ارسال کنم. لطفاً ربات را استارت کن (/start).",
                    parse_mode="Markdown"
                )

            # ۲. ارسال پیام با کیبورد کارتی در گروه
            # این پیام برای دسترسی سریع بازیکن به کارت‌هایش است.
            cards_message_id = self._view.send_cards(
                chat_id=chat_id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )

            # این پیام موقتی است و در آخر دست پاک خواهد شد.
            if cards_message_id:
                game.message_ids_to_delete.append(cards_message_id)
            
            # --- پایان بلوک اصلاح شده ---

    def _process_playing(self, chat_id: ChatId, game: Game):
        """
        حلقه اصلی بازی: وضعیت را چک می‌کند، اگر دور تمام شده به مرحله بعد می‌رود،
        در غیر این صورت نوبت را به بازیکن بعدی می‌دهد.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            self._go_to_next_street(game, chat_id)
            return

        active_players = game.players_by(states=(PlayerState.ACTIVE,))

        # The round ends if no active players are left, or if all active players have acted
        # and their current round bets are equal.
        all_acted = all(p.has_acted for p in active_players)
        rates_equal = len(set(p.round_rate for p in active_players)) <= 1

        # Exception for Big Blind pre-flop: they can still act if no one raised.
        is_preflop_bb_option = (game.state == GameState.ROUND_PRE_FLOP and
                                game.max_round_rate == SMALL_BLIND * 2 and
                                not all_acted)

        if not active_players or (all_acted and rates_equal and not is_preflop_bb_option):
            self._go_to_next_street(game, chat_id)
            return

        player = self._current_turn_player(game)
        if player and player.state == PlayerState.ACTIVE:
            # FIX 1 (PART 2): Call _send_turn_message without the 'money' argument.
            self._send_turn_message(game, player, chat_id)
        else:
            # If current player is not active, move to the next one.
            self._move_to_next_player_and_process(game, chat_id)

    # FIX 1 (PART 1): Remove the 'money' parameter. The function will fetch the latest wallet value itself.
    def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف در آینده ذخیره می‌کند."""
        if game.turn_message_id:
            self._view.remove_markup(chat_id, game.turn_message_id)

        # Fetch the most current wallet value right here, ensuring it's up-to-date.
        money = player.wallet.value()
        
        msg_id = self._view.send_turn_actions(chat_id, game, player, money)
        
        if msg_id:
            game.turn_message_id = msg_id
        game.last_turn_time = datetime.datetime.now()
        
    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        """از ایندکس مشخص شده، به دنبال بازیکن بعدی که FOLD یا ALL_IN نکرده می‌گردد."""
        num_players = len(game.players)
        for i in range(1, num_players + 1):
            next_index = (start_index + i) % num_players
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index
        return -1 # هیچ بازیکن فعالی یافت نشد

    def _move_to_next_player_and_process(self, game: Game, chat_id: ChatId):
        """
        ایندکس بازیکن را به نفر فعال بعدی منتقل کرده و حلقه بازی را ادامه می‌دهد.
        این متد، مشکل حلقه بی‌نهایت را حل می‌کند.
        """
        next_player_index = self._find_next_active_player_index(
            game, game.current_player_index
        )
        if next_player_index == -1: # اگر بازیکن فعال دیگری نمانده
            # مستقیم به مرحله بعد برو، چون شرط‌بندی تمام است
            self._go_to_next_street(game, chat_id)
        else:
            game.current_player_index = next_player_index
            self._process_playing(chat_id, game)
            
    def _go_to_next_street(self, game: Game, chat_id: ChatId) -> None:
        """بازی را به مرحله بعدی (Flop, Turn, River) یا به پایان (Finish) می‌برد."""
        self._round_rate.collect_bets_for_pot(game)

        # Reset has_acted for all players for the new betting round
        for p in game.players:
            # Don't reset for FOLD players, keep their state
            if p.state != PlayerState.FOLD:
                p.has_acted = False

        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            self.add_cards_to_table(3, game, chat_id, "فلاپ (Flop)")
            self._process_playing(chat_id, game)
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            self.add_cards_to_table(1, game, chat_id, "تِرن (Turn)")
            self._process_playing(chat_id, game)
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            self.add_cards_to_table(1, game, chat_id, "ریوِر (River)")
            self._process_playing(chat_id, game)
        elif game.state == GameState.ROUND_RIVER:
            # این حالت را به جای else صریحاً می‌نویسیم تا خواناتر باشد
            # بعد از پایان شرط‌بندی در River، باید برندگان را مشخص کنیم
            self._determine_winners(game, chat_id)
            return
    def _determine_all_scores(self, game: Game) -> List[Dict]:
        """
        برای تمام بازیکنان فعال، دست و امتیازشان را محاسبه کرده و لیستی از دیکشنری‌ها را برمی‌گرداند.
        این متد باید از نسخه بروز شده WinnerDetermination استفاده کند.
        """
        player_scores = []
        # بازیکنانی که فولد نکرده‌اند در تعیین نتیجه شرکت می‌کنند
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        
        if not contenders:
            return []

        for player in contenders:
            if not player.cards:
                continue
            
            # **نکته مهم**: متد get_hand_value در WinnerDetermination باید بروز شود تا سه مقدار برگرداند
            # score, best_hand, hand_type = self._winner_determine.get_hand_value(player.cards, game.cards_table)
            
            # پیاده‌سازی موقت تا زمان آپدیت winnerdetermination
            # در اینجا فرض می‌کنیم متد `get_hand_value_and_type` در کلاس `WinnerDetermination` وجود دارد
            try:
                score, best_hand, hand_type = self._winner_determine.get_hand_value_and_type(player.cards, game.cards_table)
            except AttributeError:
                # اگر `get_hand_value_and_type` هنوز پیاده سازی نشده است، این بخش اجرا می شود.
                # این یک fallback موقت است.
                print("WARNING: 'get_hand_value_and_type' not found in WinnerDetermination. Update winnerdetermination.py")
                score, best_hand = self._winner_determine.get_hand_value(player.cards, game.cards_table)
                # یک روش موقت برای حدس زدن نوع دست بر اساس امتیاز
                hand_type_value = score // (15**5)
                hand_type = HandsOfPoker(hand_type_value) if hand_type_value > 0 else HandsOfPoker.HIGH_CARD


            player_scores.append({
                "player": player,
                "score": score,
                "best_hand": best_hand,
                "hand_type": hand_type
            })
        return player_scores
    def _find_winners_from_scores(self, player_scores: List[Dict]) -> Tuple[List[Player], int]:
        """از لیست امتیازات، برندگان و بالاترین امتیاز را پیدا می‌کند."""
        if not player_scores:
            return [], 0
            
        highest_score = max(data['score'] for data in player_scores)
        winners = [data['player'] for data in player_scores if data['score'] == highest_score]
        return winners, highest_score
        
    def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, street_name: str):
        """Adds cards to the table and announces the new street."""
        if len(game.remain_cards) < count:
            self._view.send_message(chat_id, f"کارت کافی برای {street_name} نیست. بازی تمام می‌شود.")
            self._finish(game, chat_id)
            return
            
        new_cards = [game.remain_cards.pop() for _ in range(count)]
        game.cards_table.extend(new_cards)
        
        cards_str = " ".join(game.cards_table)
        self._view.send_message(chat_id, f"--- {street_name}: {cards_str} ---")

    def player_action_fold(self, update: Update, context: CallbackContext, game: Game) -> None:
        player = self._current_turn_player(game)
        if not player: return
        self._round_rate.player_action_fold(game, player, update.effective_chat.id)
        player.has_acted = True
        self._move_to_next_player_and_process(game, update.effective_chat.id)

    def player_action_call_check(self, update: Update, context: CallbackContext, game: Game) -> None:
        player = self._current_turn_player(game)
        if not player: return
        self._round_rate.player_action_call_check(game, player, update.effective_chat.id)
        player.has_acted = True
        self._move_to_next_player_and_process(game, update.effective_chat.id)

    def player_action_all_in(self, update: Update, context: CallbackContext, game: Game) -> None:
        player = self._current_turn_player(game)
        if not player: return
        self._round_rate.player_action_all_in(game, player, update.effective_chat.id)
        player.has_acted = True
        self._move_to_next_player_and_process(game, update.effective_chat.id)

    def player_action_raise_bet(self, update: Update, context: CallbackContext, game: Game, amount: int) -> None:
        player = self._current_turn_player(game)
        if not player: return
        try:
            self._round_rate.player_action_raise_bet(game, player, amount, update.effective_chat.id)
            player.has_acted = True
            # When someone raises, the action is on other players again.
            # Reset `has_acted` for all other active players.
            for p in game.players:
                if p.user_id != player.user_id and p.state == PlayerState.ACTIVE:
                    p.has_acted = False
            self._move_to_next_player_and_process(game, update.effective_chat.id)
        except UserException as e:
            # Answer callback query to show the error message to the user
            context.bot.answer_callback_query(callback_query_id=update.callback_query.id, text=str(e), show_alert=True)
            

    def _determine_winners(self, game: Game, chat_id: ChatId) -> None:
        """
        برندگان بازی را مشخص کرده و سپس متد _finish را برای اعلام نتایج فراخوانی می‌کند.
        """
        print("DEBUG: Determining winners...")
        
        # ۱. فقط بازیکنانی را که FOLD نکرده‌اند در نظر بگیر
        active_players = [p for p in game.players if p.state != PlayerState.FOLD]
    
        # اگر فقط یک بازیکن باقی مانده باشد، او برنده است (بدون نیاز به نمایش کارت)
        if len(active_players) == 1:
            winner_player = active_players[0]
            # یک ساختار داده سازگار با _finish ایجاد می‌کنیم
            # چون Showdown رخ نداده، نوع دست و کارت‌ها اهمیتی ندارند و می‌توانند خالی باشند.
            # امتیاز عددی (1) فقط برای این است که لیست خالی نباشد.
            # ساختار جدید: ((نوع دست, امتیاز عددی), بهترین کارت‌ها, بازیکن)
            winners_data = [((None, 1), [], winner_player)] 
            print(f"DEBUG: Only one player left. Winner is {winner_player.user_id}")
            self._finish(winners_data, game, chat_id)
            return
    
        # ۲. محاسبه امتیاز دست هر بازیکن فعال
        # لیست جدید ما تاپل‌های سه‌تایی نگهداری می‌کند: ((نوع دست, امتیاز عددی), بهترین کارت‌ها, بازیکن)
        player_scores_data: List[Tuple[Tuple[HandsOfPoker, Score], Tuple[Card, ...], Player]] = []
        for player in active_players:
            # get_hand_value حالا یک تاپل سه‌تایی برمی‌گرداند: (نوع دست، امتیاز عددی، بهترین کارت‌ها)
            hand_type, score, best_hand = self._winner_determine.get_hand_value(
                player_cards=player.cards, 
                table_cards=game.cards_table
            )
            # ما داده‌ها را در ساختار جدید بسته‌بندی می‌کنیم
            player_scores_data.append(((hand_type, score), best_hand, player))
            print(f"DEBUG: Player {player.user_id} has hand_type {hand_type.name} with score {score}")
    
        # ۳. مرتب‌سازی بازیکنان بر اساس امتیاز عددی (از بیشترین به کمترین)
        # امتیاز عددی در x[0][1] قرار دارد
        player_scores_data.sort(key=lambda x: x[0][1], reverse=True)
    
        # ۴. پیدا کردن برنده(ها) - ممکن است چند نفر امتیاز یکسان داشته باشند
        highest_score = player_scores_data[0][0][1]
        winners = [data for data in player_scores_data if data[0][1] == highest_score]
        
        print(f"DEBUG: Highest score is {highest_score}. Winners: {[w[2].user_id for w in winners]}")
    
        # ۵. فراخوانی متد _finish با داده‌های صحیح
        self._finish(winners, game, chat_id)



    def _finish(self, winners_data: List[Dict], game: Game, chat_id: ChatId):
        """
        بازی را تمام می‌کند، برنده را تعیین کرده و نتایج را با فرمت جدید و خلاقانه نمایش می‌دهد.
        """
        game.state = GameState.FINISHED
        self._view.send_message(chat_id, "<b>... Showdown ...</b>\nنمایش نهایی کارت‌ها:", parse_mode=ParseMode.HTML)
        
        # 1. دریافت امتیازات همه بازیکنان (contenders)
        player_scores_data = self._determine_all_scores(game)
        
        if not player_scores_data:
            self._view.send_message(chat_id, "خطایی در تعیین برنده رخ داد یا بازیکنی باقی نمانده. بازی ریست می‌شود.")
            game.reset() # ریست کردن وضعیت بازی
            # در اینجا می‌توانید منطق شروع دور جدید را اضافه کنید
            return

        # 2. پیدا کردن برندگان و محاسبه مبلغ برد
        winners, highest_score = self._find_winners_from_scores(player_scores_data)
        # TODO: در آینده منطق Side Pot اینجا اضافه شود. فعلا پات اصلی تقسیم می‌شود.
        win_amount = game.pot // len(winners) if winners else 0

        # 3. ساختن متن خروجی برای هر بازیکن
        # مرتب‌سازی بازیکنان بر اساس امتیاز از بیشترین به کمترین
        sorted_players_data = sorted(player_scores_data, key=lambda x: x['score'], reverse=True)

        player_results_parts = []
        for data in sorted_players_data:
            player: Player = data['player']
            hand_type: HandsOfPoker = data['hand_type']
            best_hand_cards: Tuple[Card, ...] = data['best_hand']
            
            # هایلایت کردن کارت‌های شخصی بازیکن که در دست نهایی او استفاده شده‌اند
            hand_str_parts = []
            for card in best_hand_cards:
                is_player_card = any(c.rank == card.rank and c.suit == card.suit for c in player.cards)
                if is_player_card:
                    hand_str_parts.append(f"<b>{str(card)}</b>") # Bold کردن کارت‌های شخصی
                else:
                    hand_str_parts.append(str(card))
            hand_str = " ".join(hand_str_parts)

            # دریافت اطلاعات دست (ایموجی و نام فارسی) از دیکشنری
            hand_info = HAND_NAMES_TRANSLATIONS.get(hand_type, {"emoji": "❔", "fa": "نامشخص"})
            # مثال: "🔗 پِر (7)"
            # برای سادگی، فعلا فقط نام دست را نمایش می‌دهیم. می‌توانید ارزش دست را هم اضافه کنید.
            hand_name_str = f"{hand_info['emoji']} {hand_info['fa']}"

            # تعیین وضعیت بازیکن (برنده یا بازنده)
            old_wallet = player.wallet.value()
            current_win = 0
            if player in winners:
                status_emoji = "🏆"
                player.wallet.inc(win_amount) # افزایش موجودی برنده
                current_win = win_amount
            else:
                status_emoji = "💔"
            
            new_wallet = player.wallet.value()
            
            # ساخت رشته نمایش تغییر موجودی
            wallet_change_str = f"موجودی: {old_wallet}$ ⟩⟩ <b>{new_wallet}$</b>"
            if current_win > 0:
                wallet_change_str += f" (+{current_win}$)"
            
            # کنار هم چیدن اطلاعات بازیکن در یک بلوک
            player_block = (
                f"{status_emoji} <b>{player.mention_markdown}</b>\n"
                f"   دست: {hand_name_str}\n"
                f"   کارت‌ها: {hand_str}\n"
                f"   {wallet_change_str}"
            )
            player_results_parts.append(player_block)

        # 4. ساخت پیام نهایی
        # بخش بازیکنان در یک تگ <pre>
        players_html = "<pre>" + "\n\n".join(player_results_parts) + "</pre>"
        
        # فوتر بازی در یک تگ <pre> جداگانه برای ایجاد فاصله بصری
        table_cards_str = " ".join(map(str, game.cards_table))
        footer_html = (
            f"<pre>"
            f"🏛️ <b>میز:</b> {table_cards_str if table_cards_str else 'کارت مشترکی رو نشد'}\n"
            f"🔥 <b>کارت سوخته:</b> 🂠\n"
            f"💰 <b>پات نهایی:</b> {game.pot}$"
            f"</pre>"
        )

        final_html_message = f"{players_html}\n{footer_html}"
        
        self._view.send_message(
            chat_id=chat_id,
            text=final_html_message,
            parse_mode=ParseMode.HTML
        )

        # 5. پاک کردن پیام‌های موقتی بازی (کارت‌ها، دکمه‌های نوبت و ...)
        self._cleanup_messages(game, chat_id)

        # 6. ریست کردن وضعیت بازی برای دور بعد
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players if p.wallet.value() > SMALL_BLIND * 2]
        game.reset()
        # در اینجا می‌توانید دکمه "شروع دور جدید" را ارسال کنید
        
    def _cleanup_messages(self, game: Game, chat_id: ChatId):
        """پیام‌های مربوط به دست تمام شده را پاک می‌کند."""
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
        for msg_id in game.message_ids_to_delete:
            self._view.remove_message(chat_id, msg_id)
        game.message_ids_to_delete.clear()

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            # Replacing underscore with space and title-casing the output
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"
class RoundRateModel:
    def __init__(self, view: PokerBotViewer, kv: redis.Redis, model: "PokerBotModel"):
        self._view = view
        self._kv = kv
        self._model = model # <<< نمونه model ذخیره شد

    # داخل کلاس RoundRateModel
    def set_blinds(self, game: Game, chat_id: ChatId) -> None:
        """
        بلایند کوچک و بزرگ را برای شروع دور جدید تعیین و از حساب بازیکنان کم می‌کند.
        این متد برای حالت دو نفره (Heads-up) نیز بهینه شده است.
        """
        num_players = len(game.players)
    
        if num_players < 2:
            # نباید این اتفاق بیفتد، اما برای اطمینان
            return 
    
        # --- بلوک اصلاح شده برای تعیین بلایندها ---
        if num_players == 2:
            # حالت دو نفره (Heads-up): دیلر اسمال بلایند است و اول بازی می‌کند.
            small_blind_index = game.dealer_index
            big_blind_index = (game.dealer_index + 1) % num_players
            first_action_index = small_blind_index # در pre-flop، اسمال بلایند اول حرکت می‌کند
        else:
            # حالت استاندارد برای بیش از دو بازیکن
            small_blind_index = (game.dealer_index + 1) % num_players
            big_blind_index = (game.dealer_index + 2) % num_players
            first_action_index = (big_blind_index + 1) % num_players
        # --- پایان بلوک اصلاح شده ---
    
        small_blind_player = game.players[small_blind_index]
        big_blind_player = game.players[big_blind_index]
        
        # اعمال بلایندها
        self._set_player_blind(game, small_blind_player, SMALL_BLIND, "کوچک", chat_id)
        self._set_player_blind(game, big_blind_player, SMALL_BLIND * 2, "بزرگ", chat_id)
    
        game.max_round_rate = SMALL_BLIND * 2
        
        # تعیین نوبت اولین بازیکن برای اقدام
        game.current_player_index = first_action_index
        # بازیکنی که دور شرط‌بندی به او ختم می‌شود، بیگ بلایند است
        game.trading_end_user_id = big_blind_player.user_id
        
        # ارسال پیام نوبت به بازیکن
        player_turn = game.players[game.current_player_index]
        self._view.send_turn_actions(
            chat_id=chat_id,
            game=game,
            player=player_turn,
            money=player_turn.wallet.value()
        )


    def _set_player_blind(self, game: Game, player: Player, amount: Money, blind_type: str, chat_id: ChatId):

        """یک بلایند مشخص را روی بازیکن اعمال می‌کند."""
        try:
            player.wallet.authorize(game_id=str(chat_id), amount=amount)
            player.round_rate += amount
            game.pot += amount
            self._view.send_message(
                chat_id,
                f"💸 {player.mention_markdown} بلایند {blind_type} به مبلغ {amount}$ را پرداخت کرد."
            )
        except UserException as e:
            # اگر پول کافی نبود، بازیکن آل-این می‌شود
            available_money = player.wallet.value()
            player.wallet.authorize(game_id=str(chat_id), amount=available_money)
            player.round_rate += available_money
            game.pot += available_money
            player.state = PlayerState.ALL_IN
            self._view.send_message(
                chat_id,
                f"⚠️ {player.mention_markdown} موجودی کافی برای بلایند نداشت و All-in شد ({available_money}$)."
            )

    def player_action_fold(self, game: Game, player: Player, chat_id: ChatId):
        player.state = PlayerState.FOLD
        self._view.send_message(chat_id, f"🏳️ {player.mention_markdown} فولد کرد.")
        # Player's money is already authorized. It will be handled at the end of the hand.

    def player_action_call_check(self, game: Game, player: Player, chat_id: ChatId):
        amount_to_call = game.max_round_rate - player.round_rate
        if amount_to_call > 0:
            # This is a Call
            actual_call = min(amount_to_call, player.wallet.value())
            player.wallet.authorize(game.id, actual_call)
            player.round_rate += actual_call
            player.total_bet += actual_call
            game.pot += actual_call
            self._view.send_message(chat_id, f"🎯 {player.mention_markdown} کال کرد ({actual_call}$).")
            if actual_call < amount_to_call:
                player.state = PlayerState.ALL_IN
                self._view.send_message(chat_id, f"🀄 {player.mention_markdown} با کال کردن آل-این شد.")
        else:
            # This is a Check
            self._view.send_message(chat_id, f"✋ {player.mention_markdown} چک کرد.")
    
    def player_action_all_in(self, game: Game, player: Player, chat_id: ChatId):
        all_in_amount = player.wallet.value()
        player.wallet.authorize(game.id, all_in_amount)
        
        # Add to pot and update player/game state
        game.pot += all_in_amount
        player.round_rate += all_in_amount
        player.total_bet += all_in_amount
        player.state = PlayerState.ALL_IN
        
        # Update max round rate if this all-in is a raise
        if player.round_rate > game.max_round_rate:
            game.max_round_rate = player.round_rate

        self._view.send_message(chat_id, f"🀄 {player.mention_markdown} آل-این کرد (مبلغ کل: {player.round_rate}$).")

    def player_action_raise_bet(self, game: Game, player: Player, amount: int, chat_id: ChatId):
        # amount is the total new bet amount (e.g., raise to 50)
        current_bet = player.round_rate
        raise_amount = amount - current_bet # The additional money needed

        if raise_amount <= 0:
            raise UserException("مقدار رِیز باید از شرط فعلی شما بیشتر باشد.")
        
        if amount < game.max_round_rate * 2 and game.max_round_rate > 0:
            raise UserException(f"حداقل رِیز باید دو برابر آخرین شرط باشد ({game.max_round_rate * 2}$).")

        player.wallet.authorize(game.id, raise_amount)
        
        # --- FIX 2: Correctly update the pot ---
        # Instead of adding the full `amount`, add only the `raise_amount`.
        game.pot += raise_amount
        # ----------------------------------------
        
        player.round_rate = amount
        player.total_bet += raise_amount
        game.max_round_rate = amount
        self._view.send_message(chat_id, f"💹 {player.mention_markdown} شرط را به {amount}$ افزایش داد.")

    def collect_bets_for_pot(self, game: Game):
        # This function resets the round-specific bets for the next street.
        # The money is already in the pot.
        for player in game.players:
            player.round_rate = 0
        game.max_round_rate = 0
class WalletManagerModel(Wallet):
    """
    این کلاس مسئولیت مدیریت موجودی (Wallet) هر بازیکن را با استفاده از Redis بر عهده دارد.
    این کلاس به صورت اتمی (atomic) کار می‌کند تا از مشکلات همزمانی (race condition) جلوگیری کند.
    """
    def __init__(self, user_id: UserId, kv: redis.Redis):
        self._user_id = user_id
        self._kv: redis.Redis = kv
        self._val_key = f"u_m:{user_id}"
        self._daily_bonus_key = f"u_db:{user_id}"
        self._authorized_money_key = f"u_am:{user_id}" # برای پول رزرو شده در بازی

        # اسکریپت Lua برای کاهش اتمی موجودی (جلوگیری از race condition)
        # این اسکریپت ابتدا مقدار فعلی را می‌گیرد، اگر کافی بود کم می‌کند و مقدار جدید را برمیگرداند
        # در غیر این صورت -1 را برمیگرداند.
        self._LUA_DECR_IF_GE = self._kv.register_script("""
            local current = tonumber(redis.call('GET', KEYS[1]))
            if current == nil then
                redis.call('SET', KEYS[1], ARGV[2])
                current = tonumber(ARGV[2])
            end
            local amount = tonumber(ARGV[1])
            if current >= amount then
                return redis.call('DECRBY', KEYS[1], amount)
            else
                return -1
            end
        """)

    def value(self) -> Money:
        """موجودی فعلی بازیکن را برمی‌گرداند. اگر بازیکن وجود نداشته باشد، با مقدار پیش‌فرض ایجاد می‌شود."""
        val = self._kv.get(self._val_key)
        if val is None:
            self._kv.set(self._val_key, DEFAULT_MONEY)
            return DEFAULT_MONEY
        return int(val)

    def inc(self, amount: Money = 0) -> Money:
        """موجودی بازیکن را به مقدار مشخص شده افزایش می‌دهد."""
        return self._kv.incrby(self._val_key, amount)

    def dec(self, amount: Money) -> Money:
        """
        موجودی بازیکن را به مقدار مشخص شده کاهش می‌دهد، تنها اگر موجودی کافی باشد.
        این عملیات به صورت اتمی با استفاده از اسکریپت Lua انجام می‌شود.
        """
        if amount < 0:
            raise ValueError("Amount to decrease cannot be negative.")
        if amount == 0:
            return self.value()

        result = self._LUA_DECR_IF_GE(keys=[self._val_key], args=[amount, DEFAULT_MONEY])
        if result == -1:
            raise UserException("موجودی شما کافی نیست.")
        return int(result)

    def has_daily_bonus(self) -> bool:
        """چک می‌کند آیا بازیکن پاداش روزانه خود را دریافت کرده است یا خیر."""
        return self._kv.exists(self._daily_bonus_key) > 0

    def add_daily(self, amount: Money) -> Money:
        """پاداش روزانه را به بازیکن می‌دهد و زمان آن را تا روز بعد ثبت می‌کند."""
        if self.has_daily_bonus():
            raise UserException("شما قبلاً پاداش روزانه خود را دریافت کرده‌اید.")

        now = datetime.datetime.now()
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        ttl = int((tomorrow - now).total_seconds())

        self._kv.setex(self._daily_bonus_key, ttl, "1")
        return self.inc(amount)

    # --- متدهای مربوط به تراکنش‌های بازی (برای تطابق با Wallet ABC) ---
    def authorize(self, game_id: str, amount: Money) -> None:
        """مبلغی از پول بازیکن را برای یک بازی خاص رزرو (dec) می‌کند."""
        # در این پیاده‌سازی، ما مستقیماً پول را کم می‌کنیم.
        # متد dec خودش در صورت کمبود موجودی، خطا می‌دهد.
        self.dec(amount)
        self._kv.hincrby(self._authorized_money_key, game_id, amount)

    def approve(self, game_id: str) -> None:
        """تراکنش موفق یک بازی را تایید می‌کند (پول خرج شده و نیاز به بازگشت نیست)."""
        # پول قبلاً در authorize/dec کم شده است، فقط مبلغ رزرو شده را پاک می‌کنیم.
        self._kv.hdel(self._authorized_money_key, game_id)

    def cancel(self, game_id: str) -> None:
        """تراکنش ناموفق را لغو و پول رزرو شده را به بازیکن برمی‌گرداند."""
        # مبلغی که برای این بازی رزرو شده بود را به کیف پول برمی‌گردانیم.
        # hget returns bytes, so convert to int. Default to 0 if key doesn't exist.
        amount_to_return_bytes = self._kv.hget(self._authorized_money_key, game_id)
        if amount_to_return_bytes:
            amount_to_return = int(amount_to_return_bytes)
            if amount_to_return > 0:
                self.inc(amount_to_return)
                self._kv.hdel(self._authorized_money_key, game_id)
