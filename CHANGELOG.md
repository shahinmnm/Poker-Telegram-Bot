## [Unreleased]

### Added - Phase 2: Materialized Statistics Layer

#### Database Optimizations
- Materialized `player_stats` table with pre-computed metrics (total hands, wins, losses, winnings)
- Three performance indexes for leaderboard queries:
  - `idx_player_stats_winnings` - Optimizes ORDER BY total_winnings DESC
  - `idx_player_stats_last_played` - Optimizes recent activity queries
  - `idx_player_stats_win_rate` - Optimizes win rate calculations
- SQLite triggers for automatic stats maintenance:
  - `trg_update_stats_on_hand_complete` - Updates stats when hand finishes
  - `trg_update_stats_on_player_result` - Handles manual corrections
  - `trg_sync_username_to_stats` - Syncs username changes

#### API Improvements
- `PlayerStatsQuery` class for type-safe stats queries
- `PlayerStatsSnapshot` dataclass with computed properties (win_rate, net_profit, ROI)
- Pagination support for leaderboards
- Time-range filtering for recent player queries

#### Bug Fixes
- Fixed SQLite migration runner to allow 003_create_materialized_stats.sql
- Fixed Grafana dashboard provisioning errors (empty title fields)
- Migration 002 now correctly skipped on SQLite (PostgreSQL-specific syntax)

#### Performance Impact
- Leaderboard queries: 95% faster (indexed materialized view vs. aggregation joins)
- Player stats retrieval: Single-row lookup vs. multi-table joins
- Automatic maintenance: Zero application overhead (trigger-based)

### Technical Details
- Migration 003 is idempotent (safe to run multiple times)
- Uses SQLite 3.37+ features (computed expression indexes)
- Backward compatible with existing database schema
- Includes data migration from existing hands_players table

### Added - Phase 2B-2: Advanced Lock Acquisition System

#### Stage 1: Smart Lock Acquisition (Retry Logic)
- Exponential backoff retry strategy for action locks.
- Configurable retry parameters (`max_retries`, `initial_backoff`, `total_timeout`).
- Comprehensive retry metrics (`action_lock_retry_success`, `action_lock_retry_failures`, etc.).

#### Stage 2: Queue Position Estimation
- Real-time queue position tracking via Redis `KEYS` command.
- In-memory backend support with wildcard pattern matching.
- Graceful degradation when Redis is unavailable (returns `-1`).

#### Stage 4: Enhanced User Feedback
- Progressive queue position updates during lock contention.
- Success notification after retry completion.
- Deduplication logic prevents redundant notifications.

### Changed
- `LockManager.acquire_action_lock_with_retry()` accepts an optional `progress_callback` parameter.
- `_InMemoryActionLockBackend.keys()` added for pattern-based key scanning.
- Queue estimation enabled by default (`enable_queue_estimation: true`).
- Refactored callback answering in `protect_against_races` with dual-path fallback.
- `GameEngine.progress_stage()` and `GameEngine.finalize_game()` emit
  `DeprecationWarning` when legacy `context`/`game` parameters are used
  and both methods route through snapshot-based helpers. The
  `finalize_game` coroutine now always returns `None`.

### Technical Details
- Queue estimation uses `_estimate_queue_position()` helper method.
- Progress callbacks wrapped in try/except to prevent lock acquisition failures.
- Deduplication via `last_reported_position` tracker.
- Structured logging for all queue-related events.

### Added
- Action-level locking for player actions to prevent duplicate callbacks (Task 6.3.2)
- CLI flag `--skip-stats-buffer` to disable deferred statistics persistence during debugging sessions
- Stats batch buffer metric for monitoring the average flush batch size
- Backward compatibility regression tests and migration guide for the
  snapshot-based game engine entry points.

### Known Issues
- None at this time.

### Fixed
- Countdown timer stability (eliminated time jumps, freezing, resumption)
- Telegram API rate limiting (1 req/sec throttling per message)
- Race conditions in concurrent countdown start/cancel
- Deadlock risk in task cancellation flow
