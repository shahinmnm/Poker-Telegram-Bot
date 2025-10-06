#!/usr/bin/env python3
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, TYPE_CHECKING

import redis.asyncio as aioredis
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, JobQueue

from pokerapp.cache_manager import MultiLayerCache
from pokerapp.config import Config
from pokerapp.db_client import OptimizedDatabaseClient
from pokerapp.pokerbotcontrol import PokerBotCotroller
from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.pokerbotview import PokerBotViewer
from pokerapp.query_optimizer import QueryBatcher
from pokerapp.table_manager import TableManager
from pokerapp.stats import BaseStatsService
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.telegram_safeops import TelegramSafeOps
from pokerapp.utils.redis_safeops import RedisSafeOps
from pokerapp.utils.request_metrics import RequestMetrics
from pokerapp.utils.player_report_cache import PlayerReportCache
from pokerapp.utils.cache import AdaptivePlayerReportCache
from pokerapp.utils.logging_helpers import ContextLoggerAdapter, add_context


@dataclass(frozen=True)
class WebhookSettings:
    secret_token: Optional[str]
    max_connections: Optional[int]
    allowed_updates: Optional[Sequence[str]]
    drop_pending_updates: bool = True


if TYPE_CHECKING:
    from telegram.ext import Application


MessagingServiceFactory = Callable[..., MessagingService]
TelegramSafeOpsFactory = Callable[..., TelegramSafeOps]


class PokerBot:
    """Telegram bot wrapper using PTB v20 async Application.

    The bootstrap layer (`main.py`) injects infrastructure singletons such as
    the logger, Redis pool, metrics collector, and stats service. ``PokerBot``
    remains responsible for wiring the Presentation (``PokerBotViewer``) and
    domain model (``PokerBotModel``) around the shared ``Application`` instance.
    See ``docs/game_flow.md`` for the architecture and sequence diagrams that
    map these responsibilities.
    """

    def __init__(
        self,
        token: str,
        cfg: Config,
        *,
        logger: ContextLoggerAdapter,
        kv_async: aioredis.Redis,
        table_manager: TableManager,
        stats_service: BaseStatsService,
        redis_ops: RedisSafeOps,
        cache: Optional[MultiLayerCache] = None,
        db_client: Optional[OptimizedDatabaseClient] = None,
        query_batcher: Optional[QueryBatcher] = None,
        player_report_cache: PlayerReportCache,
        adaptive_player_report_cache: AdaptivePlayerReportCache,
        request_metrics: RequestMetrics,
        private_match_service: PrivateMatchService,
        messaging_service_factory: MessagingServiceFactory,
        telegram_safeops_factory: TelegramSafeOpsFactory,
    ):
        self._cfg = cfg
        self._token = token
        self._logger = add_context(logger)
        self._webhook_settings = WebhookSettings(
            secret_token=cfg.WEBHOOK_SECRET or None,
            max_connections=getattr(
                cfg,
                "WEBHOOK_MAX_CONNECTIONS",
                getattr(cfg, "MAX_CONNECTIONS", None),
            ),
            allowed_updates=cfg.ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        self._application: Optional["Application"] = None
        self._job_queue: Optional[JobQueue] = None
        self._view: Optional[PokerBotViewer] = None
        self._model: Optional[PokerBotModel] = None
        self._controller: Optional[PokerBotCotroller] = None

        self._kv_async = kv_async
        self._redis_ops = redis_ops
        self._table_manager = table_manager
        self._stats_service = stats_service
        self._request_metrics = request_metrics
        self._private_match_service = private_match_service
        self._messaging_service_factory = messaging_service_factory
        self._telegram_safeops_factory = telegram_safeops_factory
        self._player_report_cache = player_report_cache
        self._adaptive_player_report_cache = adaptive_player_report_cache
        self._cache = cache
        self._db_client = db_client
        self._query_batcher = query_batcher
        self._build_application()

    def run(self) -> None:
        """Start the bot using the webhook listener."""
        try:
            self.run_webhook()
        except (TelegramError, OSError) as exc:
            if not self._handle_webhook_start_failure(exc):
                raise
        except Exception as exc:
            if not self._handle_webhook_start_failure(exc):
                raise

    def run_webhook(self) -> None:
        """Start the bot using webhook delivery."""
        if self._application is None:
            self._build_application()
        self._logger.info(
            "Starting webhook listener on %s:%s%s targeting %s",
            self._cfg.WEBHOOK_LISTEN,
            self._cfg.WEBHOOK_PORT,
            self._cfg.WEBHOOK_PATH,
            self._cfg.WEBHOOK_PUBLIC_URL,
        )
        self._schedule_webhook_verification()
        settings = self._webhook_settings
        try:
            self._application.run_webhook(
                listen=self._cfg.WEBHOOK_LISTEN,
                port=self._cfg.WEBHOOK_PORT,
                url_path=self._cfg.WEBHOOK_PATH,
                webhook_url=self._cfg.WEBHOOK_PUBLIC_URL,
                secret_token=settings.secret_token,
                allowed_updates=settings.allowed_updates,
                drop_pending_updates=settings.drop_pending_updates,
                max_connections=settings.max_connections,
            )
        except Exception:
            self._logger.exception("Webhook run terminated due to an error.")
            raise
        finally:
            self._logger.info("Webhook listener stopped.")

    def run_polling(self) -> None:
        """Start the bot using long polling."""
        if self._application is None:
            self._build_application()
        self._logger.info(
            "Starting polling mode for development; webhook configuration will be ignored."
        )
        try:
            self._application.run_polling(
                allowed_updates=self._webhook_settings.allowed_updates,
                drop_pending_updates=self._webhook_settings.drop_pending_updates,
            )
        except Exception:
            self._logger.exception("Polling run terminated due to an error.")
            raise
        finally:
            self._logger.info("Polling stopped.")
            try:
                asyncio.run(self._stats_service.close())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._stats_service.close())
                finally:
                    loop.close()
            except Exception:
                self._logger.exception("Failed to close statistics service after polling stop.")

    def _handle_webhook_start_failure(self, exc: Exception) -> bool:
        """Handle failures when starting the webhook listener.

        Returns True when the failure was handled (e.g., by falling back to
        polling) and False when the caller should re-raise the exception.
        """

        if getattr(self._cfg, "ALLOW_POLLING_FALLBACK", False):
            self._logger.error(
                "Webhook startup failed; falling back to polling mode because "
                "ALLOW_POLLING_FALLBACK is enabled. Error: %s",
                exc,
            )
            self._logger.warning("Using polling mode as a fallback.")
            self._build_application()
            self.run_polling()
            return True

        if self._should_force_polling_due_to_webhook_failure(exc):
            self._logger.error(
                "Webhook startup failed due to a network resolution error; automatically "
                "falling back to polling mode. Error: %s",
                exc,
            )
            self._logger.warning(
                "Automatic polling fallback triggered because the webhook host could not "
                "be resolved. Verify the webhook configuration or provide a reachable "
                "public URL to resume webhook delivery."
            )
            self._build_application()
            self.run_polling()
            return True

        return False

    def _build_application(self) -> None:
        self._dispose_application()

        self._job_queue = JobQueue()
        builder = (
            ApplicationBuilder()
            .token(self._token)
            .post_init(self._on_application_post_init)
            .post_shutdown(self._on_application_post_shutdown)
            .post_stop(self._cleanup_webhook)
            .job_queue(self._job_queue)
        )
        self._application = builder.build()
        self._application.add_error_handler(self._handle_error)

        # Viewer and model are created here so that both receive the injected
        # infrastructure dependencies (Redis, stats, metrics, messaging). The
        # controller then binds Telegram handlers to delegate into the model.
        self._view = PokerBotViewer(
            bot=self._application.bot,
            admin_chat_id=self._cfg.ADMIN_CHAT_ID,
            rate_limit_per_minute=self._cfg.RATE_LIMIT_PER_MINUTE,
            rate_limit_per_second=self._cfg.RATE_LIMIT_PER_SECOND,
            request_metrics=self._request_metrics,
            messaging_service_factory=self._messaging_service_factory,
        )
        telegram_safe_ops = self._telegram_safeops_factory(view=self._view)
        self._model = PokerBotModel(
            view=self._view,
            bot=self._application.bot,
            kv=self._kv_async,
            cfg=self._cfg,
            table_manager=self._table_manager,
            private_match_service=self._private_match_service,
            stats_service=self._stats_service,
            redis_ops=self._redis_ops,
            player_report_cache=self._player_report_cache,
            adaptive_player_report_cache=self._adaptive_player_report_cache,
            telegram_safe_ops=telegram_safe_ops,
            cache=self._cache,
            query_batcher=self._query_batcher,
        )
        self._register_game_engine()
        self._controller = PokerBotCotroller(self._model, self._application)

    def _dispose_application(self) -> None:
        if self._application is None:
            return

        try:
            stop_running = getattr(self._application, "stop_running", None)
            if callable(stop_running):
                stop_running()
        except Exception:
            self._logger.debug("Failed to stop running application cleanly.", exc_info=True)

        try:
            self._application.bot_data.pop("game_engine", None)
        except Exception:
            self._logger.debug("Failed to clear game engine reference from bot_data.", exc_info=True)

        self._application = None
        self._job_queue = None
        self._controller = None
        self._model = None
        self._view = None

    def _resolve_game_engine(self):
        if self._model is None:
            return None
        return getattr(self._model, "_game_engine", None)

    def _register_game_engine(self) -> None:
        if self._application is None:
            return

        game_engine = self._resolve_game_engine()
        if game_engine is None:
            self._logger.warning(
                "Game engine not available for registration; countdown workers will not start.",
            )
            return

        self._application.bot_data["game_engine"] = game_engine

    async def _on_application_post_init(self, application: "Application") -> None:
        game_engine = self._resolve_game_engine()
        if game_engine is None:
            self._logger.warning(
                "Cannot start game engine workers: engine reference missing during post_init.",
            )
            return

        application.bot_data["game_engine"] = game_engine
        try:
            await game_engine.start()
        except Exception:
            self._logger.exception("Failed to start GameEngine background workers")
            raise

    async def _on_application_post_shutdown(self, application: "Application") -> None:
        game_engine = application.bot_data.get("game_engine") or self._resolve_game_engine()
        if game_engine is None:
            return

        try:
            await game_engine.shutdown()
        except Exception:
            self._logger.exception("Failed to stop GameEngine background workers")
        finally:
            application.bot_data.pop("game_engine", None)

    def _should_force_polling_due_to_webhook_failure(self, exc: Exception) -> bool:
        for error in self._iter_exception_chain(exc):
            message = str(error).lower()
            if any(
                keyword in message
                for keyword in (
                    "failed to resolve host",
                    "name or service not known",
                    "temporary failure in name resolution",
                    "getaddrinfo failed",
                )
            ):
                return True
        return False

    @staticmethod
    def _iter_exception_chain(exc: Exception):
        seen = set()
        stack = [exc]
        while stack:
            current = stack.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            yield current
            stack.append(getattr(current, "__cause__", None))
            stack.append(getattr(current, "__context__", None))

    def _schedule_webhook_verification(self) -> None:
        try:
            self._application.job_queue.run_once(
                self._webhook_verification_job,
                when=1.0,
                name="webhook-verification",
            )
        except Exception:
            self._logger.exception("Unable to schedule webhook verification job.")

    async def _webhook_verification_job(
        self, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._verify_webhook_registration()

    async def _verify_webhook_registration(self) -> None:
        try:
            webhook_info = await self._application.bot.get_webhook_info()
        except Exception:
            self._logger.exception("Unable to confirm webhook registration with Telegram.")
            return

        expected_url = self._cfg.WEBHOOK_PUBLIC_URL or ""
        if webhook_info.url == expected_url:
            self._logger.info("Webhook registered at expected URL %s", webhook_info.url)
        else:
            self._logger.warning(
                "Webhook URL mismatch: expected %s, got %s",
                expected_url,
                webhook_info.url,
            )

        settings = self._webhook_settings
        expected_secret = settings.secret_token or ""
        registered_secret = getattr(webhook_info, "secret_token", None)
        if expected_secret and not registered_secret:
            self._logger.info(
                "Webhook secret token not returned by Telegram; configured secret not verifiable."
            )
        elif expected_secret and registered_secret == expected_secret:
            self._logger.info("Webhook secret token matches configured value.")
        elif expected_secret and registered_secret != expected_secret:
            self._logger.warning("Webhook secret token does not match the configured value.")
        elif not expected_secret and registered_secret:
            self._logger.warning("Webhook secret token set unexpectedly.")
        else:
            self._logger.info("Webhook secret token is not configured, as expected.")

        registered_allowed_updates = (
            tuple(webhook_info.allowed_updates)
            if getattr(webhook_info, "allowed_updates", None)
            else ()
        )
        expected_allowed_updates = tuple(settings.allowed_updates or ())
        if expected_allowed_updates == registered_allowed_updates:
            self._logger.info(
                "Webhook allowed updates match configured values: %s",
                registered_allowed_updates,
            )
        else:
            self._logger.warning(
                "Webhook allowed updates mismatch: expected %s, got %s",
                expected_allowed_updates,
                registered_allowed_updates,
            )

        registered_max_connections = getattr(webhook_info, "max_connections", None)
        if settings.max_connections is None:
            self._logger.info(
                "Webhook max_connections not configured; accepting Telegram-reported value: %s",
                registered_max_connections,
            )
        elif registered_max_connections is None:
            self._logger.info(
                "Webhook max_connections not reported by Telegram; configured value is %s",
                settings.max_connections,
            )
        elif settings.max_connections == registered_max_connections:
            self._logger.info(
                "Webhook max_connections matches configured value: %s",
                registered_max_connections,
            )
        else:
            self._logger.warning(
                "Webhook max_connections mismatch: expected %s, got %s",
                settings.max_connections,
                registered_max_connections,
            )

        self._logger.info(
            "Webhook configured to drop pending updates: %s",
            settings.drop_pending_updates,
        )

    async def _cleanup_webhook(self, application: "Application") -> None:
        drop_updates = bool(self._webhook_settings.drop_pending_updates)
        self._logger.info("Removing webhook; drop_pending_updates=%s", drop_updates)
        try:
            await application.bot.delete_webhook(drop_pending_updates=drop_updates)
        except Exception:
            self._logger.exception("Failed to delete webhook during shutdown.")
            return

        try:
            webhook_info = await application.bot.get_webhook_info()
        except Exception:
            self._logger.exception("Unable to verify webhook removal.")
            return

        if webhook_info.url:
            self._logger.warning(
                "Webhook still registered at %s after deletion attempt.",
                webhook_info.url,
            )
        else:
            self._logger.info("Webhook successfully removed from Telegram.")

        try:
            await self._stats_service.close()
        except Exception:
            self._logger.exception("Failed to close statistics service cleanly.")

    async def _handle_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        error = getattr(context, "error", None)
        if isinstance(error, BaseException):
            self._logger.error(
                "Error while processing update %s", getattr(update, "update_id", update),
                exc_info=error,
            )
        else:
            self._logger.error(
                "Error while processing update %s with payload %s",
                getattr(update, "update_id", update),
                error,
            )
