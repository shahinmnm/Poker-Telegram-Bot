#!/usr/bin/env python3

from abc import ABC, abstractmethod
import enum
import datetime
from typing import Tuple, List, Optional
from uuid import uuid4

from pokerapp.cards import get_cards
from pokerapp.config import get_game_constants


_GAME_CONSTANTS = get_game_constants().game


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


MAX_PLAYERS = _coerce_int(_GAME_CONSTANTS.get("max_players", 8), 8)
MIN_PLAYERS = _coerce_int(_GAME_CONSTANTS.get("min_players", 2), 2)
SMALL_BLIND = _coerce_int(_GAME_CONSTANTS.get("small_blind", 5), 5)
DEFAULT_MONEY = _coerce_int(_GAME_CONSTANTS.get("default_money", 1000), 1000)

MessageId = str
ChatId = str
UserId = str
Mention = str
Score = int
Money = int

class Wallet(ABC):
    @staticmethod
    def _prefix(id: int, suffix: str = ""):
        pass

    @abstractmethod
    async def add_daily(self, amount: Money) -> Money:
        pass

    @abstractmethod
    async def has_daily_bonus(self) -> bool:
        pass

    @abstractmethod
    async def inc(self, amount: Money = 0) -> Money:
        pass

    @abstractmethod
    async def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        pass

    @abstractmethod
    async def authorized_money(self, game_id: str) -> Money:
        pass

    @abstractmethod
    async def authorize(self, game_id: str, amount: Money) -> None:
        pass

    @abstractmethod
    async def authorize_all(self, game_id: str) -> Money:
        pass

    @abstractmethod
    async def value(self) -> Money:
        pass

    @abstractmethod
    async def approve(self, game_id: str) -> None:
        pass

    @abstractmethod
    async def cancel(self, game_id: str) -> None:
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
        self.anchor_message: Optional[Tuple[ChatId, MessageId]] = None
        self.anchor_role: str = "بازیکن"
        self.role_label: str = "بازیکن"
        self.is_dealer: bool = False
        self.is_small_blind: bool = False
        self.is_big_blind: bool = False
        self.private_chat_id: Optional[ChatId] = None
        self.private_keyboard_message: Optional[Tuple[ChatId, MessageId]] = None
        self.private_keyboard_signature: Optional[str] = None
        # -------------------------
    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self.__dict__)

    def is_active(self) -> bool:
        """Return ``True`` if the player is still participating in the hand."""

        return self.state in (PlayerState.ACTIVE, PlayerState.ALL_IN)

    def __getstate__(self):
        state = self.__dict__.copy()
        wallet = state.pop("wallet", None)
        if wallet is not None:
            state["_wallet_info"] = {"user_id": getattr(wallet, "_user_id", None)}
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        # wallet will be reconstructed after unpickling
        self.wallet = None

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
    
        # seats is a fixed-length list representing table seats.
        self.seats: List[Optional[Player]] = [None for _ in range(MAX_PLAYERS)]
    
        self.cards_table = []
        self.current_player_index = -1
        self.small_blind_index = -1
        self.big_blind_index = -1
        self.remain_cards = get_cards()
    
        self.ready_users = set()
        self.message_ids = {}
        # history of most recent player actions; cleared each reset
        # Each entry is a formatted string like "player: action amount$"
        self.last_actions: List[str] = []

        # 🆕 اضافه شده: پیام لیست آماده‌ها
        self.ready_message_main_id: Optional[MessageId] = None
        # متن آخرین پیام آماده‌باش برای جلوگیری از ویرایش‌های تکراری
        self.ready_message_main_text: str = ""
    
        # 🆕 اضافه شده: آرایه پیام‌هایی که باید پاک شوند
        self.message_ids_to_delete: List[MessageId] = []

        # 🆕 اضافه شده: پیام نوبت فعلی
        self.turn_message_id: Optional[MessageId] = None

        # 🆕 اضافه شده: پیام تصویر میز
        self.board_message_id: Optional[MessageId] = None

        # پیام لیست صندلی‌ها که ابتدای هر دست ارسال می‌شود
        self.seat_announcement_message_id: Optional[MessageId] = None

    # --- Seats / players helpers ----------------------------------------
    @property
    def players(self) -> List[Player]:
        """Return a compact list of players currently seated (order is seat ascending)."""
        return [p for p in self.seats if p is not None]

    def seated_players(self) -> List[Player]:
        """Alias for players() to make intent clearer in code."""
        return self.players

    def seated_count(self) -> int:
        return len(self.players)

    def assign_seat_for_user(self, user_id: UserId) -> int:
        """
        Assign the lowest available seat index to a user and return that index.
        If user already seated, return existing seat.
        """
        # if user is already seated return existing seat
        for idx, p in enumerate(self.seats):
            if p is not None and p.user_id == user_id:
                return idx
        for i in range(MAX_PLAYERS):
            if self.seats[i] is None:
                return i
        # fallback: no seat free, return -1
        return -1

    def add_player(self, player: Player, seat_index: Optional[int] = None) -> int:
        """
        Place player into a seat. If seat_index is None, pick first free seat.
        Returns the seat index where the player was placed, or -1 if no seat available.
        """
        if seat_index is None:
            seat_index = self.assign_seat_for_user(player.user_id)
            if seat_index == -1:
                return -1
        if self.seats[seat_index] is not None:
            raise UserException("Seat %s already occupied" % seat_index)
        player.seat_index = seat_index
        self.seats[seat_index] = player
        return seat_index

    def remove_player_by_user(self, user_id: UserId) -> bool:
        for idx, p in enumerate(self.seats):
            if p is not None and p.user_id == user_id:
                self.seats[idx] = None
                return True
        return False

    def get_player_by_seat(self, seat_idx: int) -> Optional[Player]:
        if 0 <= seat_idx < len(self.seats):
            return self.seats[seat_idx]
        return None

    def seat_index_for_user(self, user_id: UserId) -> int:
        for idx, p in enumerate(self.seats):
            if p is not None and p.user_id == user_id:
                return idx
        return -1

    def next_occupied_seat(self, start_seat: int) -> int:
        """Return the next occupied seat index after ``start_seat``.

        The search wraps around the table once and skips empty seats. A
        ``start_seat`` of ``-1`` is treated as "before" the first seat, which
        allows callers to locate the first occupied seat at the table. If no
        occupied seat exists the method returns ``-1``.
        """
        if not any(self.seats):
            return -1

        start = start_seat if 0 <= start_seat < MAX_PLAYERS else -1
        for i in range(1, MAX_PLAYERS + 1):
            idx = (start + i) % MAX_PLAYERS
            if self.seats[idx] is not None:
                return idx
        return -1

    def advance_dealer(self) -> int:
        """Move ``dealer_index`` to the next occupied seat and return it."""
        next_seat = self.next_occupied_seat(self.dealer_index)
        self.dealer_index = next_seat
        return next_seat

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

    def __getstate__(self):
        return self.__dict__.copy()

    def __setstate__(self, state):
        # Reset to initialize default values for attributes added in newer
        # versions before applying the saved state.
        self.reset()
        self.__dict__.update(state)
        # Ensure optional attributes exist even if they were missing from the
        # persisted state.
        if "board_message_id" not in state:
            self.board_message_id = None

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

