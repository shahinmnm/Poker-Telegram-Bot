# Lock Contention Analysis

## Summary of Guarded Locks

| Lock key prefix | Category | Primary guard(s) | Protected operations |
|-----------------|----------|------------------|----------------------|
| `stage:<chat>`  | `engine_stage` | `GameEngine.progress_stage`, `GameEngine.finalize_game`, `MatchmakingService.add_cards_to_table` | Advancing hand state, resolving winners, resetting game state |
| `chat:<chat>`   | `chat` | `PokerBotModel._chat_guard`, `_clear_game_messages` | Telegram message lifecycle, player prompt updates |
| `engine_stage:<chat>` | `engine_stage` | internal helpers for stop/cancel flows | Stop-hand workflow and post-stop cleanup |

The most time-consuming operations inside these guards were:

* Telegram API calls when deleting board/turn messages and announcing winners.
* Redis-bound wallet updates and post-hand stat reporting.
* Scheduling of “new hand ready” prompts and join invitations.

Because all of these were awaited while the `stage:<chat>` lock was held, parallel requests (for example multiple `/start` invocations) quickly exhausted the 10-second timeout and triggered repeated retries.

## Refactoring Highlights

* The stage lock timeout now honours the configuration’s `engine_stage` value (default 25 seconds) across the engine and matchmaking service, while the chat guard uses the new `chat` category timeout (default 15 seconds).  Both categories are now declared in `LockManager` so the timeouts apply automatically.
* `GameEngine.finalize_game` now limits its critical section to winner calculation, payout mutation and state reset.  Telegram deletions, notifications and stat reporting are executed after releasing the stage lock using a deferred plan collected while the lock is held.
* `_reset_game_state` accepts `defer_notifications=True` so finalisation can emit the “new hand ready” message and join prompt after the lock is released, avoiding double work and contention.
* `PokerBotModel._clear_game_messages` supports a `collect_only` mode that returns the pending message IDs while clearing in-memory state; the new `GameEngine._delete_chat_messages` helper performs the actual deletions once the stage lock is free.
* Startup and start-game paths now call `LockManager.detect_deadlock()` and log a JSON snapshot to simplify diagnostics of held or waiting locks.
* Lock debugging can be enabled incrementally through the `lock_manager` options in `config/system_constants.json`, covering duplicate detection, hierarchy enforcement, fine-grained lock rollout, and optional stack trace logging for long-lived acquisitions.

## Unprotected Mutations

The audit identified critical windows where concurrent operations could corrupt game state:

1. **`_execute_player_action`**: Awaited wallet authorisations allowed interleaved updates of `game.pot` and betting metadata.
2. **`emergency_reset`**: Multiple admin reset commands could execute concurrently, leading to partial cleanup or inconsistent state.

The Phase 2A-4 fixes serialize both operations within the `stage:<chat>` guard so state mutations remain atomic during async I/O.

## Reset Path Races

Before the remediation, `_reset_core_game_state` reloaded and persisted hand state outside of the stage lock.  Parallel `/reset` or `/start` invocations could therefore overwrite the persisted version without detecting conflicts.  The Phase 2A-5 change binds the reset flow to the stage lock, captures the optimistic lock version prior to mutation, and retries once on conflict to preserve the settlement ordering guarantees.

## Follow-up Observability Ideas

* Track per-lock holding durations (e.g. via an async context timer) and feed them into the existing request metrics to spot hot spots early.
* Consider emitting a warning when `detect_deadlock()` reports waiting tasks to aid production debugging.
