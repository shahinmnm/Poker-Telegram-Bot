"""Centralised Prometheus metric definitions for the poker application.

This module intentionally performs the ``prometheus_client`` imports lazily in
order to avoid hard dependencies during testing.  All counters and histograms
are safe no-op stand-ins when ``prometheus_client`` is not installed.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - Optional dependency in some execution environments
    from prometheus_client import Counter, Histogram
except Exception:  # pragma: no cover - provide lightweight fallbacks for tests

    class _Metric:  # type: ignore[override]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def labels(self, *_args: Any, **_kwargs: Any) -> "_Metric":
            return self

        def inc(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def observe(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    Counter = Histogram = _Metric  # type: ignore[misc, assignment]


WALLET_RESERVE_COUNTER = Counter(
    "poker_wallet_reserve_total",
    "Total number of wallet reservations initiated",
    labelnames=["status"],
)

WALLET_COMMIT_COUNTER = Counter(
    "poker_wallet_commit_total",
    "Total number of wallet reservation commits",
    labelnames=["status"],
)

WALLET_ROLLBACK_COUNTER = Counter(
    "poker_wallet_rollback_total",
    "Total number of wallet reservation rollbacks",
    labelnames=["status"],
)

WALLET_DLQ_COUNTER = Counter(
    "poker_wallet_dlq_total",
    "Total number of failed refunds routed to the wallet DLQ",
)

WALLET_OPERATION_DURATION = Histogram(
    "poker_wallet_operation_duration_seconds",
    "Latency distribution for wallet operations",
    labelnames=["operation"],
)

ACTION_DURATION = Histogram(
    "poker_action_duration_seconds",
    "Latency distribution for player betting actions",
    labelnames=["action"],
)

