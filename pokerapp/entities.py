#!/usr/bin/env python3

from abc import abstractmethod
import enum
import datetime
from typing import Tuple, List, Optional, Dict
from uuid import uuid4
from pokerapp.cards import get_cards  # Assuming cards.py exists with get_cards function

# Constants
MAX_PLAYERS = 8
MIN_PLAYERS = 2
SMALL_BLIND = 5
DEFAULT_MONEY = 1000

# Type Aliases
MessageId = str
ChatId = str
UserId = str
Mention = str
Score = int
Money = int

# Abstract Wallet class
class Wallet:
    @abstractmethod
    def value(self) -> Money:
        """Returns the current balance."""
        pass

    @abstractmethod
    def inc(self, amount: Money) -> Money:
        """Increases the balance by the given amount."""
        pass

    @abstractmethod
    def dec(self, amount: Money) -> Money:
        """Decreases the balance by the given amount if sufficient funds."""
        pass

    @abstractmethod
    def has_daily_bonus(self) -> bool:
        """Checks if the daily bonus has been claimed."""
        pass

    @abstractmethod
    def add_daily(self, amount: Money) -> Money:
        """Adds daily bonus if not already claimed."""
        pass

    @abstractmethod
    def authorize(self, game_id: str, amount: Money) -> None:
        """Authorizes (reserves) money for a game transaction."""
        pass

    @abstractmethod
    def approve(self, game_id: str) -> None:
        """Approves a successful game transaction."""
        pass

    @abstractmethod
    def cancel(self, game_id: str) -> None:
        """Cancels a failed game transaction and returns reserved money."""
        pass

    @abstractmethod
    def authorized_money(self, game_id: str) -> Money:
        """Returns the authorized amount for a specific game."""
        pass

    @abstractmethod
    def inc_authorized_money(self, game_id: str, amount: Money) -> None:
        """Increases the authorized amount for a game."""
        pass

    @abstractmethod
    def authorize_all(self, game_id: str) -> Money:
        """Authorizes all remaining money for the game."""
        pass

# Player class
class Player:
    def __init__(
        self,
        user_id: UserId,
        mention_markdown: Mention,
        wallet: Wallet,
        ready_message_id: Optional[MessageId] = None,
        seat_index: Optional[int] = None,
    ):
        self.user_id = user_id
        self.mention_markdown = mention_markdown
        self.state = PlayerState.ACTIVE
        self.wallet = wallet
        self.cards: List[str] = []  # Player's hole cards
        self.round_rate: Money = 0  # Bet in the current betting round
        self.total_bet: Money = 0  # Total bet in the hand (for side pots)
        self.has_acted: bool = False  # Has the player acted in this round?
        self.ready_message_id = ready_message_id
        self.seat_index = seat_index

    def __repr__(self):
        return f"{self.__class__.__name__}({self.__dict__!r})"

# PlayerState Enum
class PlayerState(enum.Enum):
    ACTIVE = 1
    FOLD = 0
    ALL_IN = 10

# Game class
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
        self.pot: Money = 0
        self.max_round_rate: Money = 0
        self.state = GameState.INITIAL

        # seats is a fixed-length list representing table seats.
        self.seats: List[Optional[Player]] = [None for _ in range(MAX_PLAYERS)]

        self.cards_table: List[str] = []  # Community cards
        self.current_player_index: int = -1  # Current turn seat index
        self.small_blind_index: int = -1
        self.big_blind_index: int = -1
        self.remain_cards: List[str] = get_cards()  # Deck of cards

        self.ready_users: set = set()  # Users ready to play
        self.message_ids: Dict = {}  # Stored message IDs
        self.last_actions: List = []  # Last player actions

        # Additional fields for message management
        self.ready_message_main_id: Optional[MessageId] = None
        self.message_ids_to_delete: List[MessageId] = []
        self.turn_message_id: Optional[MessageId] = None
        self.last_hand_result_message_id: Optional[MessageId] = None
        self.last_hand_end_message_id: Optional[MessageId] = None

        # For trading end
        self.trading_end_user_id: Optional[UserId] = None

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
            raise UserException(f"Seat {seat_index} already occupied")
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

    def players_by(self, states: Tuple[PlayerState, ...]) -> List[Player]:
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

    def is_round_ended(self) -> bool:
        """
        Checks if the current betting round is over.
        All active players must have acted, and all bets must be equal.
        """
        active_players = self.players_by(states=(PlayerState.ACTIVE,))
        if not active_players:
            return True  # No active players left, round is over

        # Check if all have acted
        if not all(p.has_acted for p in active_players):
            return False

        # Check if all have the same round_rate
        rates = {p.round_rate for p in active_players}
        return len(rates) == 1

    def __repr__(self):
        return f"{self.__class__.__name__}({self.__dict__!r})"

# GameState Enum
class GameState(enum.Enum):
    INITIAL = 0
    ROUND_PRE_FLOP = 1  # No cards on the table.
    ROUND_FLOP = 2  # Three cards.
    ROUND_TURN = 3  # Four cards.
    ROUND_RIVER = 4  # Five cards.
    FINISHED = 5  # The end.

# PlayerAction Enum
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

# UserException
class UserException(Exception):
    pass
