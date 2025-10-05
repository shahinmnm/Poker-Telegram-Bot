# Smart Lock Retry Design

## Overview
The smart retry mechanism smooths player experience during periods of table lock contention. When an action cannot immediately acquire the table write lock, the handler now:

1. Samples the queue depth via Redis (`lock:queue:{chat_id}`) through the new `LockManager.get_lock_queue_depth` helper.
2. Estimates the expected wait using `LockManager.estimate_wait_time` heuristics.
3. Compares the estimate with the reservation expiry window and configurable thresholds.
4. Applies an exponential backoff sequence (1s, 2s, 4s, 8s) with a maximum of three retries.
5. Emits dedicated Prometheus metrics to track retry behaviour, queue depth and observed wait times.

## Retry Decision Matrix
| Condition | Outcome |
|-----------|---------|
| Queue depth greater than threshold (default 5) | Abort immediately with a “table very busy” error |
| Estimated wait exceeds remaining reservation lifetime | Abort and roll back with a reservation expiry message |
| Estimated wait exceeds patience threshold (default 25s) | Abort and advise user to retry later |
| Reservation has less than the grace window (default 30s) remaining after expected wait | Abort early |
| None of the above | Retry with exponential backoff |

The retry policy is sourced from `config/system_constants.json` (`lock_retry` section) and can be overridden per instance when instantiating `BettingHandler`.

## Metrics
Three Prometheus series provide visibility into contention:

- `poker_lock_retry_total{outcome}` – counts successes, abandonments, timeouts and exhausted retries.
- `poker_lock_queue_depth` – histogram of sampled queue depth while retrying.
- `poker_lock_wait_duration_seconds` – histogram of wait durations per attempt (successful or otherwise).

These metrics allow Grafana dashboards and alerting (see PromQL recommendations in the feature brief) to quantify contention hot spots.

## Reservation Safety
The handler stores the reservation start time locally and derives the remaining TTL using the wallet service’s configured reservation lifetime. Before each retry it ensures:

- The operation can complete before the reservation expires.
- A 30 second grace buffer remains to cover downstream commit/save operations.

If either condition fails the reservation is rolled back with a descriptive reason so the wallet ledger remains consistent.

## Concurrency Guarantees
A dedicated test suite (`tests/test_smart_lock_retry.py`) stress tests the handler under concurrent access. Scripted lock managers simulate redis-backed queue depths, starvation and sequential release to guarantee that:

- Retries back off without deadlocking.
- Queue depth heuristics trigger fail-fast behaviour when contention is extreme.
- All reservations either commit or roll back deterministically.

## Configuration
```json
"lock_retry": {
  "max_attempts": 3,
  "backoff_delays_seconds": [1, 2, 4, 8],
  "queue_depth_threshold": 5,
  "estimated_wait_threshold_seconds": 25,
  "grace_buffer_seconds": 30
}
```
These defaults are shipped in `config/system_constants.json`. Environment variable `ENABLE_SMART_RETRY` (or the constructor flag) can disable the smart retry path for gradual rollouts.

## Rollout
To minimise risk:

1. Deploy with the feature flag disabled to validate infrastructure changes.
2. Enable dry-run logging for a subset of tables to verify queue depth estimates.
3. Gradually enable the smart retry loop (e.g. 10% of tables for 48 hours) while monitoring the new metrics.
4. Scale out to full production once contention failures drop below 5%.
