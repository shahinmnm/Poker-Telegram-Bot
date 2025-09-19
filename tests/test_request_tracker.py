import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pokerapp.utils.request_tracker import RequestTracker


def test_request_tracker_logs_at_threshold(caplog):
    tracker = RequestTracker(limit=10, info_threshold=0.7)
    chat_id = 42
    round_id = "round-1"

    async def _consume_until_threshold():
        with caplog.at_level(logging.INFO):
            for _ in range(6):
                assert await tracker.try_consume(chat_id, round_id, "turn")
            assert await tracker.try_consume(chat_id, round_id, "turn")

    asyncio.run(_consume_until_threshold())

    info_records = [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and getattr(record, "trigger", "") == "threshold"
    ]
    assert len(info_records) == 1
    record = info_records[0]
    assert record.chat_id == chat_id
    assert record.round_id == round_id
    assert record.category == "turn"
    assert record.limit == 10
    assert record.stats["total"] == 7


def test_request_tracker_verbose_logging(monkeypatch, caplog):
    monkeypatch.setenv(RequestTracker.VERBOSE_ENV_VAR, "true")
    tracker = RequestTracker(limit=5, info_threshold=None)
    chat_id = 24
    round_id = "round-verbose"

    async def _consume_verbose():
        with caplog.at_level(logging.INFO):
            assert await tracker.try_consume(chat_id, round_id, "turn")
            assert await tracker.try_consume(chat_id, round_id, "stage")

    asyncio.run(_consume_verbose())

    verbose_records = [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and getattr(record, "trigger", "") == "verbose"
    ]
    assert len(verbose_records) == 2
    totals = [record.stats["total"] for record in verbose_records]
    assert totals == [1, 2]
    assert all(record.limit == 5 for record in verbose_records)
