"""Tests for database bootstrap helpers."""

from __future__ import annotations

import logging

import pytest

from pokerapp.bootstrap import _has_migrations_applied
from pokerapp.utils.logging_helpers import ContextLoggerAdapter, DEFAULT_LOG_CONTEXT


class _DummyResult:
    def __init__(self, value: int):
        self._value = value

    def scalar(self) -> int:
        return self._value


class _DummyAsyncConnection:
    def __init__(self, dialect_name: str, *, scalar_value: int = 1):
        self.dialect = type("_DummyDialect", (), {"name": dialect_name})()
        self._scalar_value = scalar_value
        self.executed_sql: list[str] = []
        self.sync_checked = False

    async def execute(self, statement):
        self.executed_sql.append(str(statement))
        return _DummyResult(self._scalar_value)

    async def run_sync(self, fn):
        self.sync_checked = True
        return fn(object())


@pytest.mark.asyncio
async def test_has_migrations_applied_postgres_metadata_query():
    """The helper should use information_schema on PostgreSQL and succeed."""

    conn = _DummyAsyncConnection("postgresql")
    logger = ContextLoggerAdapter(logging.getLogger(__name__), DEFAULT_LOG_CONTEXT)

    result = await _has_migrations_applied(conn, logger)

    assert result is True
    assert conn.sync_checked is False
    executed_sql = " ".join(conn.executed_sql)
    assert "information_schema.tables" in executed_sql
