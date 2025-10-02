"""
Telegram API retry manager with exponential backoff.

Provides decorators to wrap Telegram API calls with automatic retry logic,
handling transient network errors and rate limiting.
"""

from __future__ import annotations

from functools import wraps
import asyncio
from typing import TypeVar, Callable, Optional, Any, Awaitable
from telegram.error import RetryAfter, TimedOut, NetworkError
import logging

T = TypeVar("T")


class TelegramRetryManager:
    """Manage retry logic for Telegram API calls with exponential backoff."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.logger = logger or logging.getLogger(__name__)

    def retry_telegram_call(
        self,
        operation_name: str,
        critical: bool = False,
    ) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[Optional[T]]]]:
        """Decorator returning a coroutine wrapper with retry logic."""

        def decorator(
            func: Callable[..., Awaitable[T]]
        ) -> Callable[..., Awaitable[Optional[T]]]:
            @wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Optional[T]:
                max_attempts = self.max_retries * 2 if critical else self.max_retries

                for attempt in range(1, max_attempts + 1):
                    try:
                        result = await func(*args, **kwargs)
                        if attempt > 1:
                            self.logger.info(
                                "Telegram %s succeeded after retry", operation_name,
                                extra={
                                    "attempt": attempt,
                                    "operation": operation_name,
                                },
                            )
                        return result
                    except RetryAfter as error:
                        wait_time = float(getattr(error, "retry_after", 0)) + 1.0
                        if attempt < max_attempts:
                            self.logger.warning(
                                "Telegram rate limited, retrying %s in %.1fs",
                                operation_name,
                                wait_time,
                                extra={
                                    "attempt": attempt,
                                    "retry_after": getattr(error, "retry_after", None),
                                    "operation": operation_name,
                                },
                            )
                            await asyncio.sleep(wait_time)
                        else:
                            self._log_final_failure(operation_name, error, attempt, critical)
                            if critical:
                                raise
                            return None
                    except (TimedOut, NetworkError) as error:
                        if attempt < max_attempts:
                            delay = min(
                                self.base_delay * (2 ** (attempt - 1)),
                                self.max_delay,
                            )
                            self.logger.warning(
                                "Telegram %s failed, retrying in %.1fs",
                                operation_name,
                                delay,
                                extra={
                                    "attempt": attempt,
                                    "error": type(error).__name__,
                                    "operation": operation_name,
                                },
                            )
                            await asyncio.sleep(delay)
                        else:
                            self._log_final_failure(operation_name, error, attempt, critical)
                            if critical:
                                raise
                            return None
                    except Exception as error:  # noqa: BLE001 - propagate unexpected errors
                        self.logger.error(
                            "Unexpected error in %s",
                            operation_name,
                            extra={
                                "error": type(error).__name__,
                                "error_message": str(error),
                                "operation": operation_name,
                            },
                            exc_info=True,
                        )
                        raise

                return None

            return wrapper

        return decorator

    def _log_final_failure(
        self,
        operation_name: str,
        error: Exception,
        attempts: int,
        critical: bool,
    ) -> None:
        """Log the final failure after exhausting retry attempts."""

        self.logger.error(
            "Telegram %s failed after %d attempts",
            operation_name,
            attempts,
            extra={
                "operation": operation_name,
                "error": type(error).__name__,
                "error_message": str(error),
                "critical": critical,
                "total_attempts": attempts,
            },
        )
