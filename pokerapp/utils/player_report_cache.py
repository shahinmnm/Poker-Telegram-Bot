"""Redis-backed cache for aggregated player statistics reports."""

from __future__ import annotations

import json
import logging
from typing import Iterable, Optional, Sequence

from pokerapp.utils.redis_safeops import RedisSafeOps


class PlayerReportCache:
    """Persist player statistics summaries in Redis with TTL handling."""

    def __init__(
        self,
        redis_ops: RedisSafeOps,
        *,
        key_prefix: str = "pokerbot:player_report:",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        base_logger = logger or logging.getLogger(__name__)
        self._logger = base_logger.getChild("player_report_cache")
        self._redis_ops = redis_ops
        self._key_prefix = key_prefix.rstrip(":") + ":"

    @staticmethod
    def _normalize_user_id(user_id: int) -> int:
        try:
            return int(user_id)
        except (TypeError, ValueError):
            return 0

    def _redis_key(self, user_id: int) -> str:
        return f"{self._key_prefix}{self._normalize_user_id(user_id)}"

    async def get_report(self, user_id: int) -> Optional[dict]:
        """Return a cached report for ``user_id`` if it exists."""

        normalized_id = self._normalize_user_id(user_id)
        key = self._redis_key(normalized_id)
        try:
            payload = await self._redis_ops.safe_get(
                key,
                log_extra={"user_id": normalized_id, "ttl": None},
            )
        except Exception:
            self._logger.exception(
                "Failed to load player report from Redis",
                extra={"user_id": normalized_id, "ttl": None},
            )
            return None

        if not payload:
            self._logger.debug(
                "Player report cache miss",
                extra={"user_id": normalized_id, "ttl": None},
            )
            return None

        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self._logger.warning(
                "Invalid JSON payload encountered when loading player report",
                extra={"user_id": normalized_id, "ttl": None},
            )
            return None

        if not isinstance(data, dict):
            self._logger.debug(
                "Discarded non-dict cached player report",
                extra={"user_id": normalized_id, "ttl": None},
            )
            return None

        self._logger.debug(
            "Player report cache hit",
            extra={"user_id": normalized_id, "ttl": None},
        )
        return data

    async def set_report(self, user_id: int, data: dict, ttl_seconds: int) -> bool:
        """Store ``data`` for ``user_id`` using ``ttl_seconds`` for expiration."""

        normalized_id = self._normalize_user_id(user_id)
        ttl = max(int(ttl_seconds or 0), 0)
        key = self._redis_key(normalized_id)
        try:
            payload = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            self._logger.warning(
                "Failed to serialise player report for caching",
                extra={"user_id": normalized_id, "ttl": ttl},
            )
            return False

        try:
            result = await self._redis_ops.safe_set(
                key,
                payload,
                expire=ttl or None,
                log_extra={"user_id": normalized_id, "ttl": ttl},
            )
        except Exception:
            self._logger.exception(
                "Failed to persist player report in Redis",
                extra={"user_id": normalized_id, "ttl": ttl},
            )
            return False

        self._logger.debug(
            "Player report stored",
            extra={"user_id": normalized_id, "ttl": ttl},
        )
        return bool(result)

    async def invalidate(self, user_ids: Iterable[int]) -> int:
        """Remove cached reports for the provided ``user_ids``."""

        normalized: Sequence[int] = [
            self._normalize_user_id(user_id)
            for user_id in user_ids
            if self._normalize_user_id(user_id)
        ]
        if not normalized:
            return 0

        keys = [self._redis_key(user_id) for user_id in normalized]
        try:
            removed = await self._redis_ops.safe_delete(
                *keys,
                log_extra={"user_id": list(normalized), "ttl": None},
            )
        except Exception:
            self._logger.exception(
                "Failed to invalidate player reports in Redis",
                extra={"user_id": list(normalized), "ttl": None},
            )
            return 0

        self._logger.debug(
            "Invalidated player reports",
            extra={"user_id": list(normalized), "ttl": None},
        )
        return int(removed)
