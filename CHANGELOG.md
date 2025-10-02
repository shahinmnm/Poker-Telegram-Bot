## [Unreleased]
### Fixed
- Countdown timer stability (eliminated time jumps, freezing, resumption)
- Telegram API rate limiting (1 req/sec throttling per message)
- Race conditions in concurrent countdown start/cancel
- Deadlock risk in task cancellation flow
