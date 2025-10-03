## [Unreleased]
### Added
- Action-level locking for player actions to prevent duplicate callbacks (Task 6.3.2)
- CLI flag `--skip-stats-buffer` to disable deferred statistics persistence during debugging sessions
- Stats batch buffer metric for monitoring the average flush batch size

### Known Issues
- SQLite deployments only run the bootstrap migration (`001_create_statistics_tables.sql`).
  Additional SQL migration files are skipped to avoid unsupported statements, so
  use PostgreSQL or MySQL for the materialised statistics tables.

### Fixed
- Countdown timer stability (eliminated time jumps, freezing, resumption)
- Telegram API rate limiting (1 req/sec throttling per message)
- Race conditions in concurrent countdown start/cancel
- Deadlock risk in task cancellation flow
