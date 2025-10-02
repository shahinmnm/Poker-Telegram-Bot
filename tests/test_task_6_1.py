import asyncio
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import NetworkError, RetryAfter, TimedOut

from pokerapp.entities import Game
from pokerapp.table_manager import TableManager
from pokerapp.telegram_retry_manager import TelegramRetryManager


class TestTelegramRetryManager:
    @pytest.fixture
    def retry_manager(self) -> TelegramRetryManager:
        return TelegramRetryManager(max_retries=3, base_delay=0.1, max_delay=1.0)

    @pytest.mark.asyncio
    async def test_retry_on_network_error(self, retry_manager: TelegramRetryManager):
        call_count = 0

        @retry_manager.retry_telegram_call("test_op", critical=False)
        async def flaky_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise NetworkError("temporary outage")
            return "success"

        result = await flaky_call()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_respect_retry_after(
        self, retry_manager: TelegramRetryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: List[float] = []
        sleep_mock = AsyncMock(side_effect=lambda duration: sleep_calls.append(duration))
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)

        call_count = 0

        @retry_manager.retry_telegram_call("test_op", critical=False)
        async def rate_limited_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RetryAfter(5.0)
            return "success"

        result = await rate_limited_call()

        assert result == "success"
        assert sleep_calls and sleep_calls[0] == pytest.approx(6.0, rel=1e-3)

    @pytest.mark.asyncio
    async def test_critical_raises_on_exhaustion(self, retry_manager: TelegramRetryManager) -> None:
        @retry_manager.retry_telegram_call("test_op", critical=True)
        async def always_fails() -> None:
            raise NetworkError("Always fails")

        with pytest.raises(NetworkError):
            await always_fails()

    @pytest.mark.asyncio
    async def test_non_critical_returns_none(self, retry_manager: TelegramRetryManager) -> None:
        @retry_manager.retry_telegram_call("test_op", critical=False)
        async def always_times_out() -> None:
            raise TimedOut("timeout")

        result = await always_times_out()
        assert result is None


class TestOptimisticLocking:
    @pytest.fixture
    def redis_mock(self) -> AsyncMock:
        mock = AsyncMock()
        mock.get = AsyncMock(return_value=None)
        mock.set = AsyncMock()
        mock.pipeline = MagicMock()
        return mock

    @pytest.fixture
    def table_manager(self, redis_mock: AsyncMock) -> TableManager:
        redis_ops = MagicMock()
        redis_ops.safe_get = AsyncMock()
        redis_ops.safe_set = AsyncMock()
        redis_ops.safe_delete = AsyncMock()
        redis_ops.safe_mset = AsyncMock()
        redis_ops.safe_smembers = AsyncMock(return_value=set())
        redis_ops.safe_sadd = AsyncMock()
        redis_ops._logger = MagicMock()

        manager = TableManager(
            redis_mock,
            redis_ops=redis_ops,
            wallet_redis_ops=redis_ops,
            state_validator=None,
        )
        manager._redis = redis_mock
        manager._redis_ops = redis_ops
        manager._logger = MagicMock()
        return manager

    @pytest.mark.asyncio
    async def test_load_with_version_initializes(self, table_manager: TableManager, redis_mock: AsyncMock) -> None:
        table_manager.load_game = AsyncMock(return_value=(Game(), None))
        redis_mock.get = AsyncMock(return_value=None)

        game, version = await table_manager.load_game_with_version(123)

        assert isinstance(game, Game)
        assert version == 0
        redis_mock.set.assert_awaited()

    @pytest.mark.asyncio
    async def test_save_increments_version(self, table_manager: TableManager, redis_mock: AsyncMock) -> None:
        game = Game()

        pipe_mock = AsyncMock()
        pipe_mock.watch = AsyncMock()
        pipe_mock.get = AsyncMock(return_value=b"5")
        pipe_mock.unwatch = AsyncMock()
        pipe_mock.multi = MagicMock()
        pipe_mock.set = MagicMock()
        pipe_mock.execute = AsyncMock()
        pipe_mock.__aenter__.return_value = pipe_mock
        pipe_mock.__aexit__.return_value = False

        redis_mock.pipeline.return_value = pipe_mock
        table_manager._update_player_index = AsyncMock()

        success = await table_manager.save_game_with_version_check(123, game, 5)

        assert success is True
        version_key = table_manager._version_key(123)
        assert any(
            call.args[0] == version_key and call.args[1] == 6
            for call in pipe_mock.set.call_args_list
        )
        table_manager._update_player_index.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_version_conflict_returns_false(
        self, table_manager: TableManager, redis_mock: AsyncMock
    ) -> None:
        game = Game()

        pipe_mock = AsyncMock()
        pipe_mock.watch = AsyncMock()
        pipe_mock.get = AsyncMock(return_value=b"10")
        pipe_mock.unwatch = AsyncMock()
        pipe_mock.__aenter__.return_value = pipe_mock
        pipe_mock.__aexit__.return_value = False

        redis_mock.pipeline.return_value = pipe_mock

        success = await table_manager.save_game_with_version_check(123, game, 5)

        assert success is False
        pipe_mock.unwatch.assert_awaited_once()
        pipe_mock.execute.assert_not_awaited()
