"""Application composition root for the Poker Telegram bot."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Set

import redis.asyncio as aioredis

from pokerapp.config import Config
from pokerapp.logging_config import setup_logging
from pokerapp.stats import BaseStatsService, NullStatsService, StatsService
from pokerapp.table_manager import TableManager
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.redis_safeops import RedisSafeOps
from pokerapp.utils.request_metrics import RequestMetrics
from pokerapp.utils.player_report_cache import PlayerReportCache


@dataclass(frozen=True)
class ApplicationServices:
    """Container for infrastructure dependencies shared across the bot."""

    logger: logging.Logger
    kv_async: aioredis.Redis
    redis_ops: RedisSafeOps
    table_manager: TableManager
    stats_service: BaseStatsService
    player_report_cache: PlayerReportCache
    request_metrics: RequestMetrics
    private_match_service: PrivateMatchService
    messaging_service_factory: Callable[..., MessagingService]


def _build_stats_service(logger: logging.Logger, cfg: Config) -> BaseStatsService:
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


def build_services(cfg: Config) -> ApplicationServices:
    """Initialise logging and infrastructure dependencies for the bot."""

    setup_logging(logging.INFO, debug_mode=cfg.DEBUG)
    logger = logging.getLogger("pokerbot")

    kv_async = aioredis.Redis(
        host=cfg.REDIS_HOST,
        port=cfg.REDIS_PORT,
        db=cfg.REDIS_DB,
        password=cfg.REDIS_PASS or None,
    )

    redis_ops = RedisSafeOps(kv_async, logger=logger.getChild("redis_safeops"))

    table_manager = TableManager(
        kv_async,
        redis_ops=redis_ops,
        wallet_redis_ops=redis_ops,
    )

    stats_service = _build_stats_service(logger.getChild("stats"), cfg)

    player_report_cache = PlayerReportCache(
        redis_ops,
        logger=logger,
    )

    request_metrics = RequestMetrics(logger_=logger.getChild("metrics"))

    private_match_service = PrivateMatchService(
        kv_async,
        table_manager,
        logger=logger.getChild("private_match"),
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
            logger_=logger.getChild("messaging_service"),
            request_metrics=request_metrics,
            deleted_messages=deleted_messages,
            deleted_messages_lock=deleted_messages_lock,
            last_message_hash=last_message_hash,
            last_message_hash_lock=last_message_hash_lock,
        )

    return ApplicationServices(
        logger=logger,
        kv_async=kv_async,
        redis_ops=redis_ops,
        table_manager=table_manager,
        stats_service=stats_service,
        player_report_cache=player_report_cache,
        request_metrics=request_metrics,
        private_match_service=private_match_service,
        messaging_service_factory=messaging_service_factory,
    )

