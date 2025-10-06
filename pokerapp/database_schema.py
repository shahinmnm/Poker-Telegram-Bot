"""SQLAlchemy ORM models for the statistics database schema."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    """Declarative base for the statistics tables."""


class PlayerStats(Base):
    """Core table containing aggregated statistics for each player."""

    __tablename__ = "player_stats"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )
    last_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )
    last_game_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_bonus_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_private_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    total_games: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_play_time: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_amount_won: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_amount_lost: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    biggest_win_amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    biggest_win_hand: Mapped[Optional[str]] = mapped_column(String(128))
    current_win_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_loss_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    longest_win_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    longest_loss_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    most_common_winning_hand: Mapped[Optional[str]] = mapped_column(String(128))
    most_common_winning_hand_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifetime_bet_amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_profit: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_all_in_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_all_in_events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_showdowns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_pot_participated: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    largest_pot_participated: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_bonus_claimed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_result: Mapped[Optional[str]] = mapped_column(String(16))


class GameSession(Base):
    """Table recording high-level information for each hand that was played."""

    __tablename__ = "game_sessions"

    hand_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pot_total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    participant_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top_winning_hand: Mapped[Optional[str]] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class GameParticipant(Base):
    """Link table mapping players to the hands they participated in."""

    __tablename__ = "game_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hand_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("hand_id", "user_id", name="uq_game_participant"),)


class PlayerHandHistory(Base):
    """Detailed history for each hand that a player took part in."""

    __tablename__ = "player_hand_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hand_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hand_type: Mapped[Optional[str]] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_won: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    amount_lost: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    net_profit: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_bet: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    pot_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    was_all_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PlayerWinningHand(Base):
    """Aggregated information about the hands players most frequently win with."""

    __tablename__ = "player_winning_hands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    hand_type: Mapped[str] = mapped_column(String(128), nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "hand_type", name="uq_player_winning_hand"),)


__all__ = [
    "Base",
    "GameParticipant",
    "GameSession",
    "PlayerHandHistory",
    "PlayerStats",
    "PlayerWinningHand",
]

