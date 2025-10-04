"""Application composition root for the Poker Telegram bot."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set

import redis.asyncio as aioredis

try:  # pragma: no cover - optional dependency
    from psycopg_pool import AsyncConnectionPool
except Exception:  # pragma: no cover - optional dependency missing
    AsyncConnectionPool = None  # type: ignore[assignment]

from pokerapp.config import Config, _SYSTEM_CONSTANTS
from pokerapp.feature_flags import FeatureFlagManager
from pokerapp.logging_config import setup_logging
from pokerapp.stats import BaseStatsService, NullStatsService, StatsService
from pokerapp.stats.buffer import StatsBatchBuffer
from pokerapp.table_manager import TableManager
from pokerapp.private_match_service import PrivateMatchService
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
from pokerapp.utils.rollout_metrics import RolloutMonitor


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
    feature_flags: FeatureFlagManager
    rollout_monitor: Optional[RolloutMonitor]
    db_pool: Optional[AsyncConnectionPool]


def _build_stats_service(logger: ContextLoggerAdapter, cfg: Config) -> BaseStatsService:
    if not cfg.DATABASE_URL:
        return NullStatsService(timezone_name=cfg.TIMEZONE_NAME)

    try:
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


def build_services(cfg: Config, *, skip_stats_buffer: bool = False) -> ApplicationServices:
    """Initialise logging and infrastructure dependencies for the bot."""

    setup_logging(logging.INFO, debug_mode=cfg.DEBUG)
    logger = enforce_context(logging.getLogger("pokerbot"))

    feature_flags = FeatureFlagManager(
        config=cfg,
        logger=logger.getChild("feature_flags"),
    )

    init_translations("config/data/translations.json")

    kv_async = aioredis.Redis(
        host=cfg.REDIS_HOST,
        port=cfg.REDIS_PORT,
        db=cfg.REDIS_DB,
        password=cfg.REDIS_PASS or None,
    )

    redis_ops = RedisSafeOps(
        kv_async,
        logger=_make_service_logger(logger, "redis_safeops", "redis"),
    )

    state_validator = GameStateValidator()
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

    recovery_logger = _make_service_logger(logger, "recovery", "recovery")
    recovery_service = RecoveryService(
        redis=kv_async,
        table_manager=table_manager,
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

    db_pool: Optional[AsyncConnectionPool] = None
    rollout_monitor: Optional[RolloutMonitor] = None
    database_url = getattr(cfg, "DATABASE_URL", "")
    if (
        AsyncConnectionPool is not None
        and isinstance(database_url, str)
        and database_url.startswith("postgresql")
    ):
        dsn = database_url
        if "+asyncpg" in dsn:
            dsn = dsn.replace("+asyncpg", "")
        if "+psycopg" in dsn:
            dsn = dsn.replace("+psycopg", "")
        try:
            db_pool = AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False)
            try:
                asyncio.run(db_pool.open())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(db_pool.open())
                finally:
                    loop.close()
            rollout_monitor = RolloutMonitor(
                db_pool=db_pool,
                feature_flags=feature_flags,
                logger=logger.getChild("rollout_monitor"),
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Failed to initialise rollout monitor")

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
        feature_flags=feature_flags,
        rollout_monitor=rollout_monitor,
        db_pool=db_pool,
    )

