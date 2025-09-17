#!/usr/bin/env python3

import asyncio
import datetime
from typing import List, Tuple, Dict, Optional

import redis
from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    Bot,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import CallbackContext, ContextTypes
from telegram.helpers import mention_markdown as format_mention_markdown

import logging

from pokerapp.config import Config
from pokerapp.winnerdetermination import (
    WinnerDetermination,
    HandsOfPoker,
)
from pokerapp.cards import Cards
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    ChatId,
    UserId,
    MessageId,
    UserException,
    Money,
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

logger = logging.getLogger(__name__)


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
        kv: redis.Redis,
        table_manager: TableManager,
    ):
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

    async def _get_game(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> Tuple[Game, ChatId]:
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

    def _get_first_player_index(self, game: Game) -> int:
        """Return index of the first active player after the dealer."""
        return self._round_rate._find_next_active_player_index(game, game.dealer_index)

    async def _send_join_prompt(self, game: Game, chat_id: ChatId) -> None:
        """Send initial join prompt with inline button if not already sent."""
        if game.state == GameState.INITIAL and not game.ready_message_main_id:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game")]]
            )
            msg_id = await self._view.send_message_return_id(
                chat_id, "برای نشستن سر میز دکمه را بزن", reply_markup=markup
            )
            if msg_id:
                game.ready_message_main_id = msg_id
                game.ready_message_main_text = "برای نشستن سر میز دکمه را بزن"
                await self._table_manager.save_game(chat_id, game)

    async def _auto_start_tick(self, context: CallbackContext) -> None:
        job = context.job
        chat_id = job.chat_id
        game = await self._table_manager.get_game(chat_id)
        remaining = context.chat_data.get("start_countdown", 0)
        if remaining <= 0:
            job.schedule_removal()
            context.chat_data.pop("start_countdown_job", None)
            await self._start_game(context, game, chat_id)
            return
        keyboard_buttons = [
            [InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game"),
             InlineKeyboardButton(text=f"شروع بازی ({remaining})", callback_data="start_game")]
        ]
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        await self._safe_edit_message_text(
            chat_id,
            game.ready_message_main_id,
            getattr(game, "ready_message_main_text", ""),
            reply_markup=keyboard,
        )
        context.chat_data["start_countdown"] = remaining - 1

    async def _schedule_auto_start(self, context: CallbackContext, game: Game, chat_id: ChatId) -> None:
        if context.chat_data.get("start_countdown_job"):
            return

        if context.job_queue is None:
            logger.warning("JobQueue not available; auto start disabled")
            return

        context.chat_data["start_countdown"] = 60
        job = context.job_queue.run_repeating(
            self._auto_start_tick, interval=1, chat_id=chat_id
        )
        context.chat_data["start_countdown_job"] = job

    def _cancel_auto_start(self, context: CallbackContext) -> None:
        job = context.chat_data.pop("start_countdown_job", None)
        if job:
            job.schedule_removal()
        context.chat_data.pop("start_countdown", None)

    async def send_cards(
        self,
        chat_id: ChatId,
        cards: Cards,
        mention_markdown: Mention,
        ready_message_id: MessageId,
    ) -> Optional[MessageId]:
        """Delegate to the viewer for sending player cards."""
        return await self._view.send_cards(
            chat_id=chat_id,
            cards=cards,
            mention_markdown=mention_markdown,
            ready_message_id=ready_message_id,
        )

    async def hide_cards(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """در نسخه جدید پیامی در چت خصوصی ارسال نمی‌کند."""
        chat_id = update.effective_chat.id
        if update.message:
            try:
                await update.message.delete()
            except Exception as e:
                logger.warning(
                    "Failed to delete hide message %s in chat %s: %s",
                    update.message.message_id,
                    chat_id,
                    e,
                )

    async def send_cards_to_user(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        کارت‌های بازیکن را در چت خصوصی دوباره ارسال می‌کند.
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
            mention = format_mention_markdown(
                update.effective_user.id, update.effective_user.full_name
            )
            await self._view.send_message(
                chat_id,
                f"{mention} در بازی فعلی حضور ندارد یا کارتی ندارد.",
                parse_mode="Markdown",
            )
            return

        stage = self._view._derive_stage_from_table(game.cards_table)
        await self._view.send_cards(
            chat_id=chat_id,
            cards=current_player.cards,
            mention_markdown=current_player.mention_markdown,
            table_cards=game.cards_table,
            hide_hand_text=True,
            stage=stage,
            reply_to_ready_message=False,
        )

    async def _safe_edit_message_text(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        parse_mode: str = ParseMode.MARKDOWN,
    ) -> Optional[MessageId]:
        """
        Safely edit a message's text, retrying on rate limits and
        sending a new message if the original cannot be edited.

        The method attempts to ensure the target message exists by
        calling ``get_chat`` before editing. It handles ``BadRequest``
        and ``RetryAfter`` errors by retrying or falling back to sending
        a fresh message. The ID of the edited or newly sent message is
        returned.
        """

        if not message_id:
            return await self._view.send_message_return_id(
                chat_id, text, reply_markup=reply_markup
            )

        # Confirm chat availability; errors here are non-fatal.
        try:
            await self._bot.get_chat(chat_id)
        except Exception:
            pass

        try:
            result = await self._view.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            if result:
                return message_id
        except BadRequest as e:
            err = str(e).lower()
            if "message is not modified" in err:
                return message_id
        except Exception as e:
            logger.error(
                "Error editing message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "request_params": {"text": text},
                },
            )

        # If editing failed, send a new message instead.
        new_id = await self._view.send_message_return_id(
            chat_id, text, reply_markup=reply_markup
        )
        return new_id

    async def show_table(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """کارت‌های روی میز را به درخواست بازیکن با فرمت جدید نمایش می‌دهد."""
        game, chat_id = await self._get_game(update, context)

        # پیام درخواست بازیکن حذف نمی‌شود
        logger.debug(
            "Skipping deletion of message %s in chat %s",
            update.message.message_id,
            chat_id,
        )

        if game.state in self.ACTIVE_GAME_STATES and game.cards_table:
            # از متد اصلاح‌شده برای نمایش میز استفاده می‌کنیم
            # با count=0 و یک عنوان عمومی و زیبا
            await self.add_cards_to_table(0, game, chat_id, "🃏 کارت‌های روی میز")
            await self._table_manager.save_game(chat_id, game)
        else:
            msg_id = await self._view.send_message_return_id(
                chat_id, "هنوز بازی شروع نشده یا کارتی روی میز نیست."
            )
            if msg_id:
                logger.debug(
                    "Skipping deletion of message %s in chat %s",
                    msg_id,
                    chat_id,
                )

    async def join_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازیکن با دکمهٔ نشستن سر میز به بازی افزوده می‌شود."""
        game, chat_id = await self._get_game(update, context)
        user = update.effective_user
        if update.callback_query:
            await update.callback_query.answer()

        await self._send_join_prompt(game, chat_id)

        if game.state != GameState.INITIAL:
            await self._view.send_message(chat_id, "⚠️ بازی قبلاً شروع شده است، لطفاً صبر کنید!")
            return

        if game.seated_count() >= MAX_PLAYERS:
            await self._view.send_message(chat_id, "🚪 اتاق پر است!")
            return

        wallet = WalletManagerModel(user.id, self._kv)
        if wallet.value() < SMALL_BLIND * 2:
            await self._view.send_message(
                chat_id,
                f"💸 موجودی شما برای ورود به بازی کافی نیست (حداقل {SMALL_BLIND * 2}$ نیاز است).",
            )
            return

        if user.id not in game.ready_users:
            player = Player(
                user_id=user.id,
                mention_markdown=format_mention_markdown(
                    user.id, user.full_name, version=1
                ),
                wallet=wallet,
                ready_message_id=game.ready_message_main_id,
                seat_index=None,
            )
            game.ready_users.add(user.id)
            seat_assigned = game.add_player(player)
            if seat_assigned == -1:
                await self._view.send_message(chat_id, "🚪 اتاق پر است!")
                return

        ready_list = "\n".join(
            [
                f"{idx+1}. (صندلی {idx+1}) {p.mention_markdown} 🟢"
                for idx, p in enumerate(game.seats)
                if p
            ]
        )
        text = (
            f"👥 *لیست بازیکنان آماده*\n\n{ready_list}\n\n"
            f"📊 {game.seated_count()}/{MAX_PLAYERS} بازیکن آماده\n\n"
            f"🚀 برای شروع بازی /start را بزنید یا منتظر بمانید."
        )

        keyboard_buttons = [
            [InlineKeyboardButton(text="نشستن سر میز", callback_data="join_game")]
        ]
        if game.seated_count() >= self._min_players:
            await self._schedule_auto_start(context, game, chat_id)
            countdown = context.chat_data.get("start_countdown", 60)
            keyboard_buttons[0].append(
                InlineKeyboardButton(
                    text=f"شروع بازی ({countdown})", callback_data="start_game"
                )
            )
        else:
            self._cancel_auto_start(context)

        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        current_text = getattr(game, "ready_message_main_text", "")

        if game.ready_message_main_id:
            if text != current_text:
                new_id = await self._safe_edit_message_text(
                    chat_id,
                    game.ready_message_main_id,
                    text,
                    reply_markup=keyboard,
                )
                if new_id:
                    game.ready_message_main_id = new_id
                    game.ready_message_main_text = text
        else:
            msg = await self._view.send_message_return_id(
                chat_id, text, reply_markup=keyboard
            )
            if msg:
                game.ready_message_main_id = msg
                game.ready_message_main_text = text

        await self._table_manager.save_game(chat_id, game)

    async def ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.join_game(update, context)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """بازی را به صورت دستی شروع می‌کند."""
        game, chat_id = await self._get_game(update, context)
        self._cancel_auto_start(context)
        if game.state not in (GameState.INITIAL, GameState.FINISHED):
            await self._view.send_message(
                chat_id, "🎮 یک بازی در حال حاضر در جریان است."
            )
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
            await self._view.send_message(
                chat_id,
                f"👤 تعداد بازیکنان برای شروع کافی نیست (حداقل {self._min_players} نفر).",
            )
        await self._table_manager.save_game(chat_id, game)

    async def stop(self, user_id: int) -> None:
        """Stop the current game for the chat where the user plays."""
        game, chat_id = await self._get_game_by_user(user_id)
        game.reset()
        await self._table_manager.save_game(chat_id, game)
        await self._view.send_message(chat_id, "🛑 بازی متوقف شد.")

    async def _start_game(
        self, context: CallbackContext, game: Game, chat_id: ChatId
    ) -> None:
        """مراحل شروع یک دست جدید بازی را انجام می‌دهد."""
        self._cancel_auto_start(context)
        if game.ready_message_main_id:
            logger.debug(
                "Skipping deletion of message %s in chat %s",
                game.ready_message_main_id,
                chat_id,
            )
            game.ready_message_main_id = None

        # Ensure dealer_index is initialized before use
        if not hasattr(game, "dealer_index"):
            game.dealer_index = -1
        game.dealer_index = (game.dealer_index + 1) % game.seated_count()

        game.state = GameState.ROUND_PRE_FLOP
        await self._divide_cards(game, chat_id)

        # این متد به تنهایی تمام کارهای لازم برای شروع راند را انجام می‌دهد.
        # از جمله تعیین بلایندها، تعیین نوبت اول و ارسال پیام نوبت.
        await self._round_rate.set_blinds(game, chat_id)

        action_str = "بازی شروع شد"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 4:
            game.last_actions.pop(0)
        if game.turn_message_id:
            current_player = game.get_player_by_seat(game.current_player_index)
            if current_player:
                await self._view.send_turn_actions(
                    chat_id=chat_id,
                    game=game,
                    player=current_player,
                    money=current_player.wallet.value(),
                    message_id=game.turn_message_id,
                    recent_actions=game.last_actions,
                )

        # نیازی به هیچ کد دیگری در اینجا نیست.
        # کدهای اضافی حذف شدند.

        # ذخیره بازیکنان برای دست بعدی (این خط می‌تواند بماند)
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in game.players]

    async def _divide_cards(self, game: Game, chat_id: ChatId):
        """کارت‌ها را فقط در گروه همراه با کیبورد انتخابی توزیع می‌کند."""
        for player in game.seated_players():
            if len(game.remain_cards) < 2:
                await self._view.send_message(
                    chat_id, "کارت‌های کافی در دسته وجود ندارد! بازی ریست می‌شود."
                )
                game.reset()
                return

            cards = [game.remain_cards.pop(), game.remain_cards.pop()]
            player.cards = cards

            stage = self._view._derive_stage_from_table(game.cards_table)
            await self._view.send_cards(
                chat_id=chat_id,
                cards=cards,
                mention_markdown=player.mention_markdown,
                table_cards=game.cards_table,
                hide_hand_text=True,
                stage=stage,
                reply_to_ready_message=False,
            )

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
            contender_details.append(
                {
                    "player": player,
                    "total_bet": player.total_bet,
                    "score": score,
                    "hand_cards": best_hand_cards,
                    "hand_type": hand_type,
                }
            )

        # ۲. شناسایی لایه‌های شرط‌بندی (Tiers) (بدون تغییر)
        bet_tiers = sorted(
            list(set(p["total_bet"] for p in contender_details if p["total_bet"] > 0))
        )

        winners_by_pot = []
        last_bet_tier = 0
        calculated_pot_total = 0  # برای پیگیری مجموع پات محاسبه شده

        # ۳. ساختن پات‌ها به صورت لایه به لایه (منطق اصلی بدون تغییر)
        for tier in bet_tiers:
            tier_contribution = tier - last_bet_tier
            eligible_for_this_pot = [
                p for p in contender_details if p["total_bet"] >= tier
            ]

            pot_size = tier_contribution * len(eligible_for_this_pot)
            calculated_pot_total += pot_size

            if pot_size > 0:
                best_score_in_pot = max(p["score"] for p in eligible_for_this_pot)

                pot_winners_info = [
                    {
                        "player": p["player"],
                        "hand_cards": p["hand_cards"],
                        "hand_type": p["hand_type"],
                    }
                    for p in eligible_for_this_pot
                    if p["score"] == best_score_in_pot
                ]

                winners_by_pot.append({"amount": pot_size, "winners": pot_winners_info})

            last_bet_tier = tier

        # --- FIX: مرحله حیاتی تطبیق و اصلاح نهایی ---
        # اینجا جادو اتفاق می‌افتد: ما پات محاسبه‌شده را با پات واقعی مقایسه می‌کنیم.
        # اگر پولی (مثل بلایندها) جا مانده باشد، آن را به پات اصلی اضافه می‌کنیم.
        discrepancy = game.pot - calculated_pot_total
        if discrepancy > 0 and winners_by_pot:
            # پول گمشده را به اولین پات (پات اصلی) اضافه کن
            winners_by_pot[0]["amount"] += discrepancy
        elif discrepancy < 0:
            # این حالت نباید رخ دهد، اما برای اطمینان لاگ می‌گیریم
            logger.error(
                "Pot calculation mismatch",
                extra={
                    "chat_id": game.chat_id if hasattr(game, "chat_id") else None,
                    "request_params": {
                        "game_pot": game.pot,
                        "calculated": calculated_pot_total,
                    },
                    "error_type": "PotMismatch",
                },
            )
            try:
                asyncio.create_task(
                    self._view.notify_admin(
                        {
                            "event": "pot_mismatch",
                            "game_pot": game.pot,
                            "calculated": calculated_pot_total,
                        }
                    )
                )
            except Exception:
                pass

        # --- FIX 2: ادغام پات‌های غیرضروری ---
        # اگر در نهایت فقط یک پات وجود داشت، اما به اشتباه به چند بخش تقسیم شده بود
        # (مثل سناریوی شما)، همه را در یک پات اصلی ادغام می‌کنیم.
        if len(bet_tiers) == 1 and len(winners_by_pot) > 1:
            logger.info("Merging unnecessary side pots into a single main pot")
            main_pot = {"amount": game.pot, "winners": winners_by_pot[0]["winners"]}
            return [main_pot]

        return winners_by_pot

    async def _process_playing(
        self, chat_id: ChatId, game: Game, context: CallbackContext
    ) -> Optional[Player]:
        """
        مغز متفکر و کنترل‌کننده اصلی جریان بازی.
        این متد پس از هر حرکت بازیکن فراخوانی می‌شود تا تصمیم بگیرد:
        1. آیا دست تمام شده؟ (یک نفر باقی مانده)
        2. آیا دور شرط‌بندی تمام شده؟
        3. در غیر این صورت، نوبت را به بازیکن فعال بعدی بده.
        این متد جایگزین چرخه بازگشتی قبلی بین _process_playing و _move_to_next_player_and_process شده است.
        """
        if game.turn_message_id:
            logger.debug(
                "Keeping turn message %s in chat %s",
                game.turn_message_id,
                chat_id,
            )

        # شرط ۱: آیا فقط یک بازیکن (یا کمتر) در بازی باقی مانده؟
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            await self._go_to_next_street(game, chat_id, context)
            return None

        # شرط ۲: آیا دور شرط‌بندی فعلی به پایان رسیده است؟
        if self._is_betting_round_over(game):
            await self._go_to_next_street(game, chat_id, context)
            return None

        # شرط ۳: بازی ادامه دارد، نوبت را به بازیکن بعدی منتقل کن
        next_player_index = self._round_rate._find_next_active_player_index(
            game, game.current_player_index
        )

        if next_player_index != -1:
            game.current_player_index = next_player_index
            return game.players[next_player_index]

        # اگر هیچ بازیکن فعالی برای حرکت بعدی وجود ندارد (مثلاً همه All-in هستند)
        await self._go_to_next_street(game, chat_id, context)
        return None

    async def _send_turn_message(self, game: Game, player: Player, chat_id: ChatId):
        """پیام نوبت را ارسال کرده و شناسه آن را برای حذف در آینده ذخیره می‌کند."""
        money = player.wallet.value()
        recent_actions = game.last_actions

        if game.turn_message_id:
            await self._view.send_turn_actions(
                chat_id, game, player, money, message_id=game.turn_message_id, recent_actions=recent_actions
            )
        else:
            msg_id = await self._view.send_turn_actions(
                chat_id, game, player, money, recent_actions=recent_actions
            )
            if msg_id:
                game.turn_message_id = msg_id

        game.last_turn_time = datetime.datetime.now()

    # --- Player Action Handlers ---
    # این بخش تمام حرکات ممکن بازیکنان در نوبتشان را مدیریت می‌کند.

    async def player_action_fold(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """بازیکن فولد می‌کند، از دور شرط‌بندی کنار می‌رود و نوبت به نفر بعدی منتقل می‌شود."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        current_player.state = PlayerState.FOLD
        action_str = f"{current_player.mention_markdown}: فولد"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 4:
            game.last_actions.pop(0)

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    async def player_action_call_check(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """بازیکن کال (پرداخت) یا چک (عبور) را انجام می‌دهد."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        call_amount = game.max_round_rate - current_player.round_rate
        current_player.has_acted = True

        try:
            if call_amount > 0:
                current_player.wallet.authorize(game.id, call_amount)
                current_player.round_rate += call_amount
                current_player.total_bet += call_amount
                game.pot += call_amount
            # منطق Check بدون نیاز به عمل خاص
        except UserException as e:
            await self._view.send_message(
                chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}"
            )
            return  # اگر پول نداشت، از ادامه متد جلوگیری کن

        action_type = "کال" if call_amount > 0 else "چک"
        amount = call_amount if call_amount > 0 else 0
        action_str = f"{current_player.mention_markdown}: {action_type}"
        if amount > 0:
            action_str += f" {amount}$"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 4:
            game.last_actions.pop(0)

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    async def player_action_raise_bet(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, raise_amount: int
    ) -> None:
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

            game.max_round_rate = current_player.round_rate
            action_text = "بِت" if call_amount == 0 else "رِیز"

            # --- بخش کلیدی منطق پوکر ---
            game.trading_end_user_id = current_player.user_id
            current_player.has_acted = True
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False

        except UserException as e:
            await self._view.send_message(
                chat_id, f"⚠️ خطای {current_player.mention_markdown}: {e}"
            )
            return

        action_str = f"{current_player.mention_markdown}: {action_text} {total_amount_to_bet}$"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 4:
            game.last_actions.pop(0)

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    async def player_action_all_in(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """بازیکن تمام موجودی خود را شرط می‌بندد (All-in)."""
        game, chat_id = await self._get_game(update, context)
        current_player = self._current_turn_player(game)
        if not current_player:
            return
        all_in_amount = current_player.wallet.value()

        if all_in_amount <= 0:
            self._view.send_message(
                chat_id,
                f"👀 {current_player.mention_markdown} موجودی برای آل-این ندارد و چک می‌کند.",
            )
            await self.player_action_call_check(
                update, context
            )  # این حرکت معادل چک است
            return

        current_player.wallet.authorize(game.id, all_in_amount)
        current_player.round_rate += all_in_amount
        current_player.total_bet += all_in_amount
        game.pot += all_in_amount
        current_player.state = PlayerState.ALL_IN
        current_player.has_acted = True

        action_str = f"{current_player.mention_markdown}: آل-این {all_in_amount}$"
        game.last_actions.append(action_str)
        if len(game.last_actions) > 4:
            game.last_actions.pop(0)

        if current_player.round_rate > game.max_round_rate:
            game.max_round_rate = current_player.round_rate
            game.trading_end_user_id = current_player.user_id
            for p in game.players_by(states=(PlayerState.ACTIVE,)):
                if p.user_id != current_player.user_id:
                    p.has_acted = False

        next_player = await self._process_playing(chat_id, game, context)
        if next_player:
            await self._send_turn_message(game, next_player, chat_id)
        await self._table_manager.save_game(chat_id, game)

    # ---- Table management commands ---------------------------------

    async def create_game(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat_id = update.effective_chat.id
        await self._table_manager.create_game(chat_id)
        game = await self._table_manager.get_game(chat_id)
        await self._send_join_prompt(game, chat_id)
        self._view.send_message(chat_id, "بازی جدید ایجاد شد.")

    async def _go_to_next_street(
        self, game: Game, chat_id: ChatId, context: CallbackContext
    ) -> None:
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
        # پیام‌های نوبت قبلی را حذف نمی‌کنیم
        if game.turn_message_id:
            logger.debug(
                "Keeping turn message %s in chat %s",
                game.turn_message_id,
                chat_id,
            )

        # بررسی می‌کنیم چند بازیکن هنوز در بازی هستند (Active یا All-in)
        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))
        if len(contenders) <= 1:
            # اگر فقط یک نفر باقی مانده، مستقیم به showdown می‌رویم تا برنده مشخص شود
            await self._showdown(game, chat_id, context)
            return

        # جمع‌آوری پول‌های شرط‌بندی شده در این دور و ریست کردن وضعیت بازیکنان
        self._round_rate.collect_bets_for_pot(game)
        for p in game.players:
            p.has_acted = False  # <-- این خط برای دور بعدی حیاتی است

        # رفتن به مرحله بعدی بر اساس وضعیت فعلی بازی
        if game.state == GameState.ROUND_PRE_FLOP:
            game.state = GameState.ROUND_FLOP
            await self.add_cards_to_table(3, game, chat_id, "🃏 فلاپ")
        elif game.state == GameState.ROUND_FLOP:
            game.state = GameState.ROUND_TURN
            await self.add_cards_to_table(1, game, chat_id, "🃏 ترن")
        elif game.state == GameState.ROUND_TURN:
            game.state = GameState.ROUND_RIVER
            await self.add_cards_to_table(1, game, chat_id, "🃏 ریور")
        elif game.state == GameState.ROUND_RIVER:
            # بعد از ریور، دور شرط‌بندی تمام شده و باید showdown انجام شود
            await self._showdown(game, chat_id, context)
            return  # <-- مهم: بعد از فراخوانی showdown، ادامه نمی‌دهیم

        # اگر هنوز بازیکنی برای بازی وجود دارد، نوبت را به نفر اول می‌دهیم
        active_players = game.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            # اگر هیچ بازیکن فعالی نمانده (همه All-in هستند)، مستقیم به مراحل بعدی می‌رویم
            # تا همه کارت‌ها رو شوند.
            await self._go_to_next_street(game, chat_id, context)
            return

        # پیدا کردن اولین بازیکن برای شروع دور جدید (معمولاً اولین فرد فعال بعد از دیلر)
        first_player_index = self._get_first_player_index(game)
        game.current_player_index = first_player_index

        # اگر بازیکنی برای بازی پیدا شد، حلقه بازی را مجدداً شروع می‌کنیم
        if game.current_player_index != -1:
            next_player = await self._process_playing(chat_id, game, context)
            if next_player:
                await self._send_turn_message(game, next_player, chat_id)
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
                score, best_hand, hand_type = (
                    self._winner_determine.get_hand_value_and_type(
                        player.cards, game.cards_table
                    )
                )
            except AttributeError:
                # اگر `get_hand_value_and_type` هنوز پیاده سازی نشده است، این بخش اجرا می شود.
                # این یک fallback موقت است.
                logger.warning(
                    "'get_hand_value_and_type' not found in WinnerDetermination",
                    extra={"chat_id": getattr(game, "chat_id", None)},
                )
                score, best_hand = self._winner_determine.get_hand_value(
                    player.cards, game.cards_table
                )
                # یک روش موقت برای حدس زدن نوع دست بر اساس امتیاز
                hand_type_value = score // (15**5)
                hand_type = (
                    HandsOfPoker(hand_type_value)
                    if hand_type_value > 0
                    else HandsOfPoker.HIGH_CARD
                )

            player_scores.append(
                {
                    "player": player,
                    "score": score,
                    "best_hand": best_hand,
                    "hand_type": hand_type,
                }
            )
        return player_scores

    def _find_winners_from_scores(
        self, player_scores: List[Dict]
    ) -> Tuple[List[Player], int]:
        """از لیست امتیازات، برندگان و بالاترین امتیاز را پیدا می‌کند."""
        if not player_scores:
            return [], 0

        highest_score = max(data["score"] for data in player_scores)
        winners = [
            data["player"] for data in player_scores if data["score"] == highest_score
        ]
        return winners, highest_score

    async def add_cards_to_table(
        self,
        count: int,
        game: Game,
        chat_id: ChatId,
        street_name: str,
        send_message: bool = True,
    ):
        """
        کارت‌های جدید را به میز اضافه کرده و پیامی متنی با کیبورد ترکیبی ارسال می‌کند.
        اگر ``count=0`` باشد، فقط کارت‌های فعلی نمایش داده می‌شود. با تنظیم
        ``send_message=False`` می‌توان فقط کارت‌ها را اضافه کرد بدون ارسال پیام.
        """
        if count > 0:
            for _ in range(count):
                if game.remain_cards:
                    game.cards_table.append(game.remain_cards.pop())

        if not send_message:
            return

        stage = self._view._derive_stage_from_table(game.cards_table)
        markup = self._view._get_table_markup(game.cards_table, stage)

        if not game.board_message_id:
            msg_id = await self._view.send_message_return_id(
                chat_id, street_name, reply_markup=markup
            )
            if msg_id:
                game.board_message_id = msg_id
                if msg_id not in game.message_ids_to_delete:
                    game.message_ids_to_delete.append(msg_id)
        else:
            new_msg_id = await self._view.send_message_return_id(
                chat_id, street_name, reply_markup=markup
            )
            if new_msg_id:
                await self._view.delete_message(chat_id, game.board_message_id)
                if game.board_message_id in game.message_ids_to_delete:
                    game.message_ids_to_delete.remove(game.board_message_id)
                game.board_message_id = new_msg_id
                if new_msg_id not in game.message_ids_to_delete:
                    game.message_ids_to_delete.append(new_msg_id)

        # به‌روزرسانی کیبورد پیام کارت‌های بازیکنان با کارت‌های میز
        for player in game.seated_players():
            if not player.cards:
                continue
            await self._view.send_cards(
                chat_id=chat_id,
                cards=player.cards,
                mention_markdown=player.mention_markdown,
                table_cards=game.cards_table,
                hide_hand_text=True,
                stage=stage,
                reply_to_ready_message=False,
            )
            await asyncio.sleep(0.1)

        # پس از ارسال/ویرایش تصویر میز، پیام نوبت باید آخرین پیام باشد
        if count == 0 and game.turn_message_id:
            current_player = self._current_turn_player(game)
            if current_player:
                await self._send_turn_message(game, current_player, chat_id)

    def _hand_name_from_score(self, score: int) -> str:
        """تبدیل عدد امتیاز به نام دست پوکر"""
        base_rank = score // HAND_RANK
        try:
            # Replacing underscore with space and title-casing the output
            return HandsOfPoker(base_rank).name.replace("_", " ").title()
        except ValueError:
            return "Unknown Hand"

    async def _clear_game_messages(self, game: Game, chat_id: ChatId) -> None:
        """Deletes all temporary messages related to the current hand."""
        logger.debug("Clearing game messages", extra={"chat_id": chat_id})

        ids_to_delete = set(game.message_ids_to_delete)

        if game.board_message_id:
            ids_to_delete.add(game.board_message_id)
            game.board_message_id = None

        if game.turn_message_id:
            ids_to_delete.add(game.turn_message_id)
            game.turn_message_id = None

        for message_id in ids_to_delete:
            try:
                await self._view.delete_message(chat_id, message_id)
            except Exception as e:
                logger.debug(
                    "Failed to delete message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(e).__name__,
                    },
                )

        game.message_ids_to_delete.clear()

    async def _showdown(
        self, game: Game, chat_id: ChatId, context: CallbackContext
    ) -> None:
        """
        فرآیند پایان دست را با استفاده از خروجی دقیق _determine_winners مدیریت می‌کند.
        """

        async def _send_with_retry(func, *args, retries: int = 3):
            for attempt in range(retries):
                try:
                    await func(*args)
                    return
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    logger.error(
                        "Error sending message attempt",
                        extra={
                            "error_type": type(e).__name__,
                            "request_params": {"attempt": attempt + 1, "args": args},
                        },
                    )
                    if attempt + 1 >= retries:
                        return
                    await asyncio.sleep(0.1)

        contenders = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN))

        await self._clear_game_messages(game, chat_id)

        if not contenders:
            # سناریوی نادر که همه قبل از showdown فولد کرده‌اند
            active_players = game.players_by(states=(PlayerState.ACTIVE,))
            if len(active_players) == 1:
                winner = active_players[0]
                winner.wallet.inc(game.pot)
                await self._view.send_message(
                    chat_id,
                    f"🏆 تمام بازیکنان دیگر فولد کردند! {winner.mention_markdown} برنده {game.pot}$ شد.",
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
                await self._view.send_message(
                    chat_id,
                    "ℹ️ هیچ برنده‌ای در این دست مشخص نشد. مشکلی در منطق بازی رخ داده است.",
                )

            # ۲. فراخوانی View برای نمایش نتایج
            # View باید آپدیت شود تا این ساختار داده جدید را به زیبایی نمایش دهد
            await _send_with_retry(
                self._view.send_showdown_results, chat_id, game, winners_by_pot
            )

        # ۳. آماده‌سازی برای دست بعدی
        remaining_players = [p for p in game.players if p.wallet.value() > 0]
        context.chat_data[KEY_OLD_PLAYERS] = [p.user_id for p in remaining_players]

        game.reset()
        await self._table_manager.save_game(chat_id, game)
        await self._send_join_prompt(game, chat_id)

        await asyncio.sleep(0.1)
        await _send_with_retry(self._view.send_new_hand_ready_message, chat_id)

    async def _end_hand(
        self, game: Game, chat_id: ChatId, context: CallbackContext
    ) -> None:
        """
        یک دست از بازی را تمام کرده، پیام‌ها را پاکسازی کرده و برای دست بعدی آماده می‌شود.
        """
        await self._clear_game_messages(game, chat_id)

        # ۲. ذخیره بازیکنان برای دست بعدی
        # این باعث می‌شود در بازی بعدی، لازم نباشد همه دوباره دکمهٔ نشستن سر میز را بزنند
        context.chat_data[KEY_OLD_PLAYERS] = [
            p.user_id for p in game.players if p.wallet.value() > 0
        ]

        # ۳. ریست کردن کامل آبجکت بازی برای شروع یک دست جدید و تمیز
        # یک آبجکت جدید Game می‌سازیم تا هیچ داده‌ای از دست قبل باقی نماند
        new_game = Game()
        context.chat_data[KEY_CHAT_DATA_GAME] = new_game
        await self._table_manager.save_game(chat_id, new_game)
        await self._send_join_prompt(new_game, chat_id)

        # ۴. اعلام پایان دست و راهنمایی برای شروع دست بعدی
        await self._view.send_message(
            chat_id=chat_id,
            text="🎉 دست تمام شد! برای شروع دست بعدی، دکمهٔ «نشستن سر میز» را بزنید یا منتظر بمانید تا کسی /start کند.",
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
    def __init__(
        self,
        view: PokerBotViewer = None,
        kv: redis.Redis = None,
        model: "PokerBotModel" = None,
    ):
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
        await self._set_player_blind(
            game, small_blind_player, SMALL_BLIND, "کوچک", chat_id
        )
        await self._set_player_blind(
            game, big_blind_player, SMALL_BLIND * 2, "بزرگ", chat_id
        )

        game.max_round_rate = SMALL_BLIND * 2
        game.current_player_index = first_action_index
        game.trading_end_user_id = big_blind_player.user_id

        player_turn = game.get_player_by_seat(game.current_player_index)
        if player_turn:
            msg_id = await self._view.send_turn_actions(
                chat_id=chat_id,
                game=game,
                player=player_turn,
                money=player_turn.wallet.value(),
                recent_actions=game.last_actions,
            )
            if msg_id:
                game.turn_message_id = msg_id

    async def _set_player_blind(
        self,
        game: Game,
        player: Player,
        amount: Money,
        blind_type: str,
        chat_id: ChatId,
    ):
        try:
            player.wallet.authorize(game_id=str(chat_id), amount=amount)
            player.round_rate += amount
            player.total_bet += amount  # ← این خط اضافه شود
            game.pot += amount

            action_str = (
                f"💸 {player.mention_markdown} بلایند {blind_type} به مبلغ {amount}$ را پرداخت کرد."
            )
            game.last_actions.append(action_str)
            if len(game.last_actions) > 4:
                game.last_actions.pop(0)
            if game.turn_message_id:
                current_player = game.get_player_by_seat(game.current_player_index)
                if current_player:
                    await self._view.send_turn_actions(
                        chat_id=chat_id,
                        game=game,
                        player=current_player,
                        money=current_player.wallet.value(),
                        message_id=game.turn_message_id,
                        recent_actions=game.last_actions,
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
                f"⚠️ {player.mention_markdown} موجودی کافی برای بلایند نداشت و All-in شد ({available_money}$).",
            )

    def finish_rate(
        self, game: Game, player_scores: Dict[Score, List[Tuple[Player, Cards]]]
    ) -> None:
        """Split the pot among players based on their hand scores.

        ``player_scores`` maps a score to a list of ``(Player, Cards)`` tuples
        where higher scores represent better hands. Players receive chips
        proportional to their wager and capped by the remaining pot.
        """
        total_players = sum(len(v) for v in player_scores.values())
        remaining_pot = game.pot

        for score in sorted(player_scores.keys(), reverse=True):
            group = player_scores[score]
            caps = [
                p.wallet.authorized_money(game.id) * total_players for p, _ in group
            ]
            group_total = sum(caps)
            if group_total == 0:
                continue
            scale = min(1, remaining_pot / group_total)
            for (player, _), cap in zip(group, caps):
                payout = cap * scale
                player.wallet.inc(int(round(payout)))
                remaining_pot -= payout
            if remaining_pot <= 0:
                break

        for group in player_scores.values():
            for player, _ in group:
                player.wallet.approve(game.id)

        game.pot = int(remaining_pot)

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
        self._authorized_money_key = f"u_am:{user_id}"  # برای پول رزرو شده در بازی

        # اسکریپت Lua برای کاهش اتمی موجودی (جلوگیری از race condition)
        # این اسکریپت ابتدا مقدار فعلی را می‌گیرد، اگر کافی بود کم می‌کند و مقدار جدید را برمیگرداند
        # در غیر این صورت -1 را برمیگرداند.
        self._LUA_DECR_IF_GE = self._kv.register_script(
            """
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
        """
        )

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
            result = self._LUA_DECR_IF_GE(
                keys=[self._val_key], args=[amount, DEFAULT_MONEY]
            )
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
        tomorrow = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + datetime.timedelta(days=1)
        ttl = int((tomorrow - now).total_seconds())

        self._kv.setex(self._daily_bonus_key, ttl, "1")
        return self.inc(amount)

    # --- متدهای مربوط به تراکنش‌های بازی (برای تطابق با Wallet ABC) ---
    def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        """Increase reserved money for a specific game."""
        self._kv.hincrby(self._authorized_money_key, game_id, amount)

    def authorized_money(self, game_id: str) -> Money:
        """Return the amount of money currently reserved for ``game_id``."""
        val = self._kv.hget(self._authorized_money_key, game_id)
        return int(val) if val else 0

    def authorize_all(self, game_id: str) -> Money:
        """Reserve the entire wallet for ``game_id`` and return that amount."""
        current = self.value()
        if current > 0:
            self.dec(current)
            self._kv.hincrby(self._authorized_money_key, game_id, current)
        return current

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
