# Architecture Overview

This document describes how the Telegram bot is wired together at runtime. The
composition root lives inside [`pokerapp/bootstrap.py`](../pokerapp/bootstrap.py)
and builds the long-lived services that the bot reuses for every chat. Those
services are injected into the game model, engine, and viewers so that gameplay
logic never relies on global singletons.

## Composition root

```mermaid
flowchart TD
    subgraph Bootstrap[bootstrap.build_services]
        CFG[Config & secrets]
        LOG[Logging]
        REDIS[(Redis pool)]
        SAFEOPS[TelegramSafeOps factory]
        MSG[MessagingService factory]
        TABLE[TableManager]
        STATS[StatsService]
        ADAPTIVE[AdaptivePlayerReportCache]
        CACHE[PlayerReportCache]
        PRIVATE[PrivateMatchService]
        METRICS[RequestMetrics]
        LOCKS[LockManager]
    end

    CFG --> LOG
    LOG --> REDIS
    REDIS --> TABLE
    REDIS --> PRIVATE
    REDIS --> SAFEOPS
    LOG --> STATS
    STATS --> ADAPTIVE
    ADAPTIVE --> CACHE
    LOG --> METRICS
    METRICS --> MSG
    METRICS --> PRIVATE
    TABLE -->|persists| REDIS
    SAFEOPS -->|wraps| MSG

    subgraph Application[Telegram bot]
        BOT[PokerBot]
        MODEL[PokerBotModel]
        ENGINE[GameEngine]
        VIEW[PokerBotViewer]
        MATCH[MatchmakingService]
    end

    Bootstrap --> BOT
    BOT --> MODEL
    MODEL --> ENGINE
    ENGINE --> MATCH
    ENGINE --> VIEW
```

*Bootstrap* is the only module that touches raw configuration, network clients,
or logging setup. Everything else is passed in as constructor arguments, which
makes the poker logic easy to test and reason about.

## Core services

| Service | Responsibility |
| ------- | -------------- |
| **TableManager** | Persists `Game` snapshots in Redis, rehydrates games after bot restarts, and enforces per-chat storage isolation. |
| **StatsService / StatsReporter** | Streams `hand_started` and `hand_finished` events into the relational database or a no-op backend, depending on configuration. |
| **PlayerReportCache** | Provides cached leaderboard and player statistics for `/stats` requests so users see instant responses. |
| **AdaptivePlayerReportCache** | Learns which players query stats most often and invalidates their cache entries immediately after each hand or stop vote. |
| **PrivateMatchService** | Manages private matches, old-player reminders, and other orchestration that spans multiple hands. |
| **MessagingService** | Encapsulates Telegram throttling, retries, and Markdown formatting. Instances are created through a factory stored in `ApplicationServices`. |
| **TelegramSafeOps** | Wraps `MessagingService` calls with logging metadata, context-aware rate limiting, and exception handling so background tasks remain resilient. |

#### Countdown Error Handling

| Error Type | Detection | Response | Queue Cleanup |
| ---------- | --------- | -------- | ------------- |
| Message deleted | "message to edit not found" in `TelegramBadRequest` | Log warning, remove anchor from queue | Yes (via `_remove_anchor_updates()`) |
| Message uneditable | "message can't be edited" in `TelegramBadRequest` | Log warning, remove anchor from queue | Yes |
| Content unchanged | "message is not modified" in `TelegramBadRequest` | Log debug, update timestamp, continue | No |
| Other BadRequest | Any other `TelegramBadRequest` | Log error, abort countdown | No |
| API error | `TelegramAPIError` | Log exception, abort countdown | No |

The `_remove_anchor_updates()` helper ensures that when an anchor is deleted (by user action or Telegram cleanup), all pending countdown messages for that anchor are removed from the queue, preventing infinite retry loops.

The `cancel_updates_for_anchor()` method is idempotent: it silently succeeds even if the anchor doesn’t exist, making it safe to call during cleanup without checking existence first.

All of these services are created once by the bootstrapper and either stored
inside `ApplicationServices` or exposed via factories for per-chat usage.

## GameEngine dependencies

`GameEngine` receives its collaborators through dependency injection. The main
constructor arguments are:

- `TableManager` — source of truth for persisted `Game` objects.
- `PokerBotViewer` — renders keyboards and status messages to Telegram.
- `MatchmakingService` — drives stage transitions (`start_game`, `progress_stage`,
  and dealing helpers) while holding the `LockManager` stage lock.
- `PlayerManager` — manages seating, ready prompts, and stop votes.
- `StatsReporter` — records `hand_started`/`hand_finished` events and invalidates
  caches for player reports.
- `RequestMetrics` — tracks per-stage timing and request categories for logging.
- `TelegramSafeOps` — ensures Telegram API calls are retried safely with rich
  logging metadata.
- `AdaptivePlayerReportCache` — keeps frequently requested statistics fresh.
- `LockManager` — coordinates stage/table/player locks so concurrent callbacks do
  not corrupt game state.

The combination of a single composition root and constructor injection keeps the
bot modular: new services can be swapped in (for example a different cache or
messaging backend) without editing the poker logic itself.

## Countdown concurrency guarantees

The pre-start countdown subsystem coordinates asynchronous Telegram edits through
a centralized worker queue.  The key invariants are:

- **Single writer per chat:** `start_prestart_countdown` schedules updates through
  `CountdownWorker.schedule_message_update`, which enqueues them in a centralized
  priority queue.  The worker processes updates serially, ensuring only one edit
  per `(chat_id, message_id)` is in flight at a time.  Cancellation via
  `cancel_prestart_countdown` immediately removes pending updates from the queue.
- **Rate-limited updates:** `schedule_message_update` funnels edits through a
  debounced worker (`_execute_debounced_update`) so multiple countdown ticks
  coalesce into a single Telegram edit.  The debounce window enforces Telegram's
  `1 req/sec` guidance without dropping the final state.
- **Monotonic timing:** Countdown math relies on `loop.time()` (monotonic) rather
  than wall clock time, keeping the remaining seconds stable even if the host's
  clock jumps forward or backward.

Together these guarantees eliminate the "time travel" countdown bug, ensure that
rapid start/stop sequences settle cleanly, and shield the bot from 429 rate-limit
errors when high volumes of edits are scheduled simultaneously.

### Lock hierarchy

The countdown subsystem uses minimal locking to coordinate concurrent updates:

```mermaid
graph TD
    A[CountdownWorker._queue_lock] --> B[_worker_task management]
    B --> C[Per-anchor update serialization]
```

`CountdownWorker._queue_lock` protects the internal priority queue during
`schedule_message_update` and `cancel_updates_for_anchor`.  The worker processes
updates serially, so no per-anchor locks are needed.  Cancellation is immediate:
pending updates are removed from the queue without waiting for task acknowledgment.

## Game State Recovery

Recovering from Redis restarts or application crashes relies on a dedicated
validation and cleanup pipeline:

- **Validation** — `GameStateValidator.validate_game` inspects each persisted
  `Game` and emits `ValidationIssue` flags when the snapshot is inconsistent.
  The validator checks for `INVALID_STAGE`, `ORPHANED_PLAYERS`,
  `MISSING_DEALER`, `INCONSISTENT_POT`, `INVALID_DECK`, and `CORRUPTED_JSON`.
- **Recovery** — `GameStateValidator.recover_game` decides whether the game can
  be repaired in place (`reset_to_waiting`) or must be recreated from scratch
  (`delete_and_recreate`).  Recoverable games keep their seated players but reset
  blinds, bets, and community cards.
- **Startup sweep** — `RecoveryService.run_startup_recovery` scans Redis for
  `chat:*:game` keys, loads each snapshot through `TableManager.load_game`, and
  applies the validator.  The service also clears any cached locks and countdown
  tasks to prevent orphaned state from blocking new commands.

```mermaid
sequenceDiagram
    participant Bootstrap
    participant Recovery as RecoveryService
    participant Redis
    participant TableManager
    Bootstrap->>Recovery: run_startup_recovery()
    Recovery->>Redis: SCAN chat:*:game
    loop each key
        Recovery->>TableManager: load_game(chat_id, validate=True)
        TableManager->>TableManager: validate_game()
        alt Recoverable issues
            TableManager->>TableManager: reset_to_waiting()
            TableManager->>Redis: save snapshot
        else Unrecoverable
            TableManager->>Redis: delete key
        end
    end
    Recovery->>LockManager: clear_all_locks()
    Recovery->>CountdownQueue: clear_all()
    Recovery-->>Bootstrap: stats
```

### Error handling

- **Graceful JSON errors:** `TableManager.load_game` catches decoding failures
  (pickle or JSON) and deletes the corrupted key instead of raising.
- **Structured logging:** Every recovery step logs its outcome with structured
  `extra` payloads so dashboards can track `games_deleted`, `games_recovered`,
  `locks_cleared`, and `countdowns_cleared` metrics.
- **Resilient iteration:** `RecoveryService` wraps each Redis key and cleanup
  phase in `try/except`, logging failures while continuing with the remaining
  work to avoid partial startup states.
