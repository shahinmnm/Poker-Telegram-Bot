# Two-Phase Commit Betting Architecture

## System Overview

```
┌───────────────────────────────┐        ┌──────────────────────┐
│ Telegram Client               │        │ Wallet Repository    │
│  (Player action)             │        │  (PostgreSQL/Redis)  │
└──────────────┬────────────────┘        └──────────┬───────────┘
               │                                    │
               │ 1️⃣ Action request                 │
               ▼                                    │
┌───────────────────────────────┐        ┌──────────▼───────────┐
│ BettingHandler                │        │ WalletService        │
│  - Validate action           │        │  (Phase 1 & Phase 2) │
│  - Coordinate locks          │        └──────────┬───────────┘
└──────────────┬────────────────┘                   │
               │ 2️⃣ Reserve (Phase 1)              │ 3️⃣ Debit wallet
               ▼                                    │  Store reservation
┌───────────────────────────────┐                   │
│ LockManager                   │                   │
│  - Acquire table write lock   │                   │
└──────────────┬────────────────┘                   │
               │ 4️⃣ Load/Validate state            │
               ▼                                    │
┌───────────────────────────────┐                   │
│ GameEngine + Redis            │                   │
│  - Load state + version       │                   │
│  - Apply action               │◄──────────────────┘
│  - Save with Lua CAS          │
└──────────────┬────────────────┘
               │ 5️⃣ Commit (Phase 2)
               ▼
┌───────────────────────────────┐
│ WalletService                 │
│  - Commit / rollback          │
│  - DLQ on refund failure      │
└───────────────────────────────┘
```

## Phase Responsibilities

### Phase 1 – Reservation (outside lock)
1. Validate intent using the latest snapshot.
2. Atomically debit the wallet via the repository.
3. Persist reservation hash in Redis with a 5-minute TTL and 30-second grace.
4. Schedule watchdog to auto-rollback on timeout.

### Phase 2 – Commit (inside lock)
1. Acquire table write lock (30s timeout).
2. Reload game state with optimistic locking version.
3. Confirm player turn and action legality.
4. Commit the reservation (idempotent status flip).
5. Apply action to state and persist via Lua compare-and-set.

### Abort & Compensation
- **Rollback pending reservation** – return chips and mark as `rolled_back`.
- **Compensate committed reservation** – credit funds and mark as `rolled_back` when state persistence fails.
- **Refund failure** – push to `wallet:dlq` with full context for manual remediation.

## Error Decision Tree

```
Start → Reserve chips?
  ├─ No (insufficient funds) → Fail fast
  └─ Yes → Acquire table lock → Load state?
       ├─ No → Rollback (game_not_found)
       └─ Yes → Player turn?
            ├─ No → Rollback (not_players_turn)
            └─ Yes → Commit reservation
                 → Apply + Save state (CAS)
                      ├─ Success → ACK
                      └─ Conflict/Failure?
                           ├─ Optimistic conflict → Compensating refund
                           └─ Wallet refund failure → DLQ + alert
```

## Performance Characteristics

| Operation              | Target P95 | Notes                                      |
|-----------------------|------------|---------------------------------------------|
| Phase 1 Reservation   | 50 ms      | DB debit + Redis write                      |
| Lock Hold (Phase 2)   | <200 ms    | Includes state load, validation, and save   |
| Compensation Workflow | <300 ms    | Debit already completed, credit on failure  |

Lock duration is dominated by the game state update; wallet mutation is executed outside the critical section.

## Monitoring & Observability

Prometheus metrics exposed via `pokerapp.metrics`:

- `poker_wallet_reserve_total{status="success|insufficient_funds|error"}`
- `poker_wallet_commit_total{status="success|not_found|error"}`
- `poker_wallet_rollback_total{status="success|not_found|error"}`
- `poker_wallet_dlq_total`
- `poker_wallet_operation_duration_seconds{operation="reserve|commit|rollback"}`
- `poker_action_duration_seconds{action="fold|check|call|raise|all_in"}`

### Sample Grafana Queries

- **Reservation Failure Rate**
  ```promql
  sum(rate(poker_wallet_reserve_total{status!="success"}[5m]))
    / sum(rate(poker_wallet_reserve_total[5m]))
  ```
- **Average Lock Hold Time**
  ```promql
  histogram_quantile(
    0.95,
    sum(rate(poker_wallet_operation_duration_seconds_bucket{operation="commit"}[5m]))
      by (le)
  )
  ```
- **DLQ Alert**
  ```promql
  increase(poker_wallet_dlq_total[1h]) > 0
  ```

## Migration Plan

1. **Deploy Two-Phase components** – roll out `WalletService`, `BettingHandler`, and `GameEngine` enhancements behind a feature flag.
2. **Shadow traffic** – mirror betting requests to the new handler while continuing to use legacy flow.
3. **Cutover** – enable feature flag per table once metrics remain within SLOs for 24 hours.
4. **Monitor** – ensure DLQ remains empty and wallet balances reconcile with historical baselines.

## Rollback Strategy

- Disable feature flag to revert to legacy single-phase handler.
- Flush reservation keys via `wallet:reservation:*` to prevent orphaned records.
- Drain DLQ entries – each record includes `reservation_id`, `user_id`, `amount`, `reason`, and timestamp to support manual reimbursement.

## Operational Notes

- All wallet and Redis interactions honour a 5-second timeout to prevent cascading stalls.
- Reservation watchdog tasks self-cancel once committed or rolled back.
- DLQ pushes log at `ERROR` level and increment `poker_wallet_dlq_total` for on-call visibility.
- Optimistic locking ensures no lost updates even under concurrent raises or all-in actions.

