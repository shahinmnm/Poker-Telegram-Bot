#!/usr/bin/env python3

import datetime
import traceback
from threading import Timer
from typing import List, Tuple, Dict, Optional

import redis
from telegram import Message, ReplyKeyboardMarkup, Update, Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackContext, ContextTypes

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
from pokerapp.table_manager import TableManager

DICE_MULT = 10
DICE_DELAY_SEC = 5
BONUSES = (5, 20, 40, 80, 160, 320)
DICES = "⚀⚁⚂⚃⚄⚅"

# legacy keys kept for backward compatibility but unused
KEY_OLD_PLAYERS = "old_players"
KEY_CHAT_DATA_GAME = "game"

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

    def __init__(self, view: PokerBotViewer, bot: Bot, cfg: Config, kv: redis.Redis, table_manager: TableManager):
        self._view: PokerBotViewer = view
        self._bot: Bot = bot
        self._cfg: Config = cfg
        self._kv = kv
        self._table_manager = table_manager
        self._winner_determine: WinnerDetermination = WinnerDetermination()
        self._round_rate = RoundRateModel(view=self._view, kv=self._kv, model=self)
    @property
    def _min_players(self):
        return 1 if self._cfg.DEBUG else MIN_PLAYERS

    async def _get_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Game, ChatId]:
        """Fetch the Game instance for the current chat, caching it in ``chat_data``.

        If the game has already been stored in ``context.chat_data`` it will be
        reused. Otherwise it is loaded from ``TableManager`` and cached for
        subsequent calls.
        """
        chat_id = update.effective_chat.id
        game = context.chat_data.get(KEY_CHAT_DATA_GAME)
        if not game:
            game = await self._table_manager.get_game(chat_id)
            context.chat_data[KEY_CHAT_DATA_GAME] = game
        return game, chat_id

    async def _get_game_by_user(self, user_id: int) -> Tuple[Game, ChatId]:
        """Find the game and chat id for a given user."""
        try:
            return await self._table_manager.find_game_by_user(user_id)
        except LookupError as exc:
            raise UserException("بازی‌ای برای توقف یافت نشد.") from exc

    @staticmethod
    def _current_turn_player(game: Game) -> Optional[Player]:
        if game.current_player_index < 0:
            return None
        # Use seat-based lookup
        return game.get_player_by_seat(game.current_player_index)
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
    def _log_bet_change(player, amount, source):
        print(f"[DEBUG] {source}: {player.mention_markdown} bet +{amount}, total_bet={player.total_bet}, round_rate={player.round_rate}, pot={game.pot}")

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

    async def send_cards(
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
            message = await self._bot.send_message(
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
                    message = await self._bot.send_message(
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
    async def hide_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        کیبورد کارتی را پنهان کرده و کیبورد "نمایش مجدد" را نشان می‌دهد.
        """
        chat_id = update.effective_chat.id
        user = update.effective_user
        await self._view.show_reopen_keyboard(chat_id, user.mention_markdown())
        # پیام "کارت‌ها پنهان شد" را پس از چند ثانیه حذف می‌کنیم تا چت شلوغ نشود.
        await self._view.remove_message_delayed(chat_id, update.message.message_id, delay=5)


    async def send_cards_to_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        کارت‌های بازیکن را با کیبورد مخصوص در گروه دوباره ارسال می‌کند.
        این متد زمانی فراخوانی می‌شود که بازیکن دکمه "نمایش کارت‌ها" را می‌زند.
        """
        game, chat_id = await self._get_game(update, context)
        user_id = update.effective_user.id
        
        # پیدا کردن بازیکن در لیست بازیکنان بازی فعلی
        current_player = None
        for p in game.players:
            if p.user_id == user_id:
                current_player = p
                break
        
        if not current_player or not current_player.cards:
            await self._view.send_message(chat_id, "شما در بازی فعلی حضور ندارید یا کارتی ندارید.")
            return

        # ارسال پیام با کیبورد کارتی
        # اینجا دیگر نیازی به ریپلای نیست.
        cards_message_id = await self._view.send_cards(
            chat_id=chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            ready_message_id=None, # <-- چون این یک نمایش مجدد است، ریپلای نمی‌زنیم.
        )
        if cards_message_id:
            game.message_ids_to_delete.append(cards_message_id)
            await self._table_manager.save_game(chat_id, game)
        
        # حذف پیام "/نمایش کارت‌ها" که بازیکن فرستاده
        await self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)
        
    async def show_table(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """کارت‌های روی میز را به درخواست بازیکن با فرمت جدید نمایش می‌دهد."""
        game, chat_id = await self._get_game(update, context)

        # پیام درخواست بازیکن را حذف می‌کنیم تا چت تمیز بماند
        await self._view.remove_message_delayed(chat_id, update.message.message_id, delay=1)

        if game.state in self.ACTIVE_GAME_STATES and game.cards_table:
            # از متد اصلاح‌شده برای نمایش میز استفاده می‌کنیم
            # با count=0 و یک عنوان عمومی و زیبا
            await self.add_cards_to_table(0, game, chat_id, "🃏 کارت‌های روی میز")
            await self._table_manager.save_game(chat_id, game)
        else:
            msg_id = await self._view.send_message_return_id(chat_id, "هنوز بازی شروع نشده یا کارتی روی میز نیست.")
            if msg_id:
                await self._view.remove_message_delayed(chat_id, msg_id, 5)

    async def ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازیکن برای شروع بازی اعلام آمادگی می‌کند."""
        game, chat_id = await self._get_game(update, context)
        user = update.effective_message.from_user

        if game.state != GameState.INITIAL:
            await self._view.send_message_reply(chat_id, update.message.message_id, "⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return

        if game.seated_count() >= MAX_PLAYERS:
            await self._view.send_message_reply(chat_id, update.message.message_id, "🚪 اتاق پر است!")
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < SMALL_BLIND * 2:
            await self._view.send_message_reply(chat_id, update.message.message_id, f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).")
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=user.mention_markdown(),
                wallet=wallet,
                ready_message_id=update.effective_message.message_id, # <-- کد صحیح
                seat_index=None,
            )
            game.ready_users.add(user.id)
            seat_assigned = game.add_player(player)
            if seat_assigned == -1:
                await self._view.send_message_reply(chat_id, update.message.message_id, "🚪 اتاق پر است!")
                return

        ready_list = "\n".join([
            f"{idx+1}. (صندلی {idx+1}) {p.mention_markdown} 🟢"
            for idx, p in enumerate(game.seats) if p
        ])
        text = (
            f"👥 *لیست بازیکنان آماده*\n\n{ready_list}\n\n"
            f"📊 {game.seated_count()}/{MAX_PLAYERS} بازیکن آماده\n\n"
            f"🚀 برای شروع بازی /start را بزنید یا منتظر بمانید."
        )

        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)
        current_text = getattr(game, "ready_message_main_text", "")

        if game.ready_message_main_id:
            if text != current_text:
                try:
                    await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=game.ready_message_main_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                    game.ready_message_main_text = text
                except BadRequest as exc:
                    print(f"Error editing ready message: {exc}")
                    msg = await self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
                    if msg:
                        game.ready_message_main_id = msg
                        game.ready_message_main_text = text
                except Exception as exc:
                    print(f"Unexpected error editing ready message: {exc}")
                    msg = await self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
                    if msg:
                        game.ready_message_main_id = msg
                        game.ready_message_main_text = text
            # If text is the same, do nothing
        else:
            msg = await self._view.send_message_return_id(chat_id, text, reply_markup=keyboard)
            if msg:
                game.ready_message_main_id = msg
                game.ready_message_main_text = text

        # بررسی برای شروع خودکار
        if game.seated_count() >= self._min_players and (game.seated_count() == await self._bot.get_chat_member_count(chat_id) - 1 or self._cfg.DEBUG):
            await self._start_game(context, game, chat_id)

        await self._table_manager.save_game(chat_id, game)
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازی را به صورت دستی شروع می‌کند."""
        game, chat_id = await self._get_game(update, context)

        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            await self._view.send_message(chat_id, "🎮 یک بازی در حال حاضر در جریان است.")
            return

        if game.state == GameState.FINISHED:
            game.reset()
            # بازیکنان قبلی را برای دور جدید نگه دار
            old_players_ids = context.chat_data.get(KEY_OLD_PLAYERS, [])
            # Re-add players logic would go here if needed.
            # For now, just resetting allows new players to join.

        if game.seated_count() >= self._min_players:
            await self._start_game(context, game, chat_id)
        else:
            await self._view.send_message(chat_id, f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).")
        await self._table_manager.save_game(chat_id, game)

    async def stop(self, user_id: int) -> None:
        """Stop the current game for the chat where the user plays."""
        game, chat_id = await self._get_game_by_user(user_id)
        game.reset()
        await self._table_manager.save_game(chat_id, game)
        await self._view.send_message(chat_id, "🛑 بازی متوقف شد.")

    async def _start_game(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""
        if game.ready_message_main_id:
            await self._view.remove_message(chat_id, game.ready_message_main_id)
            game.ready_message_main_id = None
    
        # Ensure dealer_index is initialized before use
        if not hasattr(game, 'dealer_index'):
             game.dealer_index = -1
        game.dealer_index = (game.dealer_index + 1) % game.seated_count()
    
        await self._view.send_message(chat_id, '🚀 !بازی شروع شد!')
    
        game.state = GameState.ROUND_PRE_FLOP
        await self._divide_cards(game, chat_id)
    
        # این متد به تنهایی تمام کارهای لازم برای شروع راند را انجام می‌دهد.
        # از جمله تعیین بلایندها، تعیین نوبت اول و ارسال پیام نوبت.
        await self._round_rate.set_blinds(game, chat_id)
    
        # نیازی به هیچ کد دیگری در اینجا نیست.
        # کدهای اضافی حذف شدند.
        
        # ذخیره بازیکنان برای دست بعدی (این خط می‌تواند بماند)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]


    async def _divide_cards(self, game: Game, chat_id: ChatId):
        """
        کارت‌ها را بین بازیکنان پخش می‌کند:
        ۱. کارت‌ها را در PV بازیکن ارسال می‌کند.
        ۲. یک پیام در گروه با کیبورد حاوی کارت‌های بازیکن ارسال می‌کند.
        """
        for player in game.seated_players():
            if len(game.remain_cards) < 2:
                await self._view.send_message(chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود.")
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

            # --- شروع بلوک اصلاح شده ---

            # ۱. ارسال کارت‌ها به چت خصوصی (برای سابقه و دسترسی آسان)
            try:
                await self._view.send_desk_cards_img(
                    chat_id=player.user_id,
                    cards=cards,
                    caption="🃏 کارت‌های شما برای این دست."
                )
            except Exception as e:
                print(f"WARNING: Could not send cards to private chat for user {player.user_id}. Error: {e}")
                await self._view.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {player.mention_markdown}، نتوانستم کارت‌ها را در PV ارسال کنم. لطفاً ربات را استارت کن (/start).",
                    parse_mode="Markdown"
                )

            # ۲. ارسال پیام با کیبورد کارتی در گروه
            # این پیام برای دسترسی سریع بازیکن به کارت‌هایش است.
            cards_message_id = await self._view.send_cards(
                chat_id=chat_id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                ready_message_id=player.ready_message_id,
            )

            # این پیام موقتی است و در آخر دست پاک خواهد شد.
            if cards_message_id:
                game.message_ids_to_delete.append(cards_message_id)
            
    def _is_betting_round_over(self, game: Game) -> bool:
        """
        بررسی می‌کند که آیا دور شرط‌بندی فعلی به پایان رسیده است یا خیر.
        یک دور زمانی تمام می‌شود که:
        1. تمام بازیکنانی که فولد نکرده‌اند، حداقل یک بار حرکت کرده باشند.
        2. تمام بازیکنانی که فولد نکرده‌اند، مقدار یکسانی پول در این دور گذاشته باشند.
        """
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
    
        # اگر هیچ بازیکن فعالی وجود ندارد (مثلاً همه all-in یا فولد کرده‌اند)، دور تمام است.
        if not active_players:
            return True
    
        # شرط اول: آیا همه بازیکنان فعال حرکت کرده‌اند؟
        # فلگ `has_acted` باید در ابتدای هر street و بعد از هر raise ریست شود.
        if not all(p.has_acted for p in active_players):
            return False
    
        # شرط دوم: آیا همه بازیکنان فعال مقدار یکسانی شرط بسته‌اند؟
        # مقدار شرط اولین بازیکن فعال را به عنوان مرجع در نظر می‌گیریم.
        reference_rate = active_players[0].round_rate
        if not all(p.round_rate == reference_rate for p in active_players):
            return False
    
        # اگر هر دو شرط برقرار باشد، دور تمام شده است.
        return True


    def _determine_winners(self, game: Game, contenders: list[Player]):
        """
        مغز متفکر مالی ربات! (نسخه ۲.۰ - خود اصلاحگر)
        برندگان را با در نظر گرفتن Side Pot مشخص کرده و با استفاده از game.pot
        از صحت محاسبات اطمینان حاصل می‌کند.
        """
        if not contenders or game.pot == 0:
            return []

        # ۱. محاسبه قدرت دست هر بازیکن (بدون تغییر)
        contender_details = []
        for player in contenders:
            hand_type, score, best_hand_cards = self._winner_determine.get_hand_value(
                player.cards, game.cards_table
            )
            contender_details.append({
                "player": player,
                "total_bet": player.total_bet,
                "score": score,
                "hand_cards": best_hand_cards,
                "hand_type": hand_type,
            })

        # ۲. شناسایی لایه‌های شرط‌بندی (Tiers) (بدون تغییر)
        bet_tiers = sorted(list(set(p['total_bet'] for p in contender_details if p['total_bet'] > 0)))

        winners_by_pot = []
        last_bet_tier = 0
        calculated_pot_total = 0 # برای پیگیری مجموع پات محاسبه شده

        # ۳. ساختن پات‌ها به صورت لایه به لایه (منطق اصلی بدون تغییر)
        for tier in bet_tiers:
            tier_contribution = tier - last_bet_tier
            eligible_for_this_pot = [p for p in contender_details if p['total_bet'] >= tier]
            
            pot_size = tier_contribution * len(eligible_for_this_pot)
            calculated_pot_total += pot_size
            
            if pot_size > 0:
                best_score_in_pot = max(p['score'] for p in eligible_for_this_pot)
                
                pot_winners_info = [
                    {
                        "player": p['player'],
                        "hand_cards": p['hand_cards'],
                        "hand_type": p['hand_type'],
                    }
                    for p in eligible_for_this_pot if p['score'] == best_score_in_pot
                ]
                
                winners_by_pot.append({
                    "amount": pot_size,
                    "winners": pot_winners_info
                })

            last_bet_tier = tier
        
        # --- FIX: مرحله حیاتی تطبیق و اصلاح نهایی ---
        # اینجا جادو اتفاق می‌افتد: ما پات محاسبه‌شده را با پات واقعی مقایسه می‌کنیم.
        # اگر پولی (مثل بلایندها) جا مانده باشد، آن را به پات اصلی اضافه می‌کنیم.
        discrepancy = game.pot - calculated_pot_total
        if discrepancy > 0 and winners_by_pot:
            # پول گمشده را به اولین پات (پات اصلی) اضافه کن
            winners_by_pot[0]['amount'] += discrepancy
        elif discrepancy < 0:
            # این حالت نباید رخ دهد، اما برای اطمینان لاگ می‌گیریم
            print(f"[ERROR] Pot calculation mismatch! Game pot: {game.pot}, Calculated: {calculated_pot_total}")

        # --- FIX 2: ادغام پات‌های غیرضروری ---
        # اگر در نهایت فقط یک پات وجود داشت، اما به اشتباه به چند بخش تقسیم شده بود
        # (مثل سناریوی شما)، همه را در یک پات اصلی ادغام می‌کنیم.
        if len(bet_tiers) == 1 and len(winners_by_pot) > 1:
            print("[INFO] Merging unnecessary side pots into a single main pot.")
            main_pot = {"amount": game.pot, "winners": winners_by_pot[0]['winners']}
            return [main_pot]
            
        return winners_by_pot

    async def _process_playing(self, chat_id: ChatId, game: Game, context: CallbackContext) -> None:
        """
        مغز متفکر و کنترل‌کننده اصلی جریان بازی.
        این متد پس از هر حرکت بازیکن فراخوانی می‌شود تا تصمیم بگیرد:
        1. آیا دست تمام شده؟ (یک نفر باقی مانده)
        2. آیا دور شرط‌بندی تمام شده؟
        3. در غیر این صورت، نوبت را به بازیکن فعال بعدی بده.
        این متد جایگزین چرخه بازگشتی قبلی بین _process_playing و _move_to_next_player_and_process شده است.
        """
        # پاک کردن پیام نوبت قبلی برای تمیز نگه داشتن چت
        if game.turn_message_id:
            await self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None
    
        # شرط ۱: آیا فقط یک بازیکن (یا کمتر) در بازی باقی مانده؟
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            await self._go_to_next_street(game, chat_id, context)
            return
    
        # شرط ۲: آیا دور شرط‌بندی فعلی به پایان رسیده است؟
        if self._is_betting_round_over(game):
            await self._go_to_next_street(game, chat_id, context)
            return
    
        # شرط ۳: بازی ادامه دارد، نوبت را به بازیکن بعدی منتقل کن
        # از متدی که از قبل در RoundRateModel وجود داشت استفاده می‌کنیم
        # *** تنها خط اصلاح شده اینجاست ***
        next_player_index = self._round_rate._find_next_active_player_index(game, game.current_player_index)
    
        if next_player_index != -1:
            # ایندکس بازیکن فعلی را *قبل* از ارسال پیام نوبت آپدیت می‌کنیم
            game.current_player_index = next_player_index
            player = game.players[next_player_index]

            # ارسال پیام نوبت به بازیکن جدید
            await self._send_turn_message(game, player, chat_id)
        else:
            # اگر هیچ بازیکن فعالی برای حرکت بعدی وجود ندارد (مثلاً همه All-in هستند)
            # مستقیماً به مرحله بعدی برو
            await self._go_to_next_street(game, chat_id, context)

    # FIX 1 (PART 1): Remove the 'money' parameter. The function will fetch the latest wallet value itself.
    async def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف در آینده ذخیره می‌کند."""
        if game.turn_message_id:
            await self._view.remove_markup(chat_id, game.turn_message_id)

        # Fetch the most current wallet value right here, ensuring it's up-to-date.
        money = player.wallet.value()
        
        msg_id = await self._view.send_turn_actions(chat_id, game, player, money)
        
        if msg_id:
            game.turn_message_id = msg_id
        game.last_turn_time = datetime.datetime.now()
    # --- Player Action Handlers ---
    # این بخش تمام حرکات ممکن بازیکنان در نوبتشان را مدیریت می‌کند.
    
    async def player_action_fold(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازیکن فولد می‌کند، از دور شرط‌بندی کنار می‌رود و نوبت به نفر بعدی منتقل می‌شود."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        current_player.state = PlayerState.FOLD
        await self._view.send_message(chat_id, f"🏳️ {current_player.mention_markdown} فولد کرد.")
    
        # برای اطمینان از پاک شدن دکمه‌ها، مارک‌آپ را حذف می‌کنیم
        if game.turn_message_id:
            await self._view.remove_markup(chat_id, game.turn_message_id)

        await self._process_playing(chat_id, game, context)
        await self._table_manager.save_game(chat_id, game)
    
    async def player_action_call_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازیکن کال (پرداخت) یا چک (عبور) را انجام می‌دهد."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        call_amount = game.max_round_rate - current_player.round_rate
        current_player.has_acted = True
    
        try:
            if call_amount > 0:
                # منطق Call
                current_player.wallet.authorize(game.id, call_amount)
                current_player.round_rate += call_amount
                current_player.total_bet += call_amount
                game.pot += call_amount
                await self._view.send_message(chat_id, f"🎯 {current_player.mention_markdown} با {call_amount}$ کال کرد.")
            else:
                # منطق Check
                await self._view.send_message(chat_id, f"✋ {current_player.mention_markdown} چک کرد.")
        except UserException as e:
            await self._view.send_message(chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}")
            return  # اگر پول نداشت، از ادامه متد جلوگیری کن
    
        if game.turn_message_id:
            await self._view.remove_markup(chat_id, game.turn_message_id)

        await self._process_playing(chat_id, game, context)
        await self._table_manager.save_game(chat_id, game)
    
    async def player_action_raise_bet(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raise_amount: int) -> None:
        """بازیکن شرط را افزایش می‌دهد (Raise) یا برای اولین بار شرط می‌بندد (Bet)."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        call_amount = game.max_round_rate - current_player.round_rate
        total_amount_to_bet = call_amount + raise_amount
    
        try:
            current_player.wallet.authorize(game.id, total_amount_to_bet)
            current_player.round_rate += total_amount_to_bet
            current_player.total_bet += total_amount_to_bet
            game.pot += total_amount_to_bet
    
            # به‌روزرسانی حداکثر شرط و اعلام آن
            game.max_round_rate = current_player.round_rate
            action_text = "بِت" if call_amount == 0 else "رِیز"
            await self._view.send_message(
                chat_id,
                f"💹 {current_player.mention_markdown} {action_text} زد و شرط رو به {current_player.round_rate}$ رسوند.",
            )
    
            # --- بخش کلیدی منطق پوکر ---
            # وقتی کسی رِیز می‌کند، نوبت بازی باید یک دور کامل دیگر بچرخد
            game.trading_end_user_id = current_player.user_id
            current_player.has_acted = True
            # وضعیت بقیه بازیکنان فعال را برای بازی در دور جدید ریست می‌کنیم
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False
    
        except UserException as e:
            await self._view.send_message(chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}")
            return
    
        if game.turn_message_id:
            await self._view.remove_markup(chat_id, game.turn_message_id)

        await self._process_playing(chat_id, game, context)
        await self._table_manager.save_game(chat_id, game)
    
    async def player_action_all_in(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازیکن تمام موجودی خود را شرط می‌بندد (All-in)."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        all_in_amount = current_player.wallet.value()
    
        if all_in_amount <= 0:
            self._view.send_message(chat_id, f"👀 {current_player.mention_markdown} موجودی برای آل-این ندارد و چک می‌کند.")
            await self.player_action_call_check(update, context)  # این حرکت معادل چک است
            return
    
        current_player.wallet.authorize(game.id, all_in_amount)
        current_player.round_rate += all_in_amount
        current_player.total_bet += all_in_amount
        game.pot += all_in_amount
        current_player.state = PlayerState.ALL_IN
        current_player.has_acted = True
    
        self._view.send_message(chat_id, f"🀄 {current_player.mention_markdown} با {all_in_amount}$ آل‑این کرد!")

        if current_player.round_rate > game.max_round_rate:
            game.max_round_rate = current_player.round_rate
            # اگر آل-این باعث افزایش شرط شد، مانند رِیز عمل می‌کند
            game.trading_end_user_id = current_player.user_id
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False

        if game.turn_message_id:
            await self._view.remove_markup(chat_id, game.turn_message_id)

        await self._process_playing(chat_id, game, context)
        await self._table_manager.save_game(chat_id, game)

    # ---- Table management commands ---------------------------------

    async def create_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await self._table_manager.create_game(chat_id)
        self._view.send_message(chat_id, "بازی جدید ایجاد شد.")

    async def _go_to_next_street(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        بازی را به مرحله بعدی (street) می‌برد.
        این متد مسئولیت‌های زیر را بر عهده دارد:
        1. جمع‌آوری شرط‌های این دور و افزودن به پات اصلی.
        2. ریست کردن وضعیت‌های مربوط به دور (مثل has_acted و round_rate).
        3. تعیین اینکه آیا باید به مرحله بعد برویم یا بازی با showdown تمام می‌شود.
        4. پخش کردن کارت‌های جدید روی میز (فلاپ، ترن، ریور).
        5. پیدا کردن اولین بازیکن فعال برای شروع دور شرط‌بندی جدید.
        6. اگر فقط یک بازیکن باقی مانده باشد، او را برنده اعلام می‌کند.
        """
        # ابتدا، تمام پیام‌های نوبت قبلی را پاک می‌کنیم تا چت تمیز بماند
        if game.turn_message_id:
            await self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None
    
        # بررسی می‌کنیم چند بازیکن هنوز در بازی هستند (Active یا All-in)
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            # اگر فقط یک نفر باقی مانده، مستقیم به showdown می‌رویم تا برنده مشخص شود
              await self._showdown(game, chat_id, context)
              return
    
        # جمع‌آوری پول‌های شرط‌بندی شده در این دور و ریست کردن وضعیت بازیکنان
        self._round_rate.collect_bets_for_pot(game)
        for p in game.players:
            p.has_acted = False # <-- این خط برای دور بعدی حیاتی است
    
        # رفتن به مرحله بعدی بر اساس وضعیت فعلی بازی
        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            await self.add_cards_to_table(3, game, chat_id, "🃏 فلاپ (Flop)")
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            await self.add_cards_to_table(1, game, chat_id, "🃏 ترن (Turn)")
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            await self.add_cards_to_table(1, game, chat_id, "🃏 ریور (River)")
        elif game.state == GameState.ROUND_RIVER:
            # بعد از ریور، دور شرط‌بندی تمام شده و باید showdown انجام شود
              await self._showdown(game, chat_id, context)
              return # <-- مهم: بعد از فراخوانی showdown، ادامه نمی‌دهیم
    
        # اگر هنوز بازیکنی برای بازی وجود دارد، نوبت را به نفر اول می‌دهیم
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            # اگر هیچ بازیکن فعالی نمانده (همه All-in هستند)، مستقیم به مراحل بعدی می‌رویم
            # تا همه کارت‌ها رو شوند.
              await self._go_to_next_street(game, chat_id, context)
              return
    
        # پیدا کردن اولین بازیکن برای شروع دور جدید (معمولاً اولین فرد فعال بعد از دیلر)
        # توجه: شما باید متد _get_first_player_index را داشته باشید.
        # اگر ندارید، فعلاً از این پیاده‌سازی ساده استفاده کنید:
        try:
            # این متد باید ایندکس اولین بازیکن *فعال* بعد از دیلر را پیدا کند
            game.current_player_index = self._get_first_player_index(game)
        except AttributeError:
            # پیاده‌سازی موقت اگر متد بالا وجود ندارد
            print("WARNING: _get_first_player_index() not found. Using fallback logic.")
            first_player_index = -1
            start_index = (game.dealer_index + 1) % game.seated_count()
            for i in range(game.seated_count()):
                idx = (start_index + i) % game.seated_count()
                if game.players[idx].state == PlayerState.ACTIVE:
                    first_player_index = idx
                    break
            game.current_player_index = first_player_index
    
        # اگر بازیکنی برای بازی پیدا شد، حلقه بازی را مجدداً شروع می‌کنیم
        if game.current_player_index != -1:
            await self._process_playing(chat_id, game, context)
        else:
            # اگر به هر دلیلی بازیکنی پیدا نشد، به مرحله بعد می‌رویم
            await self._go_to_next_street(game, chat_id, context)

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
        
    async def add_cards_to_table(self, count: int, game: Game, chat_id: ChatId, street_name: str):
        """
        کارت‌های جدید را به میز اضافه کرده و تصویر میز را با فرمت جدید و زیبا ارسال می‌کند.
        اگر count=0 باشد، فقط کارت‌های فعلی را نمایش می‌دهد.
        """
        # مرحله ۱: اضافه کردن کارت‌های جدید در صورت نیاز
        if count > 0:
            for _ in range(count):
                if game.remain_cards:
                    game.cards_table.append(game.remain_cards.pop())

        # مرحله ۲: بررسی وجود کارت روی میز
        if not game.cards_table:
            # اگر کارتی روی میز نیست، به جای عکس، یک پیام متنی ساده می‌فرستیم.
            msg_id = await self._view.send_message_return_id(
                chat_id, "هنوز کارتی روی میز نیامده است."
            )
            if msg_id:
                game.message_ids_to_delete.append(msg_id)
                await self._view.remove_message_delayed(chat_id, msg_id, 5)
            return

        # مرحله ۳: ساخت رشته کارت‌ها با فرمت جدید (دو فاصله بین هر کارت)
        cards_str = "  ".join(game.cards_table)

        # مرحله ۴: ساخت کپشن دو خطی و زیبا
        caption = f"{street_name}\n{cards_str}"

        # مرحله ۵: ارسال تصویر میز با کپشن جدید
        msg = await self._view.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=caption,
        )

        # پیام تصویر میز را برای حذف در انتهای دست، ذخیره می‌کنیم
        if msg:
            game.message_ids_to_delete.append(msg.message_id)

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            # Replacing underscore with space and title-casing the output
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"
            
    def _clear_game_messages(self, game: Game, chat_id: ChatId) -> None:
        """
        تمام پیام‌های مربوط به این دست از بازی، از جمله پیام نوبت فعلی
        و سایر پیام‌های ثبت‌شده را پاک می‌کند تا چت برای نمایش نتایج تمیز شود.
        """
        print(f"DEBUG: Clearing game messages...")
    
        # ۱. پاک کردن پیام نوبت فعال (که دکمه‌ها را دارد)
        if game.turn_message_id:
            self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None # آن را نال می‌کنیم تا دوباره استفاده نشود
    
        # ۲. پاک کردن بقیه پیام‌های ذخیره شده در لیست
        # ما از یک کپی از لیست استفاده می‌کنیم تا حذف عناصر در حین پیمایش مشکلی ایجاد نکند
        for message_id in list(game.message_ids_to_delete):
            self._view.remove_message(chat_id, message_id)
        
        # ۳. بعد از اتمام کار، لیست را کاملاً خالی می‌کنیم
        game.message_ids_to_delete.clear()
        
    async def _showdown(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        فرآیند پایان دست را با استفاده از خروجی دقیق _determine_winners مدیریت می‌کند.
        """
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        if not contenders:
            # سناریوی نادر که همه قبل از showdown فولد کرده‌اند
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if len(active_players) == 1:
                winner = active_players[0]
                winner.wallet.inc(game.pot)
                await self._view.send_message(
                    chat_id,
                    f"🏆 تمام بازیکنان دیگر فولد کردند! {winner.mention_markdown} برنده {game.pot}$ شد."
                )
        else:
            # ۱. تعیین برندگان و تقسیم تمام پات‌ها (اصلی و فرعی)
            winners_by_pot = self._determine_winners(game, contenders)

            if winners_by_pot:
                # این حلقه روی تمام پات‌های ساخته شده (اصلی و فرعی) حرکت می‌کند
                for pot in winners_by_pot:
                    pot_amount = pot.get("amount", 0)
                    winners_info = pot.get("winners", [])
                    
                    if pot_amount > 0 and winners_info:
                        win_amount_per_player = pot_amount // len(winners_info)
                        for winner in winners_info:
                            player = winner["player"]
                            player.wallet.inc(win_amount_per_player)
            else:
                 await self._view.send_message(chat_id, "ℹ️ هیچ برنده‌ای در این دست مشخص نشد. مشکلی در منطق بازی رخ داده است.")


            # ۲. فراخوانی View برای نمایش نتایج
            # View باید آپدیت شود تا این ساختار داده جدید را به زیبایی نمایش دهد
            await self._view.send_showdown_results(chat_id, game, winners_by_pot)

        # ۳. پاکسازی و ریست کردن بازی برای دست بعدی (بدون تغییر)
        for msg_id in game.message_ids_to_delete:
            await self._view.remove_message(chat_id, msg_id)
        game.message_ids_to_delete.clear()

        if game.turn_message_id:
            await self._view.remove_message(chat_id, game.turn_message_id)
            game.turn_message_id = None

        remaining_players = [p for p in game.players if p.wallet.value() > 0]
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in remaining_players]

        game.reset()

        await self._view.send_new_hand_ready_message(chat_id)
        
    async def _end_hand(self, game: Game, chat_id: ChatId, context: CallbackContext) -> None:
        """
        یک دست از بازی را تمام کرده، پیام‌ها را پاکسازی کرده و برای دست بعدی آماده می‌شود.
        """
        # ۱. پاکسازی تمام پیام‌های موقت این دست (کارت‌های بازیکنان و ...)
        # این کار باعث می‌شود چت گروه شلوغ نشود
        for message_id in set(game.message_ids_to_delete): # از set استفاده می‌کنیم که پیام تکراری حذف نکنیم
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                # اگر پیام قبلاً حذف شده یا مشکلی پیش بیاید، خطا را فقط چاپ می‌کنیم
                print(f"INFO: Could not delete message {message_id} in chat {chat_id}. Reason: {e}")

        # پاک کردن آخرین پیام نوبت
        if game.turn_message_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=game.turn_message_id)
            except Exception as e:
                print(f"INFO: Could not delete turn message {game.turn_message_id}. Reason: {e}")

        # ۲. ذخیره بازیکنان برای دست بعدی
        # این باعث می‌شود در بازی بعدی، لازم نباشد همه دوباره /ready بزنند
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players if p.wallet.value() > 0]

        # ۳. ریست کردن کامل آبجکت بازی برای شروع یک دست جدید و تمیز
        # یک آبجکت جدید Game می‌سازیم تا هیچ داده‌ای از دست قبل باقی نماند
        context.chat_data[KEY_CHAT_DATA_GAME] = Game()

        # ۴. اعلام پایان دست و راهنمایی برای شروع دست بعدی
        keyboard = ReplyKeyboardMarkup([["/ready", "/start"]], resize_keyboard=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎉 دست تمام شد! برای شروع دست بعدی، /ready بزنید یا منتظر بمانید تا کسی /start کند.",
            reply_markup=keyboard
        )


    def _format_cards(self, cards: Cards) -> str:
        """
        کارت‌ها را با فرمت ثابت و زیبای Markdown برمی‌گرداند.
        برای هماهنگی با نسخه قدیمی، بین کارت‌ها دو اسپیس قرار می‌دهیم.
        """
        if not cards:
            return "??  ??"
        return "  ".join(str(card) for card in cards)



class RoundRateModel:
    def __init__(self, view: PokerBotViewer = None, kv: redis.Redis = None, model: "PokerBotModel" = None):
        self._view = view
        self._kv = kv
        self._model = model  # optional reference to model
        
    def _find_next_active_player_index(self, game: Game, start_index: int) -> int:
        num_players = game.seated_count()
        for i in range(1, num_players + 1):
            next_index = (start_index + i) % num_players
            if game.players[next_index].state == PlayerState.ACTIVE:
                return next_index
        return -1
        
    def _get_first_player_index(self, game: Game) -> int:
        return self._find_next_active_player_index(game, game.dealer_index)


    # داخل کلاس RoundRateModel
    async def set_blinds(self, game: Game, chat_id: ChatId) -> None:
        """
        Determine small/big blinds (using seat indices) and debit the players.
        Works for heads-up (2-player) and multiplayer by walking occupied seats.
        """
        num_players = game.seated_count()
        if num_players < 2:
            return

        # find next occupied seats for small and big blinds
        # heads-up special case: dealer is small blind
        if num_players == 2:
            small_blind_index = game.dealer_index
            big_blind_index = game.next_occupied_seat(small_blind_index)
            first_action_index = small_blind_index
        else:
            small_blind_index = game.next_occupied_seat(game.dealer_index)
            big_blind_index = game.next_occupied_seat(small_blind_index)
            first_action_index = game.next_occupied_seat(big_blind_index)

        # record in game
        game.small_blind_index = small_blind_index
        game.big_blind_index = big_blind_index

        small_blind_player = game.get_player_by_seat(small_blind_index)
        big_blind_player = game.get_player_by_seat(big_blind_index)

        if small_blind_player is None or big_blind_player is None:
            return

        # apply blinds
        await self._set_player_blind(game, small_blind_player, SMALL_BLIND, "کوچک", chat_id)
        await self._set_player_blind(game, big_blind_player, SMALL_BLIND * 2, "بزرگ", chat_id)

        game.max_round_rate = SMALL_BLIND * 2
        game.current_player_index = first_action_index
        game.trading_end_user_id = big_blind_player.user_id

        player_turn = game.get_player_by_seat(game.current_player_index)
        if player_turn:
            await self._view.send_turn_actions(
                chat_id=chat_id,
                game=game,
                player=player_turn,
                money=player_turn.wallet.value()
            )
    

    async def _set_player_blind(self, game: Game, player: Player, amount: Money, blind_type: str, chat_id: ChatId):
        try:
            player.wallet.authorize(game_id=str(chat_id), amount=amount)
            player.round_rate += amount
            player.total_bet += amount  # ← این خط اضافه شود
            game.pot += amount
            await self._view.send_message(
                chat_id,
                f"💸 {player.mention_markdown} بلایند {blind_type} به مبلغ {amount}$ را پرداخت کرد."
            )
        except UserException as e:
            available_money = player.wallet.value()
            player.wallet.authorize(game_id=str(chat_id), amount=available_money)
            player.round_rate += available_money
            player.total_bet += available_money  # ← این خط هم اضافه شود
            game.pot += available_money
            player.state = PlayerState.ALL_IN
            await self._view.send_message(
                chat_id,
                f"⚠️ {player.mention_markdown} موجودی کافی برای بلایند نداشت و All-in شد ({available_money}$)."
            )

    def collect_bets_for_pot(self, game: Game):
        # This function resets the round-specific bets for the next street.
        # The money is already in the pot.
        for player in game.seated_players():
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

        try:
            result = self._LUA_DECR_IF_GE(keys=[self._val_key], args=[amount, DEFAULT_MONEY])
        except (redis.exceptions.NoScriptError, ModuleNotFoundError):
            current = self._kv.get(self._val_key)
            if current is None:
                self._kv.set(self._val_key, DEFAULT_MONEY)
                current = DEFAULT_MONEY
            else:
                current = int(current)
            if current >= amount:
                self._kv.decrby(self._val_key, amount)
                result = current - amount
            else:
                result = -1
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
