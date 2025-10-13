from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from pokerapp.config import DEFAULT_TIMEZONE_NAME, get_game_constants
from pokerapp.database_schema import (
    Base,
    GameParticipant,
    GameSession,
    PlayerHandHistory,
    PlayerStats,
    PlayerWinningHand,
)
from pokerapp.utils.datetime_utils import ensure_utc
from pokerapp.utils.time_utils import format_local, now_utc
from pokerapp.utils.markdown import escape_markdown_v1
from pokerapp.stats.buffer import StatsBatchBuffer

if TYPE_CHECKING:
    from pokerapp.utils.cache import AdaptivePlayerReportCache


logger = logging.getLogger(__name__)

_CONSTANTS = get_game_constants()
_EMOJI_DATA = _CONSTANTS.emojis


def _chip_emoji(key: str, default: str) -> str:
    if isinstance(_EMOJI_DATA, dict):
        chips = _EMOJI_DATA.get("chips", {})
        if isinstance(chips, dict):
            value = chips.get(key)
            if isinstance(value, str) and value:
                return value
    return default


_PROFIT_EMOJI = _chip_emoji("profit", "💰")
_WINNINGS_EMOJI = _chip_emoji("winnings", "💵")
_AVERAGE_POT_EMOJI = _chip_emoji("average_pot", _chip_emoji("pot", "🏺"))

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


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


@dataclass(slots=True)
class _BufferedHandRecord:
    """Serializable payload representing a completed hand awaiting persistence."""

    hand_id: str
    chat_id: int
    coerced_chat_id: int
    pot_total: int
    ended_at: dt.datetime
    started_at: Optional[dt.datetime]
    duration_seconds: int
    results: List[PlayerHandResult]

    def to_buffer(self) -> Dict[str, Any]:
        """Return a dictionary payload suitable for :class:`StatsBatchBuffer`."""

        return {
            "hand_id": self.hand_id,
            "chat_id": self.chat_id,
            "coerced_chat_id": self.coerced_chat_id,
            "pot_total": self.pot_total,
            "ended_at": self.ended_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "duration_seconds": self.duration_seconds,
            "results": [asdict(result) for result in self.results],
        }

    @classmethod
    def from_buffer(cls, payload: Dict[str, Any]) -> "_BufferedHandRecord":
        """Reconstruct an instance from buffered dictionary payload."""

        ended_raw = payload.get("ended_at")
        started_raw = payload.get("started_at")
        if isinstance(ended_raw, str) and ended_raw:
            ended_at = dt.datetime.fromisoformat(ended_raw)
        elif isinstance(ended_raw, dt.datetime):
            ended_at = ended_raw
        else:
            ended_at = now_utc()
        started_at: Optional[dt.datetime]
        if isinstance(started_raw, str) and started_raw:
            started_at = dt.datetime.fromisoformat(started_raw)
        elif isinstance(started_raw, dt.datetime):
            started_at = ensure_utc(started_raw)
        else:
            started_at = None

        results_payload = payload.get("results", [])
        results = [
            PlayerHandResult(
                user_id=int(item.get("user_id", 0)),
                display_name=item.get("display_name", str(item.get("user_id", 0))),
                total_bet=int(item.get("total_bet", 0)),
                payout=int(item.get("payout", 0)),
                net_profit=int(item.get("net_profit", 0)),
                hand_type=item.get("hand_type"),
                was_all_in=bool(item.get("was_all_in", False)),
                result=item.get("result"),
            )
            for item in results_payload
        ]

        duration_seconds = int(payload.get("duration_seconds", 0))

        return cls(
            hand_id=str(payload.get("hand_id", "")),
            chat_id=int(payload.get("chat_id", 0)),
            coerced_chat_id=int(payload.get("coerced_chat_id", payload.get("chat_id", 0))),
            pot_total=int(payload.get("pot_total", 0)),
            ended_at=ensure_utc(ended_at),
            started_at=ensure_utc(started_at) if started_at else None,
            duration_seconds=duration_seconds,
            results=results,
        )



class BaseStatsService:
    """Abstract base class for statistics services."""

    def __init__(self, *, timezone_name: str = DEFAULT_TIMEZONE_NAME) -> None:
        self._timezone_name = timezone_name

    @property
    def timezone_name(self) -> str:
        return self._timezone_name

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

    async def record_hand_finished_batch(
        self,
        *,
        hand_id: str,
        chat_id: int,
        results: Iterable[PlayerHandResult],
        pot_total: int,
        end_time: Optional[dt.datetime] = None,
    ) -> None:
        """Optimised batch version of :meth:`finish_hand`.

        The default implementation simply delegates to :meth:`finish_hand` so
        that subclasses can override only one of the two methods. Concrete
        implementations are encouraged to override this method to perform
        batched persistence when available.
        """

        await self.finish_hand(
            hand_id,
            chat_id,
            results,
            pot_total,
            end_time=end_time,
        )

    async def record_daily_bonus(
        self, user_id: int, amount: int, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        raise NotImplementedError

    async def build_player_report(self, user_id: int) -> Optional[PlayerStatisticsReport]:
        raise NotImplementedError

    def bind_player_report_cache(
        self, cache: "AdaptivePlayerReportCache"
    ) -> None:
        """Allow services to access the adaptive player report cache."""


    def format_report(self, report: PlayerStatisticsReport) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class NullStatsService(BaseStatsService):
    """Fallback service used when the SQL database is not configured."""

    def __init__(self, *, timezone_name: str = DEFAULT_TIMEZONE_NAME) -> None:
        super().__init__(timezone_name=timezone_name)

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

    async def record_hand_finished_batch(
        self,
        *,
        hand_id: str,
        chat_id: int,
        results: Iterable[PlayerHandResult],
        pot_total: int,
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

    def bind_player_report_cache(
        self, cache: "AdaptivePlayerReportCache"
    ) -> None:
        return None


class StatsService(BaseStatsService):
    """Concrete implementation backed by an async SQLAlchemy engine."""

    def __init__(
        self,
        database_url: str,
        *,
        echo: bool = False,
        player_report_cache: Optional["AdaptivePlayerReportCache"] = None,
        timezone_name: str = DEFAULT_TIMEZONE_NAME,
    ) -> None:
        super().__init__(timezone_name=timezone_name)
        self._enabled = bool(database_url)
        self._engine: Optional[AsyncEngine] = None
        self._sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None
        self._schema_lock = asyncio.Lock()
        self._initialized = False
        self._active_hands: Dict[str, _HandContext] = {}
        self._stats_buffer: Optional[StatsBatchBuffer] = None
        self._buffer_flusher_started = False
        self._buffer_start_lock = asyncio.Lock()
        self._player_report_cache = player_report_cache
        if not self._enabled:
            return

        engine_kwargs: Dict[str, object] = {"echo": echo, "future": True}
        if database_url.startswith("sqlite+aiosqlite:///:memory"):
            engine_kwargs["poolclass"] = StaticPool
        self._engine = create_async_engine(database_url, **engine_kwargs)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    def bind_player_report_cache(
        self, cache: "AdaptivePlayerReportCache"
    ) -> None:
        self._player_report_cache = cache

    def attach_buffer(self, buffer: StatsBatchBuffer) -> None:
        """Attach a :class:`StatsBatchBuffer` used for deferred persistence."""

        self._stats_buffer = buffer
        self._buffer_flusher_started = False

    @staticmethod
    def _utcnow() -> dt.datetime:
        return now_utc()

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
        inside_trigger = False
        for line in sql.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue

            upper = stripped.upper()
            if upper.startswith("CREATE TRIGGER"):
                inside_trigger = True

            buffer.append(line)

            if inside_trigger:
                if upper in {"END", "END;"}:
                    statement = "\n".join(buffer).strip()
                    buffer.clear()
                    inside_trigger = False
                    if statement.endswith(";"):
                        statement = statement[:-1].rstrip()
                    if statement:
                        statements.append(statement)
                continue

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

    async def _check_migration_presence(self, table_name: str) -> bool:
        """Check if a specific table exists (migration already applied)."""

        if not self._engine:
            return False

        try:
            async with self._engine.begin() as conn:
                backend = getattr(conn.dialect, "name", "").lower()

                if backend == "sqlite":
                    query = text(
                        """
                        SELECT COUNT(*)
                        FROM sqlite_master
                        WHERE type='table' AND name=:table_name
                        """
                    )
                else:
                    query = text(
                        """
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_name = :table_name
                        )
                        """
                    )

                result = await conn.execute(query, {"table_name": table_name})
                return bool(result.scalar())
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning(
                "Migration presence check failed: %s",  # noqa: G003
                exc,
                extra={"event_type": "stats_schema_migration_check_failed"},
            )
            return False

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
            # Skip PostgreSQL-specific migrations (002) on SQLite
            if backend == "sqlite" and path.name == "002_add_performance_indexes.sql":
                logger.debug(
                    "Skipping PostgreSQL-specific migration %s for SQLite", path.name
                )
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
                except OperationalError as exc:
                    message = str(exc).lower()
                    if (
                        "no such table" in message
                        or "no such column" in message
                        or "has no column" in message
                    ):
                        logger.warning(
                            "Skipping migration statement from %s due to missing dependency: %s",
                            path.name,
                            text,
                        )
                        continue
                    logger.exception(
                        "Failed to apply migration statement from %s", path.name
                    )
                    raise
                except Exception:
                    logger.exception(
                        "Failed to apply migration statement from %s", path.name
                    )
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

    async def _ensure_buffer_started(self) -> None:
        """Ensure the stats batch buffer background flusher is running."""

        if not self._stats_buffer or self._buffer_flusher_started:
            return

        async with self._buffer_start_lock:
            if not self._stats_buffer or self._buffer_flusher_started:
                return
            await self._stats_buffer.start_background_flusher()
            self._buffer_flusher_started = True

    async def close(self) -> None:
        if self._stats_buffer is not None:
            await self._stats_buffer.shutdown()
            self._stats_buffer = None
            self._buffer_flusher_started = False
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
        now = ensure_utc(timestamp) if timestamp else self._utcnow()
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

    def _stats_to_dict(
        self, stats: PlayerStats, fallback_username: str
    ) -> Dict[str, Any]:
        return {
            "user_id": stats.user_id,
            "username": stats.username or fallback_username,
            "display_name": stats.display_name or fallback_username,
            "total_games": stats.total_games,
            "total_wins": stats.total_wins,
            "total_losses": stats.total_losses,
            "total_play_time": stats.total_play_time,
            "total_amount_won": stats.total_amount_won,
            "total_amount_lost": stats.total_amount_lost,
            "lifetime_profit": stats.lifetime_profit,
        }

    async def get_or_create_player_stats(
        self, user_id: int, username: str
    ) -> Dict[str, Any]:
        """Fetch player statistics, creating defaults when missing."""

        normalized_id = self._coerce_int(user_id)
        fallback_username = username or str(normalized_id)
        minimal_defaults = {
            "user_id": normalized_id,
            "username": fallback_username,
            "display_name": fallback_username,
            "total_games": 0,
            "total_wins": 0,
            "total_losses": 0,
            "total_play_time": 0,
            "total_amount_won": 0,
            "total_amount_lost": 0,
            "lifetime_profit": 0,
        }

        if not self._enabled or self._sessionmaker is None:
            return minimal_defaults

        await self._ensure_schema()

        async with self._sessionmaker() as session:
            stats = await session.get(PlayerStats, normalized_id)
            if stats is None:
                logger.warning(
                    "Player stats not found for user_id=%s, initializing defaults",
                    normalized_id,
                    extra={"user_id": normalized_id},
                )
                now = self._utcnow()
                async with session.begin():
                    session.add(
                        PlayerStats(
                            user_id=normalized_id,
                            display_name=fallback_username,
                            username=username or None,
                            first_seen=now,
                            last_seen=now,
                        )
                    )

                stats = await session.get(PlayerStats, normalized_id)
                if stats is None:
                    logger.error(
                        "Failed to create player stats for user_id=%s",
                        normalized_id,
                        extra={"user_id": normalized_id},
                    )
                    return minimal_defaults

                logger.info(
                    "Created default stats for user_id=%s",
                    normalized_id,
                    extra={
                        "category": "player_stats",
                        "event": "initialization",
                        "user_id": normalized_id,
                        "username": username,
                    },
                )

            return self._stats_to_dict(stats, fallback_username)

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
        started_at = ensure_utc(start_time) if start_time else self._utcnow()
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

    async def record_hand_finished_batch(
        self,
        *,
        hand_id: str,
        chat_id: int,
        results: Iterable[PlayerHandResult],
        pot_total: int,
        end_time: Optional[dt.datetime] = None,
    ) -> None:
        if not self._enabled or self._sessionmaker is None:
            return
        await self._ensure_schema()
        payload = self._build_hand_buffer_payload(
            hand_id=hand_id,
            chat_id=chat_id,
            results=results,
            pot_total=pot_total,
            end_time=end_time,
        )
        if payload is None:
            return

        if self._stats_buffer is not None:
            await self._ensure_buffer_started()
            await self._stats_buffer.add([payload.to_buffer()])
            return

        await self._flush_hand_batch_records([payload])

    def _build_hand_buffer_payload(
        self,
        *,
        hand_id: str,
        chat_id: int,
        results: Iterable[PlayerHandResult],
        pot_total: int,
        end_time: Optional[dt.datetime],
    ) -> Optional[_BufferedHandRecord]:
        ended_at = ensure_utc(end_time) if end_time else self._utcnow()
        context = self._active_hands.pop(hand_id, None)
        started_at = context.started_at if context else ended_at
        if started_at:
            started_at = ensure_utc(started_at)
        duration_seconds = (
            int(max((ended_at - started_at).total_seconds(), 0))
            if started_at
            else 0
        )

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

        if not normalized_results:
            return None

        coerced_chat_id = self._coerce_int(chat_id)

        return _BufferedHandRecord(
            hand_id=hand_id,
            chat_id=chat_id,
            coerced_chat_id=coerced_chat_id,
            pot_total=pot_total,
            ended_at=ended_at,
            started_at=started_at,
            duration_seconds=duration_seconds,
            results=normalized_results,
        )

    async def _flush_hand_batch_records(
        self, records: Iterable[Dict[str, Any] | _BufferedHandRecord]
    ) -> None:
        if not self._enabled or self._sessionmaker is None:
            return

        payloads: List[_BufferedHandRecord] = []
        for record in records:
            if isinstance(record, _BufferedHandRecord):
                payloads.append(record)
            else:
                payloads.append(_BufferedHandRecord.from_buffer(record))

        if not payloads:
            return

        await self._ensure_schema()
        for payload in payloads:
            try:
                await self._process_hand_finished_batch(payload)
            except Exception:
                logger.warning(
                    "Failed to flush buffered statistics for hand_id=%s", payload.hand_id,
                    exc_info=True,
                )

    async def _process_hand_finished_batch(self, payload: _BufferedHandRecord) -> None:
        if not payload.results or self._sessionmaker is None:
            return

        players_for_invalidation: List[int] = []

        async with self._sessionmaker() as session:
            async with session.begin():
                game = await session.get(GameSession, payload.hand_id)
                top_hand = next(
                    (
                        result.hand_type
                        for result in sorted(
                            payload.results,
                            key=lambda r: r.payout,
                            reverse=True,
                        )
                        if result.hand_type
                    ),
                    None,
                )
                if game is None:
                    game = GameSession(
                        hand_id=payload.hand_id,
                        chat_id=payload.coerced_chat_id,
                        started_at=payload.started_at,
                        finished_at=payload.ended_at,
                        duration_seconds=payload.duration_seconds,
                        pot_total=payload.pot_total,
                        participant_count=len(payload.results),
                        top_winning_hand=top_hand,
                        is_active=False,
                    )
                    session.add(game)
                else:
                    game.chat_id = payload.coerced_chat_id
                    game.finished_at = payload.ended_at
                    game.duration_seconds = payload.duration_seconds
                    game.pot_total = payload.pot_total
                    if payload.results:
                        game.participant_count = len(payload.results)
                    game.top_winning_hand = top_hand
                    game.is_active = False

                await session.execute(
                    delete(PlayerHandHistory).where(
                        PlayerHandHistory.hand_id == payload.hand_id
                    )
                )

                player_ids = [result.user_id for result in payload.results]
                players_for_invalidation = list(dict.fromkeys(player_ids))
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

                history_rows: List[Dict[str, Any]] = []
                new_stats_objects: List[PlayerStats] = []
                new_winning_rows: List[PlayerWinningHand] = []

                for result in payload.results:
                    stats = existing_stats.get(result.user_id)
                    if stats is None:
                        stats = PlayerStats(
                            user_id=result.user_id,
                            display_name=result.display_name,
                            first_seen=payload.started_at,
                            last_seen=payload.ended_at,
                        )
                        existing_stats[result.user_id] = stats
                        new_stats_objects.append(stats)
                    else:
                        stats.last_seen = payload.ended_at
                        if result.display_name and not stats.display_name:
                            stats.display_name = result.display_name

                    stats.total_games += 1
                    stats.total_play_time += payload.duration_seconds
                    stats.lifetime_bet_amount += result.total_bet
                    stats.lifetime_profit += result.net_profit
                    stats.total_amount_won += result.payout
                    stats.total_pot_participated += payload.pot_total
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
                                winning_hand_rows[key] = row
                                new_winning_rows.append(row)
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
                    stats.last_game_at = payload.ended_at
                    if payload.pot_total > stats.largest_pot_participated:
                        stats.largest_pot_participated = payload.pot_total
                    if result.hand_type and "فولد" not in result.hand_type:
                        stats.total_showdowns += 1

                    history_rows.append(
                        {
                            "hand_id": payload.hand_id,
                            "user_id": result.user_id,
                            "chat_id": payload.coerced_chat_id,
                            "started_at": payload.started_at,
                            "finished_at": payload.ended_at,
                            "duration_seconds": payload.duration_seconds,
                            "hand_type": result.hand_type,
                            "result": outcome,
                            "amount_won": result.payout,
                            "amount_lost": loss_amount if outcome == "loss" else max(result.total_bet - result.payout, 0),
                            "net_profit": result.net_profit,
                            "total_bet": result.total_bet,
                            "pot_size": payload.pot_total,
                            "was_all_in": result.was_all_in,
                        }
                    )

                if new_stats_objects:
                    session.add_all(new_stats_objects)
                if new_winning_rows:
                    session.add_all(new_winning_rows)
                if history_rows:
                    await session.execute(
                        insert(PlayerHandHistory),
                        history_rows,
                    )

        if (
            players_for_invalidation
            and self._player_report_cache is not None
        ):
            self._player_report_cache.invalidate_on_event(
                players_for_invalidation,
                event_type="hand_finished",
                chat_id=payload.coerced_chat_id,
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
        await self.record_hand_finished_batch(
            hand_id=hand_id,
            chat_id=chat_id,
            results=results,
            pot_total=pot_total,
            end_time=end_time,
        )

    async def record_daily_bonus(
        self, user_id: int, amount: int, *, timestamp: Optional[dt.datetime] = None
    ) -> None:
        if not self._enabled or self._sessionmaker is None:
            return
        await self._ensure_schema()
        now = ensure_utc(timestamp) if timestamp else self._utcnow()
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

        if self._player_report_cache is not None:
            self._player_report_cache.invalidate_on_event(
                [self._coerce_int(user_id)],
                event_type="bonus_claimed",
            )

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

            stats.first_seen = ensure_utc(stats.first_seen)
            stats.last_seen = ensure_utc(stats.last_seen)
            if stats.last_game_at is not None:
                stats.last_game_at = ensure_utc(stats.last_game_at)
            if stats.last_bonus_at is not None:
                stats.last_bonus_at = ensure_utc(stats.last_bonus_at)

            for row in recent_games:
                if row.started_at is not None:
                    row.started_at = ensure_utc(row.started_at)
                if row.finished_at is not None:
                    row.finished_at = ensure_utc(row.finished_at)

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
            safe_display_name = escape_markdown_v1(stats.display_name)
            lines.append(f"👤 نام: {safe_display_name}")
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
            f"{_PROFIT_EMOJI} سود/زیان تجمعی: {self._format_currency(stats.lifetime_profit)}"
        )
        lines.append(
            f"{_WINNINGS_EMOJI} مجموع برد: {self._format_currency(stats.total_amount_won)} | "
            f"📉 مجموع باخت: {self._format_currency(stats.total_amount_lost)}"
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
                f"{_AVERAGE_POT_EMOJI} میانگین اندازه پات: {self._format_currency(int(average_pot))}"
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
            last_game = format_local(
                stats.last_game_at, self._timezone_name, fmt="%Y-%m-%d %H:%M %Z"
            )
            lines.append(f"🕰️ آخرین بازی: {last_game}")
        if stats.last_bonus_at:
            last_bonus = format_local(
                stats.last_bonus_at, self._timezone_name, fmt="%Y-%m-%d %H:%M %Z"
            )
            lines.append(f"🎯 آخرین بونوس: {last_bonus}")

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
                if game.finished_at:
                    timestamp = format_local(
                        game.finished_at, self._timezone_name, fmt="%Y-%m-%d %H:%M"
                    )
                else:
                    timestamp = "-"
                hand_name = game.hand_type or "بدون شو-داون"
                lines.append(
                    f"• {timestamp} | {prefix} | سود: {self._format_currency(game.net_profit)} | دست: {hand_name}"
                )

        lines.append(
            "\n🤖 برای مشاهده آمار لحظه‌ای دست جدید، همیشه از دکمه «📊 آمار بازی» در چت خصوصی استفاده کنید."
        )
        return "\n".join(lines)
