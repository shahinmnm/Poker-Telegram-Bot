## [Unreleased]
### Added
- Action-level locking for player actions to prevent duplicate callbacks (Task 6.3.2)

### Fixed
- Countdown timer stability (eliminated time jumps, freezing, resumption)
- Telegram API rate limiting (1 req/sec throttling per message)
- Race conditions in concurrent countdown start/cancel
- Deadlock risk in task cancellation flow
