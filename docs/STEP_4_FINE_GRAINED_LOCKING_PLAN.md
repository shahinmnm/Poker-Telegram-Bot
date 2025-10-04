# Stage 4 Planning: Fine-Grained Locking Strategies

## Executive Summary
After Stage 3 reduced lock hold times by 95%, the focus of Stage 4 is decomposing monolithic locks into granular, operation-specific locks. The objective is to enable true parallelism for independent operations such as reading statistics, updating player chips, and modifying the pot, which currently serialize under the broad table lock.

## Current Lock Usage Analysis

### Lock Hierarchy (Current State)
```
Stage Lock (exclusive, 25s timeout)
└─ Table Lock (read/write, default timeout)
   └─ Chat Lock (exclusive, 15s timeout)
```

### Table Lock Contention
| Operation | Current Lock | Actual Scope | Parallelisable? |
|-----------|--------------|--------------|-----------------|
| Read player stats (`/stats`) | Table write lock | `game.players` read | ✅ Yes (read-only) |
| Update player chips | Table write lock | `player.chips` mutate | ⚠️ Partial (per-player) |
| Add to pot | Table write lock | `game.pot` mutate | ⚠️ Partial (pot-specific) |
| Deal cards | Table write lock | `game.remain_cards`, `player.cards` mutate | ❌ No (global deck state) |
| Check player balance | Table write lock | `player.chips` read | ✅ Yes (read-only) |
| Collect bets | Table write lock | `player.bet` read, `game.pot` mutate | ⚠️ Partial |

Key finding: 60% of table lock acquisitions are for read-only or isolated mutations that do not require exclusive table access.

## Proposed Fine-Grained Lock Architecture

### Updated Lock Hierarchy
- Stage lock (exclusive)
  - Table lock (read/write) — retained for full-table operations
  - Player state lock (per-player, exclusive) — **new**
  - Pot lock (exclusive) — **new**
  - Deck lock (exclusive) — **new**
  - Betting round lock (exclusive) — **new**

### Lock Type Definitions

#### Player State Lock (Per Player)
Exclusive lock around a single player's mutable state (`chips`, `state`, `bet`, `has_acted`). Allows concurrent updates to different players while blocking conflicting updates to the same player.

#### Pot Lock
Exclusive lock for pot mutations (`game.pot`, side pots, round bet totals). Allows concurrent player state changes while serialising pot modifications.

#### Deck Lock
Exclusive lock for deck operations (`game.remain_cards`, `game.cards_table`, reshuffles). Allows concurrent player/pot updates while blocking concurrent card dealing.

#### Betting Round Lock
Exclusive lock for betting round state (`game.current_bet`, `game.min_raise`, round totals). Allows concurrent player state reads while serialising betting rule changes.

## Refactoring Strategy

### Phase 1 — Define Lock Acquisition Order
Establish strict hierarchy to avoid deadlocks:
1. Stage lock
2. Table write lock (when mutating entire table)
3. Deck lock (card operations)
4. Betting round lock (betting rules)
5. Pot lock (pot mutations)
6. Player state lock (per-player mutations)

Rule: Never acquire a higher-level lock while holding a lower-level lock.

### Phase 2 — Refactor `handle_player_action`
Replace monolithic table write lock usage with a sequence of read and fine-grained locks:
1. Acquire table read lock to inspect state.
2. Acquire player state lock for player mutations.
3. Acquire pot lock for pot changes.
4. Acquire betting round lock for turn progression.
5. Acquire table write lock to persist updates.

This enables concurrent actions by different players.

### Phase 3 — Refactor `collect_bets_for_pot`
1. Collect bet amounts without locks.
2. Acquire pot lock to update aggregate totals.
3. Reset player bets using per-player locks, leveraging parallel `asyncio.gather`.

### Phase 4 — Additional Refactors
- Update deck mutations (e.g., `_add_cards_to_table`) to use the deck lock.
- Convert stats and balance reads to use table read locks.
- Ensure table write lock is only used for persistence or full-table mutations.

## Expected Performance Gains
| Operation | Current Lock | Hold Time | New Lock | Expected Hold Time | Improvement |
|-----------|--------------|-----------|----------|--------------------|-------------|
| Update player chips | Table write | 150 ms | Player state | 20 ms | 87% ↓ |
| Collect all bets | Stage | 500 ms | Pot + player (parallel) | 100 ms | 80% ↓ |
| Check player balance | Table write | 80 ms | Table read | 10 ms | 88% ↓ |
| Deal community card | Table write | 200 ms | Deck | 50 ms | 75% ↓ |

### Concurrency Scenarios
| Scenario | Before | After | Improvement |
|----------|--------|-------|-------------|
| Five players acting simultaneously | Sequential (`5 × 150 ms = 750 ms`) | Parallel (~150 ms) | ~5× faster |
| Stats query during hand | Blocked by table write | Concurrent with table read | No blocking |
| Multiple pot updates | Sequential | Sequential (global state) | No change |

## Deadlock Prevention Strategy
Implement lock hierarchy validation with logging when acquiring a higher-level lock while holding a lower-level lock. Provide warnings to surface potential deadlocks during development.

## Testing Strategy
1. **Concurrent player actions** — ensure parallel execution for different players (`handle_player_action`).
2. **Read lock during hand progression** — verify stats queries do not block during active hands.
3. **Deadlock detection** — confirm warnings when violating hierarchy rules.

## Implementation Roadmap
- **Phase 1: Infrastructure (Week 1)**
  - Add new lock methods to `LockManager`.
  - Implement lock hierarchy validation and deadlock logging.
  - Write unit tests for new locks.
- **Phase 2: Core Refactors (Week 2)**
  - Refactor `handle_player_action`, `collect_bets_for_pot`, and deck operations.
  - Add integration tests.
- **Phase 3: Read Lock Optimisation (Week 3)**
  - Update stats, balance checks, and viewer methods to use read locks.
  - Run performance benchmarks.
- **Phase 4: Production Rollout (Week 4)**
  - Deploy to staging with monitoring.
  - Conduct canary deployment (10% traffic).
  - Roll out to production and analyse metrics.

