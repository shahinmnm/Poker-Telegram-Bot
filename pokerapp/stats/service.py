from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    select,
    func,
    delete,
)
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class Base(DeclarativeBase):
    """Base declarative class used for the statistics models."""


class PlayerStats(Base):
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
    __tablename__ = "game_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hand_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("hand_id", "user_id", name="uq_game_participant"),)


class PlayerHandHistory(Base):
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
    __tablename__ = "player_winning_hands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    hand_type: Mapped[str] = mapped_column(String(128), nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "hand_type", name="uq_player_winning_hand"),)


@dataclass(slots=True)
class PlayerIdentity:
    user_id: int
    display_name: str
    username: Optional[str] = None
    full_name: Optional[str] = None
    private_chat_id: Optional[int] = None


@dataclass(slots=True)
class PlayerHandResult:
    user_id: int
    display_name: str
    total_bet: int
    payout: int
    net_profit: int
    hand_type: Optional[str] = None
    was_all_in: bool = False
    result: Optional[str] = None


@dataclass(slots=True)
class PlayerStatisticsReport:
    stats: PlayerStats
    recent_games: List[PlayerHandHistory] = field(default_factory=list)
    top_winning_hands: List[PlayerWinningHand] = field(default_factory=list)


@dataclass(slots=True)
class _HandContext:
    hand_id: str
    chat_id: int
    started_at: dt.datetime
    players: List[PlayerIdentity]


class BaseStatsService:
    """Abstract base class for statistics services."""

    async def register_player_profile(
        self, identity: PlayerIdentity, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        raise NotImplementedError

    async def start_hand(
        self,
        hand_id: str,
        chat_id: int,
        players: Iterable[PlayerIdentity],
        *,
        start_time: Optional[dt.datetime] = None,
    ) -> None:
        raise NotImplementedError

    async def finish_hand(
        self,
        hand_id: str,
        chat_id: int,
        results: Iterable[PlayerHandResult],
        pot_total: int,
        *,
        end_time: Optional[dt.datetime] = None,
    ) -> None:
        raise NotImplementedError

    async def record_daily_bonus(
        self, user_id: int, amount: int, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        raise NotImplementedError

    async def build_player_report(self, user_id: int) -> Optional[PlayerStatisticsReport]:
        raise NotImplementedError

    def format_report(self, report: PlayerStatisticsReport) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class NullStatsService(BaseStatsService):
    """Fallback service used when the SQL database is not configured."""

    async def register_player_profile(
        self, identity: PlayerIdentity, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        return None

    async def start_hand(
        self,
        hand_id: str,
        chat_id: int,
        players: Iterable[PlayerIdentity],
        *,
        start_time: Optional[dt.datetime] = None,
    ) -> None:
        return None

    async def finish_hand(
        self,
        hand_id: str,
        chat_id: int,
        results: Iterable[PlayerHandResult],
        pot_total: int,
        *,
        end_time: Optional[dt.datetime] = None,
    ) -> None:
        return None

    async def record_daily_bonus(
        self, user_id: int, amount: int, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        return None

    async def build_player_report(self, user_id: int) -> Optional[PlayerStatisticsReport]:
        return None

    def format_report(self, report: PlayerStatisticsReport) -> str:
        return ""


class StatsService(BaseStatsService):
    """Concrete implementation backed by an async SQLAlchemy engine."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self._enabled = bool(database_url)
        self._engine: Optional[AsyncEngine] = None
        self._sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None
        self._schema_lock = asyncio.Lock()
        self._initialized = False
        self._active_hands: Dict[str, _HandContext] = {}
        if not self._enabled:
            return

        engine_kwargs: Dict[str, object] = {"echo": echo, "future": True}
        if database_url.startswith("sqlite+aiosqlite:///:memory"):
            engine_kwargs["poolclass"] = StaticPool
        self._engine = create_async_engine(database_url, **engine_kwargs)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @staticmethod
    def _utcnow() -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    @staticmethod
    def _coerce_int(value: Optional[int | str]) -> int:
        if value is None:
            return 0
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _split_sql_statements(sql: str) -> List[str]:
        statements: List[str] = []
        buffer: List[str] = []
        for line in sql.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            buffer.append(line)
            if stripped.endswith(";"):
                statement = "\n".join(buffer).strip()
                buffer.clear()
                if statement.endswith(";"):
                    statement = statement[:-1].rstrip()
                if statement:
                    statements.append(statement)
        remainder = "\n".join(buffer).strip()
        if remainder:
            statements.append(remainder.rstrip(";"))
        return statements

    @staticmethod
    def _prepare_statement_for_sqlite(statement: str) -> str:
        updated = re.sub(
            r"SERIAL\s+PRIMARY\s+KEY",
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            statement,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"DEFAULT\s+NOW\s*\(\)",
            "DEFAULT CURRENT_TIMESTAMP",
            updated,
            flags=re.IGNORECASE,
        )
        return updated

    async def _run_migrations(self, conn: AsyncConnection) -> None:
        if not MIGRATIONS_DIR.exists():
            return
        backend = getattr(conn.dialect, "name", "").lower()
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            try:
                sql = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("Unable to read migration %s: %s", path, exc)
                continue
            statements = self._split_sql_statements(sql)
            if not statements:
                continue
            logger.debug("Applying migration %s", path.name)
            for statement in statements:
                text = statement
                if backend == "sqlite":
                    text = self._prepare_statement_for_sqlite(text)
                if not text.strip():
                    continue
                try:
                    await conn.exec_driver_sql(text)
                except Exception:
                    logger.exception("Failed to apply migration statement from %s", path.name)
                    raise

    async def ensure_ready(self) -> None:
        if not self._enabled:
            return
        await self._ensure_schema()

    def ensure_ready_blocking(self) -> None:
        if not self._enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self.ensure_ready())
            finally:
                loop.close()
        else:
            loop.create_task(self.ensure_ready())

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def _ensure_schema(self) -> None:
        if not self._enabled or self._initialized:
            return
        async with self._schema_lock:
            if self._initialized or not self._enabled:
                return
            assert self._engine is not None
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await self._run_migrations(conn)
            self._initialized = True

    def _normalize_identity(self, identity: PlayerIdentity) -> PlayerIdentity:
        return PlayerIdentity(
            user_id=self._coerce_int(identity.user_id),
            display_name=identity.display_name,
            username=identity.username,
            full_name=identity.full_name,
            private_chat_id=(
                self._coerce_int(identity.private_chat_id)
                if identity.private_chat_id is not None
                else None
            ),
        )

    async def register_player_profile(
        self, identity: PlayerIdentity, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        if not self._enabled or self._sessionmaker is None:
            return
        await self._ensure_schema()
        normalized = self._normalize_identity(identity)
        now = timestamp or self._utcnow()
        async with self._sessionmaker() as session:
            async with session.begin():
                stats = await session.get(PlayerStats, normalized.user_id)
                display_name = normalized.full_name or normalized.display_name
                if stats is None:
                    stats = PlayerStats(
                        user_id=normalized.user_id,
                        display_name=display_name,
                        username=normalized.username,
                        first_seen=now,
                        last_seen=now,
                        last_private_chat_id=normalized.private_chat_id,
                    )
                    session.add(stats)
                else:
                    stats.last_seen = now
                    if display_name and not stats.display_name:
                        stats.display_name = display_name
                    if normalized.username and stats.username != normalized.username:
                        stats.username = normalized.username
                    if normalized.private_chat_id:
                        stats.last_private_chat_id = normalized.private_chat_id

    async def start_hand(
        self,
        hand_id: str,
        chat_id: int,
        players: Iterable[PlayerIdentity],
        *,
        start_time: Optional[dt.datetime] = None,
    ) -> None:
        if not self._enabled or self._sessionmaker is None:
            return
        await self._ensure_schema()
        started_at = start_time or self._utcnow()
        player_list = [self._normalize_identity(player) for player in players]
        self._active_hands[hand_id] = _HandContext(
            hand_id=hand_id,
            chat_id=self._coerce_int(chat_id),
            started_at=started_at,
            players=player_list,
        )

        async with self._sessionmaker() as session:
            async with session.begin():
                game = await session.get(GameSession, hand_id)
                if game is None:
                    game = GameSession(
                        hand_id=hand_id,
                        chat_id=self._coerce_int(chat_id),
                        started_at=started_at,
                        participant_count=len(player_list),
                        is_active=True,
                    )
                    session.add(game)
                else:
                    game.chat_id = self._coerce_int(chat_id)
                    game.started_at = started_at
                    game.participant_count = len(player_list)
                    game.is_active = True

                if not player_list:
                    return

                player_ids = [p.user_id for p in player_list]
                existing_stats = {
                    stat.user_id: stat
                    for stat in (
                        await session.execute(
                            select(PlayerStats).where(PlayerStats.user_id.in_(player_ids))
                        )
                    ).scalars()
                }

                existing_participants = {
                    participant.user_id
                    for participant in (
                        await session.execute(
                            select(GameParticipant).where(
                                GameParticipant.hand_id == hand_id
                            )
                        )
                    ).scalars()
                }

                for identity in player_list:
                    stats = existing_stats.get(identity.user_id)
                    display_name = identity.full_name or identity.display_name
                    if stats is None:
                        stats = PlayerStats(
                            user_id=identity.user_id,
                            display_name=display_name,
                            username=identity.username,
                            first_seen=started_at,
                            last_seen=started_at,
                            last_private_chat_id=identity.private_chat_id,
                        )
                        session.add(stats)
                    else:
                        stats.last_seen = started_at
                        if display_name and not stats.display_name:
                            stats.display_name = display_name
                    if identity.user_id not in existing_participants:
                        session.add(
                            GameParticipant(
                                hand_id=hand_id,
                                user_id=identity.user_id,
                                joined_at=started_at,
                            )
                        )

    async def finish_hand(
        self,
        hand_id: str,
        chat_id: int,
        results: Iterable[PlayerHandResult],
        pot_total: int,
        *,
        end_time: Optional[dt.datetime] = None,
    ) -> None:
        if not self._enabled or self._sessionmaker is None:
            return
        await self._ensure_schema()
        ended_at = end_time or self._utcnow()
        context = self._active_hands.pop(hand_id, None)
        started_at = context.started_at if context else ended_at
        duration_seconds = int(
            max((ended_at - started_at).total_seconds(), 0)
        ) if started_at else 0

        normalized_results = [
            PlayerHandResult(
                user_id=self._coerce_int(result.user_id),
                display_name=result.display_name,
                total_bet=max(int(result.total_bet), 0),
                payout=max(int(result.payout), 0),
                net_profit=int(result.net_profit),
                hand_type=result.hand_type,
                was_all_in=bool(result.was_all_in),
                result=result.result,
            )
            for result in results
        ]

        if not normalized_results and context:
            normalized_results = [
                PlayerHandResult(
                    user_id=player.user_id,
                    display_name=player.display_name,
                    total_bet=0,
                    payout=0,
                    net_profit=0,
                    hand_type=None,
                    was_all_in=False,
                    result="push",
                )
                for player in context.players
            ]

        async with self._sessionmaker() as session:
            async with session.begin():
                game = await session.get(GameSession, hand_id)
                top_hand = next(
                    (
                        result.hand_type
                        for result in sorted(
                            normalized_results,
                            key=lambda r: r.payout,
                            reverse=True,
                        )
                        if result.hand_type
                    ),
                    None,
                )
                if game is None:
                    game = GameSession(
                        hand_id=hand_id,
                        chat_id=self._coerce_int(chat_id),
                        started_at=started_at,
                        finished_at=ended_at,
                        duration_seconds=duration_seconds,
                        pot_total=pot_total,
                        participant_count=len(normalized_results),
                        top_winning_hand=top_hand,
                        is_active=False,
                    )
                    session.add(game)
                else:
                    game.chat_id = self._coerce_int(chat_id)
                    game.finished_at = ended_at
                    game.duration_seconds = duration_seconds
                    game.pot_total = pot_total
                    if normalized_results:
                        game.participant_count = len(normalized_results)
                    game.top_winning_hand = top_hand
                    game.is_active = False

                await session.execute(
                    delete(PlayerHandHistory).where(
                        PlayerHandHistory.hand_id == hand_id
                    )
                )

                if not normalized_results:
                    return

                player_ids = [result.user_id for result in normalized_results]
                existing_stats = {
                    stat.user_id: stat
                    for stat in (
                        await session.execute(
                            select(PlayerStats).where(PlayerStats.user_id.in_(player_ids))
                        )
                    ).scalars()
                }

                winning_hand_rows = {
                    (row.user_id, row.hand_type): row
                    for row in (
                        await session.execute(
                            select(PlayerWinningHand).where(
                                PlayerWinningHand.user_id.in_(player_ids)
                            )
                        )
                    ).scalars()
                }

                for result in normalized_results:
                    stats = existing_stats.get(result.user_id)
                    if stats is None:
                        stats = PlayerStats(
                            user_id=result.user_id,
                            display_name=result.display_name,
                            first_seen=started_at,
                            last_seen=ended_at,
                        )
                        session.add(stats)
                    else:
                        stats.last_seen = ended_at
                        if result.display_name and not stats.display_name:
                            stats.display_name = result.display_name

                    stats.total_games += 1
                    stats.total_play_time += duration_seconds
                    stats.lifetime_bet_amount += result.total_bet
                    stats.lifetime_profit += result.net_profit
                    stats.total_amount_won += result.payout
                    stats.total_pot_participated += pot_total
                    if result.was_all_in:
                        stats.total_all_in_events += 1

                    loss_amount = 0
                    outcome = result.result
                    if outcome is None:
                        if result.net_profit > 0:
                            outcome = "win"
                        elif result.net_profit < 0:
                            outcome = "loss"
                        else:
                            outcome = "push"

                    if outcome == "win":
                        stats.total_wins += 1
                        stats.current_win_streak += 1
                        stats.current_loss_streak = 0
                        if stats.current_win_streak > stats.longest_win_streak:
                            stats.longest_win_streak = stats.current_win_streak
                        if result.net_profit > stats.biggest_win_amount:
                            stats.biggest_win_amount = result.net_profit
                            stats.biggest_win_hand = result.hand_type
                        if result.hand_type:
                            key = (result.user_id, result.hand_type)
                            row = winning_hand_rows.get(key)
                            if row is None:
                                row = PlayerWinningHand(
                                    user_id=result.user_id,
                                    hand_type=result.hand_type,
                                    win_count=1,
                                )
                                session.add(row)
                                winning_hand_rows[key] = row
                            else:
                                row.win_count += 1
                            if row.win_count > stats.most_common_winning_hand_count:
                                stats.most_common_winning_hand_count = row.win_count
                                stats.most_common_winning_hand = result.hand_type
                        if result.was_all_in:
                            stats.total_all_in_wins += 1
                    elif outcome == "loss":
                        stats.total_losses += 1
                        stats.current_loss_streak += 1
                        stats.current_win_streak = 0
                        if stats.current_loss_streak > stats.longest_loss_streak:
                            stats.longest_loss_streak = stats.current_loss_streak
                        loss_amount = result.total_bet
                    else:
                        stats.current_win_streak = 0
                        stats.current_loss_streak = 0

                    if outcome != "win":
                        stats.total_amount_lost += loss_amount
                    else:
                        loss_component = max(result.total_bet - result.payout, 0)
                        stats.total_amount_lost += loss_component

                    stats.last_result = outcome
                    stats.last_game_at = ended_at
                    if pot_total > stats.largest_pot_participated:
                        stats.largest_pot_participated = pot_total
                    if result.hand_type and "فولد" not in result.hand_type:
                        stats.total_showdowns += 1

                    session.add(
                        PlayerHandHistory(
                            hand_id=hand_id,
                            user_id=result.user_id,
                            chat_id=self._coerce_int(chat_id),
                            started_at=started_at,
                            finished_at=ended_at,
                            duration_seconds=duration_seconds,
                            hand_type=result.hand_type,
                            result=outcome,
                            amount_won=result.payout,
                            amount_lost=loss_amount if outcome == "loss" else max(result.total_bet - result.payout, 0),
                            net_profit=result.net_profit,
                            total_bet=result.total_bet,
                            pot_size=pot_total,
                            was_all_in=result.was_all_in,
                        )
                    )

    async def record_daily_bonus(
        self, user_id: int, amount: int, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        if not self._enabled or self._sessionmaker is None:
            return
        await self._ensure_schema()
        now = timestamp or self._utcnow()
        async with self._sessionmaker() as session:
            async with session.begin():
                stats = await session.get(PlayerStats, self._coerce_int(user_id))
                if stats is None:
                    stats = PlayerStats(
                        user_id=self._coerce_int(user_id),
                        first_seen=now,
                        last_seen=now,
                        total_bonus_claimed=amount,
                        last_bonus_at=now,
                    )
                    session.add(stats)
                else:
                    stats.total_bonus_claimed += amount
                    stats.last_bonus_at = now
                    stats.last_seen = now

    async def build_player_report(self, user_id: int) -> Optional[PlayerStatisticsReport]:
        if not self._enabled or self._sessionmaker is None:
            return None
        await self._ensure_schema()
        async with self._sessionmaker() as session:
            stats = await session.get(PlayerStats, self._coerce_int(user_id))
            if stats is None:
                return None
            recent_games = (
                await session.execute(
                    select(PlayerHandHistory)
                    .where(PlayerHandHistory.user_id == stats.user_id)
                    .order_by(PlayerHandHistory.finished_at.desc())
                    .limit(5)
                )
            ).scalars().all()
            top_winning = (
                await session.execute(
                    select(PlayerWinningHand)
                    .where(PlayerWinningHand.user_id == stats.user_id)
                    .order_by(PlayerWinningHand.win_count.desc())
                    .limit(3)
                )
            ).scalars().all()
            return PlayerStatisticsReport(
                stats=stats,
                recent_games=list(recent_games),
                top_winning_hands=list(top_winning),
            )

    @staticmethod
    def _format_number(value: int) -> str:
        formatted = f"{value:,}".replace(",", "٬")
        return formatted

    @classmethod
    def _format_currency(cls, value: int) -> str:
        sign = "-" if value < 0 else ""
        return f"{sign}{cls._format_number(abs(value))}$"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = int(round(seconds))
        minutes, sec = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours} ساعت و {minutes} دقیقه"
        if minutes:
            return f"{minutes} دقیقه و {sec} ثانیه"
        return f"{sec} ثانیه"

    def format_report(self, report: PlayerStatisticsReport) -> str:
        stats = report.stats
        total_games = max(stats.total_games, 0)
        win_rate = (stats.total_wins / total_games * 100) if total_games else 0.0
        average_duration = (
            stats.total_play_time / total_games if total_games else 0
        )
        average_profit = (
            stats.lifetime_profit / total_games if total_games else 0
        )
        average_bet = (
            stats.lifetime_bet_amount / total_games if total_games else 0
        )
        roi = None
        if stats.total_amount_lost > 0:
            roi = (
                (stats.total_amount_won - stats.total_amount_lost)
                / stats.total_amount_lost
            ) * 100

        lines: List[str] = []
        lines.append("📊 گزارش پیشرفته عملکرد شما")
        if stats.display_name:
            lines.append(f"👤 نام: {stats.display_name}")
        if stats.username:
            lines.append(f"🔖 نام کاربری: @{stats.username}")
        lines.append(f"🎮 مجموع دست‌ها: {self._format_number(total_games)}")
        lines.append(
            f"🏆 بردها: {self._format_number(stats.total_wins)} | ❌ باخت‌ها: {self._format_number(stats.total_losses)}"
        )
        lines.append(f"📈 نرخ برد: {win_rate:.1f}%")
        lines.append(
            f"⏱️ میانگین زمان هر دست: {self._format_duration(average_duration)}"
        )
        lines.append(
            f"💰 سود/زیان تجمعی: {self._format_currency(stats.lifetime_profit)}"
        )
        lines.append(
            "💵 مجموع برد: "
            f"{self._format_currency(stats.total_amount_won)} | 📉 مجموع باخت: "
            f"{self._format_currency(stats.total_amount_lost)}"
        )
        lines.append(
            f"💳 مجموع شرط‌بندی: {self._format_currency(stats.lifetime_bet_amount)}"
        )
        lines.append(
            f"📊 میانگین سود هر دست: {self._format_currency(int(average_profit))}"
        )
        lines.append(
            f"🎯 میانگین مبلغ شرط: {self._format_currency(int(average_bet))}"
        )
        lines.append(
            f"🔥 طولانی‌ترین برد متوالی: {self._format_number(stats.longest_win_streak)} دست | 🥀 طولانی‌ترین باخت متوالی: {self._format_number(stats.longest_loss_streak)} دست"
        )
        if stats.total_pot_participated:
            average_pot = stats.total_pot_participated / max(total_games, 1)
            lines.append(
                f"🏺 میانگین اندازه پات: {self._format_currency(int(average_pot))}"
            )
        if stats.biggest_win_amount > 0:
            hand_name = stats.biggest_win_hand or "دست نامشخص"
            lines.append(
                f"💎 بزرگ‌ترین برد: {self._format_currency(stats.biggest_win_amount)} با دست «{hand_name}»"
            )
        if stats.most_common_winning_hand:
            lines.append(
                f"📊 رایج‌ترین دست برنده: {stats.most_common_winning_hand} ({self._format_number(stats.most_common_winning_hand_count)} بار)"
            )
        if stats.total_all_in_wins:
            lines.append(
                f"🀄 بردهای آل-این: {self._format_number(stats.total_all_in_wins)}"
            )
        if stats.total_all_in_events:
            success_rate = (
                (stats.total_all_in_wins / stats.total_all_in_events) * 100
                if stats.total_all_in_events
                else 0
            )
            lines.append(
                f"⚔️ دفعات آل-این: {self._format_number(stats.total_all_in_events)} (موفقیت {success_rate:.1f}٪)"
            )
        if stats.total_showdowns:
            lines.append(
                f"🪄 تعداد شو-داون: {self._format_number(stats.total_showdowns)}"
            )
        if stats.largest_pot_participated:
            lines.append(
                f"🏦 بزرگ‌ترین پات: {self._format_currency(stats.largest_pot_participated)}"
            )
        if stats.total_bonus_claimed:
            lines.append(
                f"🎁 مجموع بونوس دریافتی: {self._format_currency(stats.total_bonus_claimed)}"
            )
        if roi is not None:
            lines.append(f"📐 بازده سرمایه (ROI): {roi:.1f}%")
        if stats.last_game_at:
            lines.append(
                f"🕰️ آخرین بازی: {stats.last_game_at.strftime('%Y-%m-%d %H:%M UTC')}"
            )
        if stats.last_bonus_at:
            lines.append(
                f"🎯 آخرین بونوس: {stats.last_bonus_at.strftime('%Y-%m-%d %H:%M UTC')}"
            )

        if report.top_winning_hands:
            lines.append("\n🥇 پراکندگی دست‌های برنده:")
            for row in report.top_winning_hands:
                lines.append(
                    f"• {row.hand_type}: {self._format_number(row.win_count)} برد"
                )

        if report.recent_games:
            lines.append("\n📝 پنج دست اخیر:")
            for game in report.recent_games:
                outcome = game.result
                if outcome == "win":
                    prefix = "✅ برد"
                elif outcome == "loss":
                    prefix = "❌ باخت"
                else:
                    prefix = "🤝 مساوی"
                timestamp = (
                    game.finished_at.strftime("%Y-%m-%d %H:%M")
                    if game.finished_at
                    else "-"
                )
                hand_name = game.hand_type or "بدون شو-داون"
                lines.append(
                    f"• {timestamp} | {prefix} | سود: {self._format_currency(game.net_profit)} | دست: {hand_name}"
                )

        lines.append(
            "\n🤖 برای مشاهده آمار لحظه‌ای دست جدید، همیشه از دکمه «📊 آمار بازی» در چت خصوصی استفاده کنید."
        )
        return "\n".join(lines)
