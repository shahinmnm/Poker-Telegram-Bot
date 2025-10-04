"""Health check endpoints for rollout monitoring."""

from __future__ import annotations

from typing import Dict

from aiohttp import web

from pokerapp.utils.rollout_metrics import RolloutMonitor


async def fine_grained_locks_health(request: web.Request) -> web.Response:
    """Return health status of fine-grained locks rollout."""

    monitor: RolloutMonitor = request.app["rollout_monitor"]

    total_actions = 0
    total_successes = 0
    total_failures = 0
    lock_errors = 0
    total_locks = 0

    for metrics in monitor.iter_metrics():
        total_actions += metrics.action_successes + metrics.action_failures
        total_successes += metrics.action_successes
        total_failures += metrics.action_failures
        lock_errors += metrics.lock_errors
        total_locks += len(metrics.lock_wait_times) + metrics.lock_errors

    error_rate = total_failures / max(1, total_actions)
    lock_error_rate = lock_errors / max(1, total_locks)

    healthy = error_rate < 0.05 and lock_error_rate < 0.01

    payload: Dict[str, object] = {
        "healthy": healthy,
        "metrics": {
            "total_actions": total_actions,
            "success_rate": total_successes / max(1, total_actions),
            "error_rate": error_rate,
            "lock_error_rate": lock_error_rate,
        },
    }

    return web.json_response(payload)


__all__ = ["fine_grained_locks_health"]
