"""Feature flag manager for gradual rollout of fine-grained locks."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from pokerapp.config import Config


class FeatureFlagManager:
    """Control fine-grained lock rollout with percentage-based targeting."""

    def __init__(self, config: Config, logger: logging.Logger):
        self._config = config
        self._logger = logger
        self._rollout_percentage = 0
        self._enabled = False
        self._load_config()

    def _load_config(self) -> None:
        """Load rollout configuration from system constants."""

        system_constants = getattr(self._config, "system_constants", None)
        if isinstance(system_constants, dict):
            lock_config = system_constants.get("lock_manager", {})
        else:
            lock_config = {}
        self._enabled = lock_config.get("enable_fine_grained_locks", False)
        self._rollout_percentage = lock_config.get("rollout_percentage", 0)

        self._logger.info(
            "Feature flags loaded",
            extra={
                "fine_grained_locks_enabled": self._enabled,
                "rollout_percentage": self._rollout_percentage,
            },
        )

    @property
    def rollout_percentage(self) -> int:
        """Return the currently configured rollout percentage."""

        return self._rollout_percentage

    def is_enabled_for_chat(self, chat_id: int) -> bool:
        """Determine if fine-grained locks are enabled for this chat.

        Uses deterministic hashing to ensure:
        - Same chat always gets same result
        - Percentage controls rollout (0 = disabled, 100 = all chats)
        """

        if not self._enabled:
            return False

        if self._rollout_percentage >= 100:
            return True

        if self._rollout_percentage <= 0:
            return False

        chat_hash = hashlib.sha256(str(chat_id).encode()).hexdigest()
        chat_bucket = int(chat_hash[:8], 16) % 100

        return chat_bucket < self._rollout_percentage

    async def reload_config(self) -> None:
        """Hot-reload configuration without restart."""

        await asyncio.to_thread(self._config.reload_system_constants)
        old_percentage = self._rollout_percentage
        self._load_config()

        if old_percentage != self._rollout_percentage:
            self._logger.info(
                "Rollout percentage updated",
                extra={
                    "old_percentage": old_percentage,
                    "new_percentage": self._rollout_percentage,
                },
            )


__all__ = ["FeatureFlagManager"]
