"""Application composition root for the Poker Telegram bot."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set, TYPE_CHECKING

import redis.asyncio as aioredis
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from pokerapp.cache_manager import CacheConfig, MultiLayerCache
from pokerapp.config import Config, _SYSTEM_CONSTANTS
from pokerapp.db_client import CachePolicy, OptimizedDatabaseClient
from pokerapp.logging_config import setup_logging
from pokerapp.stats import BaseStatsService, NullStatsService, StatsService
from pokerapp.stats.buffer import StatsBatchBuffer
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.query_optimizer import QueryBatcher
from pokerapp.table_manager import TableManager
from pokerapp.translations import init_translations
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.redis_safeops import RedisSafeOps
from pokerapp.utils.request_metrics import RequestMetrics
from pokerapp.utils.telegram_safeops import TelegramSafeOps
from pokerapp.utils.player_report_cache import PlayerReportCache
from pokerapp.utils.cache import AdaptivePlayerReportCache
from pokerapp.utils.logging_helpers import ContextLoggerAdapter, enforce_context
from pokerapp.state_validator import GameStateValidator
from pokerapp.recovery_service import RecoveryService
from pokerapp.telegram_retry_manager import TelegramRetryManager
from pokerapp.database_schema import Base as StatisticsBase

if TYPE_CHECKING:
    from pokerapp.lock_manager import LockManager as SmartLockManager


def _build_redis_client_kwargs(cfg: Config) -> Dict[str, Any]:
    """Return connection settings for the Redis client."""

    return {
        "host": cfg.REDIS_HOST,
        "port": cfg.REDIS_PORT,
        "db": cfg.REDIS_DB,
        "password": cfg.REDIS_PASS or None,
        "auto_close_connection_pool": False,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "retry_on_timeout": True,
        "health_check_interval": 30,
    }


def _create_redis_client(client_kwargs: Dict[str, Any]) -> aioredis.Redis:
    """Create a Redis client ensuring connections are established lazily."""

    kwargs_copy = dict(client_kwargs)
    redis_client = aioredis.Redis(**kwargs_copy)
    setattr(redis_client, "_client_init_kwargs", kwargs_copy)
    return redis_client


@dataclass(frozen=True)
class ApplicationServices:
    """Container for infrastructure dependencies shared across the bot."""

    logger: ContextLoggerAdapter
    kv_async: aioredis.Redis
    redis_ops: RedisSafeOps
    table_manager: TableManager
    stats_service: BaseStatsService
    player_report_cache: PlayerReportCache
    adaptive_player_report_cache: AdaptivePlayerReportCache
    request_metrics: RequestMetrics
    private_match_service: PrivateMatchService
    messaging_service_factory: Callable[..., MessagingService]
    telegram_safeops_factory: Callable[..., TelegramSafeOps]
    retry_manager: TelegramRetryManager
    stats_buffer: Optional[StatsBatchBuffer]
    cache: MultiLayerCache
    db_client: Optional[OptimizedDatabaseClient]
    query_batcher: Optional[QueryBatcher]
    smart_lock_manager: SmartLockManager


def _build_stats_service(logger: ContextLoggerAdapter, cfg: Config) -> BaseStatsService:
    if not cfg.DATABASE_URL:
        return NullStatsService(timezone_name=cfg.TIMEZONE_NAME)

    try:
        _ensure_statistics_schema(
            cfg.DATABASE_URL,
            echo=getattr(cfg, "DATABASE_ECHO", False),
            logger=logger,
        )
        stats_service = StatsService(
            cfg.DATABASE_URL,
            echo=getattr(cfg, "DATABASE_ECHO", False),
            timezone_name=cfg.TIMEZONE_NAME,
        )
        stats_service.ensure_ready_blocking()
        return stats_service
    except Exception:  # pragma: no cover - defensive logging
        logger.exception("Failed to initialise StatsService; using NullStatsService")
        return NullStatsService(timezone_name=cfg.TIMEZONE_NAME)


def _make_service_logger(
    parent_logger: ContextLoggerAdapter, child_name: str, category: str
) -> ContextLoggerAdapter:
    """Return a child logger enriched with the provided ``category`` context."""

    return enforce_context(
        parent_logger.getChild(child_name), {"request_category": category}
    )


async def _has_migrations_applied(
    conn: AsyncConnection, logger: ContextLoggerAdapter
) -> bool:
    backend = getattr(conn.dialect, "name", "").lower()
    backend = backend.split("+")[0] if backend else backend

    metadata_queries = {
        "postgresql": text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = 'game_sessions'
            )
            """
        ),
        "postgres": text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = 'game_sessions'
            )
            """
        ),
        "mysql": text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 'game_sessions'
            )
            """
        ),
        "mariadb": text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 'game_sessions'
            )
            """
        ),
    }

    if backend == "sqlite":
        metadata_query = text(
            """
            SELECT COUNT(*) > 0
            FROM sqlite_master
            WHERE type='table' AND name='game_sessions'
            """
        )
    else:
        metadata_query = metadata_queries.get(backend)

    if metadata_query is not None:
        try:
            result = await conn.execute(metadata_query)
            return bool(result.scalar())
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning(
                "Migration presence check failed for %s backend: %s",  # noqa: G003
                backend or "unknown",
                exc,
                extra={"event_type": "stats_schema_migration_check_failed"},
            )
    else:
        logger.debug(
            "No metadata query configured for %s backend; falling back to inspector",
            backend or "unknown",
        )

    try:
        return await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).has_table("game_sessions")
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning(
            "Migration presence inspector fallback failed for %s backend: %s",  # noqa: G003
            backend or "unknown",
            exc,
            extra={"event_type": "stats_schema_migration_check_failed"},
        )
        return False


async def _initialize_statistics_schema(
    database_url: str, *, echo: bool, logger: ContextLoggerAdapter
) -> None:
    """Ensure the statistics schema exists by creating ORM tables if required."""

    engine = create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
    )

    try:
        async with engine.begin() as conn:
            async def _has_migration_table(conn: AsyncConnection) -> bool:
                """Check if the _migration_history table exists."""

                try:
                    backend = getattr(conn.dialect, "name", "").lower()

                    if backend == "sqlite":
                        query = text(
                            """
                            SELECT COUNT(*) > 0
                            FROM sqlite_master
                            WHERE type='table' AND name='_migration_history'
                            """
                        )
                    else:
                        query = text(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM information_schema.tables
                                WHERE table_name='_migration_history'
                            )
                            """
                        )

                    result = await conn.execute(query)
                    return bool(result.scalar())
                except Exception as exc:  # pragma: no cover - defensive logging
                    logger.warning(
                        "Migration table check failed: %s",  # noqa: G003
                        exc,
                        extra={
                            "event_type": "stats_migration_table_check_failed"
                        },
                    )
                    return False

            async def _get_applied_migration_count(conn: AsyncConnection) -> int:
                """Count applied migrations."""

                try:
                    query = text("SELECT COUNT(*) FROM _migration_history")
                    result = await conn.execute(query)
                    return int(result.scalar() or 0)
                except Exception:
                    return 0

            has_migration_table = await _has_migration_table(conn)

            if has_migration_table:
                applied_count = await _get_applied_migration_count(conn)
                logger.info(
                    "Statistics schema already initialized",
                    extra={
                        "event_type": "stats_schema_verified",
                        "applied_migrations": applied_count,
                    },
                )
                return

            logger.info(
                "Bootstrapping statistics schema",
                extra={"event_type": "stats_schema_bootstrap_start"},
            )
            try:
                await conn.run_sync(StatisticsBase.metadata.create_all)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error(
                    "Failed to create statistics schema: %s",  # noqa: G003
                    exc,
                    extra={"event_type": "stats_schema_bootstrap_failed"},
                    exc_info=True,
                )
                raise

            logger.info(
                "Statistics schema bootstrapped successfully",
                extra={"event_type": "stats_schema_bootstrap_complete"},
            )
    finally:
        await engine.dispose()


def _ensure_statistics_schema(
    database_url: str, *, echo: bool, logger: ContextLoggerAdapter
) -> None:
    """Synchronous wrapper for schema initialization (blocking)."""
    if not database_url:
        logger.info(
            "No database URL provided, skipping schema initialization",
            extra={"event_type": "stats_schema_skipped"},
        )
        return

    async def _runner() -> None:
        await _initialize_statistics_schema(database_url, echo=echo, logger=logger)

    try:
        asyncio.run(_runner())
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error(
            "Schema initialization failed: %s",  # noqa: G003
            exc,
            extra={"event_type": "stats_schema_init_failed"},
            exc_info=True,
        )
        raise


def build_services(cfg: Config, *, skip_stats_buffer: bool = False) -> ApplicationServices:
    """Initialise logging and infrastructure dependencies for the bot."""

    setup_logging(logging.INFO, debug_mode=cfg.DEBUG)
    logger = enforce_context(logging.getLogger("pokerbot"))

    from pokerapp.lock_manager import LockManager as SmartLockManager

    init_translations("config/data/translations.json")

    redis_client_kwargs = _build_redis_client_kwargs(cfg)

    state_validator = GameStateValidator()

    recovery_logger = _make_service_logger(logger, "recovery", "recovery")
    recovery_redis = _create_redis_client(redis_client_kwargs)
    recovery_ops = RedisSafeOps(
        recovery_redis,
        logger=_make_service_logger(logger, "redis_safeops_recovery", "redis"),
    )
    recovery_table_manager = TableManager(
        recovery_redis,
        redis_ops=recovery_ops,
        wallet_redis_ops=recovery_ops,
        state_validator=state_validator,
    )
    recovery_service = RecoveryService(
        redis=recovery_redis,
        table_manager=recovery_table_manager,
        logger=recovery_logger,
    )
    try:
        asyncio.run(recovery_service.run_startup_recovery())
    except RuntimeError:
        recovery_logger.warning(
            "Skipping startup recovery; event loop already running",
            extra={"event_type": "startup_recovery_skipped"},
        )
    except Exception:
        recovery_logger.exception("Startup recovery failed")
    finally:
        try:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    recovery_redis.aclose(close_connection_pool=True)
                )
                recovery_logger.debug(
                    "Scheduled async Redis cleanup",
                    extra={"event_type": "recovery_redis_close_scheduled"},
                )
            except RuntimeError:
                asyncio.run(recovery_redis.aclose(close_connection_pool=True))
                recovery_logger.debug(
                    "Completed sync Redis cleanup",
                    extra={"event_type": "recovery_redis_close_completed"},
                )
        except Exception as exc:
            recovery_logger.warning(
                f"Redis cleanup failed: {exc}",
                extra={
                    "event_type": "recovery_redis_close_error",
                    "error_type": type(exc).__name__,
                },
            )

    kv_async = _create_redis_client(redis_client_kwargs)
    redis_pool = kv_async
    logger.info(
        "Redis client initialized with lazy connection",
        extra={
            "event_type": "redis_client_created",
            "host": cfg.REDIS_HOST,
            "port": cfg.REDIS_PORT,
            "health_check_interval": 30,
        },
    )

    smart_lock_manager = SmartLockManager(
        logger=logger.logger.getChild("smart_lock_manager"),
        redis_pool=redis_pool,
    )

    cache_logger = _make_service_logger(logger, "cache", "cache")
    cache_config = CacheConfig(
        l1_ttl_seconds=cfg.CACHE_L1_TTL,
        l1_max_size=cfg.CACHE_L1_MAX_SIZE,
        l2_ttl_seconds=cfg.CACHE_L2_TTL,
        enable_l1=cfg.CACHE_L1_ENABLED,
        enable_l2=cfg.CACHE_L2_ENABLED,
        key_prefix=cfg.CACHE_KEY_PREFIX,
        default_ttl_seconds=cfg.CACHE_DEFAULT_TTL,
    )
    cache = MultiLayerCache(kv_async, config=cache_config, logger=cache_logger)

    db_client: Optional[OptimizedDatabaseClient] = None
    query_batcher: Optional[QueryBatcher] = None
    if cfg.DATABASE_POOL_DSN:
        cache_enabled = cfg.CACHE_L1_ENABLED or cfg.CACHE_L2_ENABLED
        cache_policy = CachePolicy(
            ttl_seconds=cfg.CACHE_DEFAULT_TTL,
            category="player_stats",
            enabled=cache_enabled,
        )
        db_logger = _make_service_logger(logger, "db_client", "database")
        db_client = OptimizedDatabaseClient(
            cfg.DATABASE_POOL_DSN,
            min_size=cfg.DB_POOL_MIN_SIZE,
            max_size=cfg.DB_POOL_MAX_SIZE,
            cache=cache if cache_enabled else None,
            cache_policy=cache_policy,
            logger=db_logger,
            command_timeout=cfg.DB_COMMAND_TIMEOUT,
        )
        query_batcher = QueryBatcher(
            db_client,
            batch_window_ms=cfg.QUERY_BATCH_WINDOW_MS,
            logger=_make_service_logger(logger, "query_batcher", "database"),
        )

    redis_ops = RedisSafeOps(
        kv_async,
        logger=_make_service_logger(logger, "redis_safeops", "redis"),
    )

    table_manager = TableManager(
        kv_async,
        redis_ops=redis_ops,
        wallet_redis_ops=redis_ops,
        state_validator=state_validator,
    )

    retry_manager = TelegramRetryManager(
        max_retries=cfg.TELEGRAM_MAX_RETRIES,
        base_delay=cfg.TELEGRAM_RETRY_BASE_DELAY,
        max_delay=cfg.TELEGRAM_RETRY_MAX_DELAY,
        logger=logger,
    )

    stats_logger = _make_service_logger(logger, "stats", "stats")
    stats_service = _build_stats_service(stats_logger, cfg)
    stats_buffer: Optional[StatsBatchBuffer] = None

    adaptive_player_report_cache = AdaptivePlayerReportCache(
        default_ttl=cfg.PLAYER_REPORT_TTL_DEFAULT,
        bonus_ttl=cfg.PLAYER_REPORT_TTL_BONUS,
        post_hand_ttl=cfg.PLAYER_REPORT_TTL_POST_HAND,
        logger_=_make_service_logger(
            logger, "adaptive_player_report_cache", "player_report_cache"
        ),
        persistent_store=redis_ops,
    )
    stats_service.bind_player_report_cache(adaptive_player_report_cache)

    if isinstance(stats_service, StatsService) and not skip_stats_buffer:
        stats_buffer = StatsBatchBuffer(
            session_maker=getattr(stats_service, "_sessionmaker", None),
            flush_callback=stats_service._flush_hand_batch_records,
            config=_SYSTEM_CONSTANTS,
        )
        stats_service.attach_buffer(stats_buffer)
    elif skip_stats_buffer and isinstance(stats_service, StatsService):
        stats_logger.info(
            "Statistics batch buffer disabled via configuration",
            extra={"event_type": "stats_buffer_disabled"},
        )

    player_report_cache = PlayerReportCache(
        redis_ops,
        logger=_make_service_logger(
            logger, "shared_player_report_cache", "player_report_cache"
        ),
    )

    request_metrics = RequestMetrics(
        logger_=_make_service_logger(logger, "metrics", "metrics")
    )

    private_match_service = PrivateMatchService(
        kv_async,
        table_manager,
        logger=_make_service_logger(logger, "private_match", "private_match"),
        constants=cfg.constants,
        redis_ops=redis_ops,
    )

    def messaging_service_factory(
        *,
        bot,
        deleted_messages: Set[int],
        deleted_messages_lock,
        last_message_hash: Dict[int, str],
        last_message_hash_lock,
        cache_ttl: int = 3,
        cache_maxsize: int = 500,
    ) -> MessagingService:
        return MessagingService(
            bot,
            cache_ttl=cache_ttl,
            cache_maxsize=cache_maxsize,
            logger_=_make_service_logger(logger, "messaging_service", "messaging"),
            request_metrics=request_metrics,
            deleted_messages=deleted_messages,
            deleted_messages_lock=deleted_messages_lock,
            last_message_hash=last_message_hash,
            last_message_hash_lock=last_message_hash_lock,
            table_manager=table_manager,
            retry_manager=retry_manager,
        )

    def telegram_safeops_factory(*, view) -> TelegramSafeOps:
        return TelegramSafeOps(
            view,
            logger=_make_service_logger(
                logger, "telegram_safeops", "telegram_safeops"
            ),
            max_retries=cfg.TELEGRAM_MAX_RETRIES,
            base_delay=cfg.TELEGRAM_RETRY_BASE_DELAY,
            max_delay=cfg.TELEGRAM_RETRY_MAX_DELAY,
            backoff_multiplier=cfg.TELEGRAM_RETRY_MULTIPLIER,
        )

    return ApplicationServices(
        logger=logger,
        kv_async=kv_async,
        redis_ops=redis_ops,
        table_manager=table_manager,
        stats_service=stats_service,
        player_report_cache=player_report_cache,
        adaptive_player_report_cache=adaptive_player_report_cache,
        request_metrics=request_metrics,
        private_match_service=private_match_service,
        messaging_service_factory=messaging_service_factory,
        telegram_safeops_factory=telegram_safeops_factory,
        retry_manager=retry_manager,
        stats_buffer=stats_buffer,
        cache=cache,
        db_client=db_client,
        query_batcher=query_batcher,
        smart_lock_manager=smart_lock_manager,
    )

