# Poker Game Engine Lock Pattern & State Mutation Audit

## Summary Statistics
- **Total GameEngine methods analyzed:** 71.
- **Methods acquiring at least one lock:** 21 (~29.6% coverage).
- **Risk distribution:** CRITICAL 3, LOW 1, NONE 67; no HIGH or MEDIUM findings.
- **Common lock contexts:** stage-level orchestration relies on `_trace_lock_guard` (stage lock), while table mutations use `table_write_lock`; player actions batch-acquire locks via `_player_action_locks`.【F:pokerapp/game_engine.py†L2426-L2495】【F:pokerapp/game_engine.py†L427-L581】【F:pokerapp/game_engine.py†L1007-L1035】【F:pokerapp/game_engine.py†L3638-L3684】

## Lock Acquisition Patterns
- **Stage progression locks:** Methods such as `start_game`, `progress_stage`, `finalize_game`, `stop_game`, and related stop-vote flows wrap critical sections in `_trace_lock_guard`, ensuring serialized execution per chat ID via the Redis-backed stage key.【F:pokerapp/game_engine.py†L2426-L2495】【F:pokerapp/game_engine.py†L2904-L2943】【F:pokerapp/game_engine.py†L2945-L3054】【F:pokerapp/game_engine.py†L3638-L3684】
- **Table mutation locks:** Entry points that mutate persisted tables (`join_game`, `leave_game`, `update_player_chips`, `process_action`, `handle_*`, `process_bet`) consistently acquire `table_write_lock` around a retry loop, pairing `load_game_with_version` with `save_game_with_version_check` for optimistic concurrency.【F:pokerapp/game_engine.py†L427-L581】【F:pokerapp/game_engine.py†L583-L681】【F:pokerapp/game_engine.py†L683-L815】【F:pokerapp/game_engine.py†L1293-L1794】
- **Player action batching:** `_player_action_locks` batches stage, wallet, and report locks to protect multi-resource player moves but does not encompass `_execute_player_action`, leaving downstream mutations unguarded.【F:pokerapp/game_engine.py†L1007-L1035】【F:pokerapp/game_engine.py†L1817-L1908】
- **Nested locks:** None detected—each method acquires at most one explicit lock context, reducing deadlock risk but leaving gaps where helper functions run lock-free.

## State Mutation Analysis
- **Unprotected mutations:** `_execute_player_action`, `emergency_reset`, and `_reset_core_game_state` mutate tracked game or player attributes without holding any lock, directly updating `player.state`, `game.pot`, `game.stage`, and `game.state`.【F:pokerapp/game_engine.py†L1824-L1893】【F:pokerapp/game_engine.py†L2564-L2586】【F:pokerapp/game_engine.py†L3470-L3505】
- **Lock-protected mutation:** `process_bet` modifies `current_game.pot` while a `table_write_lock` is held and persists with `save_game_with_version_check`, representing the lone LOW-risk write thanks to versioned persistence.【F:pokerapp/game_engine.py†L1640-L1794】 
- **Persistence calls (`await table_manager.save_game*`):**
  - Plain `save_game`: `_handle_countdown_completion`, `emergency_reset`, `_reset_core_game_state`, `stop_game` (within stage lock) rely on non-versioned saves.【F:pokerapp/game_engine.py†L2773-L2851】【F:pokerapp/game_engine.py†L2564-L2586】【F:pokerapp/game_engine.py†L3470-L3505】【F:pokerapp/game_engine.py†L3638-L3684】
  - Version-checked saves: all table entry points (`join_game`, `leave_game`, `update_player_chips`, `process_action`, `handle_call`, `handle_fold`, `handle_raise`, `process_bet`) already call `save_game_with_version_check` after holding `table_write_lock`.【F:pokerapp/game_engine.py†L427-L1794】

## TOCTTOU Risk Assessment
- **CRITICAL (writes without locks):** `_execute_player_action`, `emergency_reset`, `_reset_core_game_state`.
- **LOW (protected writes):** `process_bet` due to table lock + version check.
- **NONE:** Remaining methods are read-only, view/utility helpers, or lock-acquiring orchestration without tracked writes.
- **HIGH / MEDIUM:** None observed; no read-then-write sequences with intervening awaits lacking version checks were found beyond the critical unprotected mutations.

## Critical Findings: Unprotected Writes
| Method | State Writes | Details |
| --- | --- | --- |
| `_execute_player_action` | `player.state`, `game.pot` | Mutates player elimination state and accumulates pot while awaiting wallet authorization and without any stage/table lock, allowing concurrent callers to interleave pot updates and player state changes.【F:pokerapp/game_engine.py†L1824-L1893】 |
| `emergency_reset` | `game.stage`, `game.pot` | Performs crash recovery writes without `stage_lock`, resetting stage and pot before persisting to storage; concurrent handlers could observe partially reset state.【F:pokerapp/game_engine.py†L2564-L2586】 |
| `_reset_core_game_state` | `game.pot`, `game.state` | Finalizes a hand by zeroing pot and marking the game finished without staging lock protection, despite asynchronous wallet reads and persistence that could race with active gameplay.【F:pokerapp/game_engine.py†L3470-L3505】 |

## High Risk: TOCTTOU Patterns
- **None detected.** All detected writes either lacked locks entirely (classified as CRITICAL) or were guarded by table locks with version checks.

## Lock Coverage Matrix
| Method | Locks Acquired | State Writes | Risk Level |
| --- | --- | --- | --- |
| _execute_player_action | — | game.pot@L1858, game.pot@L1893, player.state@L1826 | CRITICAL |
| _reset_core_game_state | — | game.pot@L3478, game.state@L3479 | CRITICAL |
| emergency_reset | — | game.stage@L2582, game.pot@L2584 | CRITICAL |
| process_bet | table_write_lock | current_game.pot@L1735 | LOW |
| __init__ | — | — | NONE |
| _announce_new_hand_ready | — | — | NONE |
| _build_lock_context | — | — | NONE |
| _build_stop_cancellation_message | — | — | NONE |
| _build_telegram_log_extra | — | — | NONE |
| _cancel_all_timers_internal | — | — | NONE |
| _check_if_stop_passes | — | — | NONE |
| _create_joining_player | — | — | NONE |
| _delete_chat_messages | — | — | NONE |
| _determine_pot_winners | — | — | NONE |
| _determine_winners | — | — | NONE |
| _distribute_payouts | — | — | NONE |
| _evaluate_contender_hands | — | — | NONE |
| _execute_payouts | — | — | NONE |
| _finalize_stop_request | stage_lock | — | NONE |
| _find_player_by_user_id | — | — | NONE |
| _force_release_locks | — | — | NONE |
| _get_available_seats | — | — | NONE |
| _handle_countdown_completion | stage_lock | — | NONE |
| _initialize_stop_translations | — | — | NONE |
| _invalidate_adaptive_report_cache | — | — | NONE |
| _log_engine_event_lock_failure | — | — | NONE |
| _log_extra | — | — | NONE |
| _log_lock_snapshot | — | — | NONE |
| _log_long_hold_snapshot | — | — | NONE |
| _loop_time | — | — | NONE |
| _notify_results | — | — | NONE |
| _player_action_locks | stage_lock | — | NONE |
| _prepare_hand_statistics | — | — | NONE |
| _process_fold_win | — | — | NONE |
| _process_showdown_results | — | — | NONE |
| _record_hand_results | — | — | NONE |
| _refund_players | — | — | NONE |
| _reset_game_state | — | — | NONE |
| _reset_game_state_after_round | stage_lock | — | NONE |
| _reset_game_state_after_stop | stage_lock | — | NONE |
| _stage_lock_key | — | — | NONE |
| _start_prestart_countdown | — | — | NONE |
| _trace_lock_guard | — | — | NONE |
| _update_votes_and_message | — | — | NONE |
| _validate_stop_request | — | — | NONE |
| _validate_stop_voter | — | — | NONE |
| add_cards_to_table | stage_lock | — | NONE |
| build_stop_request_markup | — | — | NONE |
| cancel_hand | stage_lock | — | NONE |
| cancel_prestart_countdown | — | — | NONE |
| compute_turn_deadline | — | — | NONE |
| confirm_stop_vote | stage_lock | — | NONE |
| finalize_game | stage_lock | — | NONE |
| hand_type_to_label | — | — | NONE |
| handle_call | table_write_lock | — | NONE |
| handle_fold | table_write_lock | — | NONE |
| handle_raise | table_write_lock | — | NONE |
| join_game | table_write_lock | — | NONE |
| leave_game | table_write_lock | — | NONE |
| process_action | table_write_lock | — | NONE |
| progress_stage | stage_lock | — | NONE |
| refresh_turn_deadline | — | — | NONE |
| render_stop_request_message | — | — | NONE |
| request_stop | — | — | NONE |
| resume_stop_vote | stage_lock | — | NONE |
| shutdown | — | — | NONE |
| start | — | — | NONE |
| start_game | stage_lock | — | NONE |
| state_token | — | — | NONE |
| stop_game | stage_lock | — | NONE |
| update_player_chips | table_write_lock | — | NONE |

## Recommended Actions
1. **Priority 1 – Wrap critical helpers in `stage_lock`:** apply `_trace_lock_guard` (or `stage_lock`) around `_execute_player_action`, `emergency_reset`, and `_reset_core_game_state` before any state mutation to serialize concurrent flows.

   ```python
   async def _execute_player_action(self, game: Game, player: Player, action: str, amount: int) -> bool:
       lock_key = self._stage_lock_key(game.chat_id)
       async with self._trace_lock_guard(
           lock_key=lock_key,
           chat_id=game.chat_id,
           game=game,
           stage_label="stage_lock:execute_player_action",
           event_stage_label="execute_player_action",
           timeout=self._stage_lock_timeout,
       ):
           # existing mutation logic (pot updates, player.state changes)
           ...
   ```

2. **Priority 2 – Use optimistic versioning for direct saves:** when a critical helper must persist without the table entrypoints, load the game with a version token and write back with `save_game_with_version_check` to prevent TOCTTOU overwrites.

   ```python
   game, version = await self._table_manager.load_game_with_version(chat_id)
   # mutate tracked fields under the stage lock
   game.state = GameState.FINISHED
   game.pot = 0
   await self._table_manager.save_game_with_version_check(chat_id, game, version)
   ```

3. **Priority 3 – Refactor potential medium-risk reads:** when future helpers need to read multiple attributes before awaiting external I/O, snapshot the state and validate it post-await before writing.

   ```python
   original_stage = game.stage
   await some_async_call()
   if game.stage != original_stage:
       return  # abort because state changed during await
   game.stage = GameState.ROUND_FLOP
   ```

These changes will close the lock gaps, introduce consistent version checks, and provide a template for avoiding future TOCTTOU issues.
