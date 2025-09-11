#!/usr/bin/env python3

from abc import abstractmethod
import enum
import datetime
from typing import Tuple, List, Optional
from uuid import uuid4
from pokerapp.cards import get_cards
MAX_PLAYERS = 8
MIN_PLAYERS = 2
SMALL_BLIND = 5
DEFAULT_MONEY = 1000

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

class Player:
    def __init__(
        self,
        user_id: UserId,
        mention_markdown: Mention,
        wallet: Wallet,
        ready_message_id: str,
        seat_index: Optional[int] = None,
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
        self.seat_index = seat_index
        # -------------------------
def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self.__dict__)

class PlayerState(enum.Enum):
    ACTIVE = 1
    FOLD = 0
    ALL_IN = 10


class Game:
    def __init__(self):
        # dealer_index is a seat index into self.seats (0..MAX_PLAYERS-1)
        self.dealer_index = 0
        self.reset()

    def reset(self):
        """
        Initialize or reset the game. We use a fixed-size seats array to
        represent table seats so players keep their seat between hands.
        """
        self.id = str(uuid4())
        self.pot = 0
        self.max_round_rate = 0
        self.state = GameState.INITIAL
    
        # در این نسخه ساده‌شده برای تست‌ها، بازیکنان را در یک لیست ساده نگه می‌داریم
        self._players: List[Player] = []

        self.cards_table = []
        self.current_player_index = -1
        self.small_blind_index = -1
        self.big_blind_index = -1
        self.remain_cards = get_cards()

        self.ready_users = set()
        self.message_ids = {}
        self.last_actions = []  # تاریخچه اکشن‌ها برای HUD
    
        # 🆕 پیام لیست آماده‌ها
        self.ready_message_main_id: Optional[MessageId] = None
    
        # 🆕 پیام‌هایی که انتهای دست باید پاک شوند
        self.message_ids_to_delete: List[MessageId] = []
    
        # 🆕 پیام نوبت فعلی (پیام جدا و پین‌شونده برای دکمه‌ها)
        self.turn_message_id: Optional[MessageId] = None
    
        # 🆕 پیام HUD (ثابت و ادیت‌شونده؛ پین نمی‌شود)
        self.hud_message_id: Optional[MessageId] = None
        
    def add_last_action(self, text: str) -> None:
        """
        یک اکشن جدید را به لیست آخرین اکشن‌ها اضافه می‌کند و طول
        لیست را حداکثر به ۳ مورد محدود نگه می‌دارد (FIFO).
        این متد صرفاً برای نمایش در HUD است و در منطق بازی دخالت ندارد.
        """
        if text is None:
            return
        self.last_actions.append(text)
        if len(self.last_actions) > 3:
            self.last_actions = self.last_actions[-3:]


    @property
    def players(self) -> List[Player]:
        return self._players

    def seated_players(self) -> List[Player]:
        return self._players

    def seated_count(self) -> int:
        return len(self._players)

    # متدهای زیر برای سازگاری باقی مانده‌اند اما پیاده‌سازی ساده‌ای دارند
    def add_player(self, player: Player, seat_index: Optional[int] = None) -> int:
        self._players.append(player)
        return len(self._players) - 1

    def remove_player_by_user(self, user_id: UserId) -> bool:
        for p in list(self._players):
            if p.user_id == user_id:
                self._players.remove(p)
                return True
        return False

    def get_player_by_seat(self, seat_idx: int) -> Optional[Player]:
        if 0 <= seat_idx < len(self._players):
            return self._players[seat_idx]
        return None

    def seat_index_for_user(self, user_id: UserId) -> int:
        for idx, p in enumerate(self._players):
            if p.user_id == user_id:
                return idx
        return -1

    def next_occupied_seat(self, start_seat: int) -> int:
        """
        Return the next occupied seat index after start_seat (exclusive).
        If no other occupied seats, return -1.
        """
        if start_seat < 0:
            return -1
        for i in range(1, MAX_PLAYERS + 1):
            idx = (start_seat + i) % MAX_PLAYERS
            if self.seats[idx] is not None:
                return idx
        return -1

    def advance_dealer(self):
        """
        Move dealer_index to the next occupied seat. If none found, set to -1.
        """
        nxt = self.next_occupied_seat(self.dealer_index)
        self.dealer_index = nxt if nxt != -1 else -1

    def players_by(self, states: Tuple) -> List[Player]:
        """Return players whose state is in states (search seats)."""
        return [p for p in self.players if p.state in states]

    def all_in_players_are_covered(self) -> bool:
        """
        Checks if all players who are all-in have put in less money than at least one active player.
        This is to determine if betting can continue or if we should fast-forward.
        """
        active_players = self.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            return True  # No active players, betting cannot continue

        max_active_bet = max((p.total_bet for p in active_players), default=0)
        all_in_players = self.players_by(states=(PlayerState.ALL_IN,))

        for p_all_in in all_in_players:
            if p_all_in.total_bet >= max_active_bet:
                # An all-in player has more or equal bet than any active player.
                # Betting can only continue if there are at least two active players who can still bet.
                if len(active_players) < 2:
                    return True  # Not enough players to continue betting
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

