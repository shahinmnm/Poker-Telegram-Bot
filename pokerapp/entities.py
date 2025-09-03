#!/usr/bin/env python3
from abc import abstractmethod
import enum
import datetime
from typing import Tuple, List, Optional
from uuid import uuid4
from pokerapp.cards import get_cards
from typing import Any

MAX_PLAYERS = 8
MIN_PLAYERS = 2
SMALL_BLIND = 5
DEFAULT_MONEY = 1000

KEY_CHAT_DATA_GAME = "game"

MessageId = str
ChatId = str
UserId = str
Mention = str
Score = int
Money = int

@abstractmethod
class Wallet:
    @staticmethod
    def _prefix(id: int, suffix: str = ""):
        pass

    def add_daily(self, amount: Money) -> Money:
        pass

    def has_daily_bonus(self) -> bool:
        pass

    def inc(self, amount: Money = 0) -> None:
        pass

    def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        pass

    def authorized_money(self, game_id: str) -> Money:
        pass

    def authorize(self, game_id: str, amount: Money) -> None:
        pass

    def authorize_all(self, game_id: str) -> Money:
        pass

    def value(self) -> Money:
        pass

    def approve(self, game_id: str) -> None:
        pass

    def cancel(self, game_id: str) -> None:
        pass
class MessageLifespan(enum.Enum):
    TURN = "turn"   # پاک‌سازی در نوبت بعدی
    HAND = "hand"   # پاک‌سازی در انتهای دست

class Player:
    def __init__(
        self,
        user_id: UserId,
        mention_markdown: Mention,
        wallet: Wallet,
        ready_message_id: str,
    ):
        self.user_id = user_id
        self.mention_markdown = mention_markdown
        self.state = PlayerState.ACTIVE
        self.wallet = wallet
        self.cards = []
        self.round_rate = 0
        self.ready_message_id = ready_message_id
        # --- ویژگی‌های اضافه شده ---
        self.total_bet = 0  # کل مبلغ شرط‌بندی شده در یک دست
        self.has_acted = False # آیا در راند فعلی نوبت خود را بازی کرده؟
        # -------------------------

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self.__dict__)

class PlayerState(enum.Enum):
    ACTIVE = 1
    FOLD = 0
    ALL_IN = 10

class Game:
    def __init__(self):
        self.dealer_index = 0  # ایندکس بازیکن Dealer فعلی
        self.reset()

    def reset(self):
        self.id = str(uuid4())
        self.pot = 0
        self.max_round_rate = 0
        self.state = GameState.INITIAL
        self.players: List[Player] = []
        self.cards_table = []
        self.current_player_index = -1
        self.remain_cards = get_cards()
        self.trading_end_user_id = 0
        self.ready_users = set()
        self.last_turn_time = datetime.datetime.now()
        self.turn_message_id: Optional[MessageId] = None # برای حذف دکمه‌های نوبت
        self.message_ledger: List[Tuple[MessageId, MessageLifespan]] = []
        self.ready_message_main_id = None  # پیام اصلی لیست بازیکنان آماده

    @staticmethod
    def _game_from_context(context: Any) -> "Game":
        """
        Safely get or create a Game instance in chat_data.
        Using `Any` avoids importing telegram types here (prevents NameError).
        Ensures `message_ledger` and `message_ids_to_delete` exist on the Game instance.
        """
        if KEY_CHAT_DATA_GAME not in context.chat_data:
            context.chat_data[KEY_CHAT_DATA_GAME] = Game()
        g = context.chat_data[KEY_CHAT_DATA_GAME]
        if not hasattr(g, 'message_ledger'):
            g.message_ledger = []
        if not hasattr(g, 'message_ids_to_delete'):
            g.message_ids_to_delete = []
        return g

        
    def reset_round_rates_and_actions(self):
        """
        Resets the betting state for a new round (street).
        This should be called before dealing the flop, turn, or river.
        """
        self.max_round_rate = 0
        for player in self.players:
            player.round_rate = 0
            # Only reset has_acted for players still in the hand
            if player.state != PlayerState.FOLD:
                player.has_acted = False

    def players_by(self, states: Tuple[PlayerState, ...]) -> List[Player]:
        return list(filter(lambda p: p.state in states, self.players))
        
    def all_in_players_are_covered(self) -> bool:
        """
        Checks if all players who are all-in have put in less money than at least one active player.
        This is to determine if betting can continue or if we should fast-forward.
        """
        active_players = self.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            return True # No more betting possible
            
        max_active_bet = max(p.total_bet for p in active_players) if active_players else 0
        all_in_players = self.players_by(states=(PlayerState.ALL_IN,))
        
        for p_all_in in all_in_players:
            if p_all_in.total_bet >= max_active_bet:
                # An all-in player has more or equal bet than any active player.
                # Betting can only continue if there are at least two active players who can still bet.
                if len(active_players) < 2:
                    return True # Not enough players to continue betting
                return False 
                
        return True

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self.__dict__)

class GameState(enum.Enum):
    INITIAL = 0
    ROUND_PRE_FLOP = 1  # No cards on the table.
    ROUND_FLOP = 2  # Three cards.
    ROUND_TURN = 3  # Four cards.
    ROUND_RIVER = 4  # Five cards.
    FINISHED = 5  # The end.

class PlayerAction(enum.Enum):
    CHECK = "✋ چک"
    CALL = "🎯 کال"
    FOLD = "🏳️ فولد"
    RAISE_RATE = "💹 رِیز"
    BET = "💰 بِت"
    ALL_IN = "🀄 آل‑این"
    SMALL = 10
    NORMAL = 25
    BIG = 50

class UserException(Exception):
    pass

