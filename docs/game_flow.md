# PokerBot Game Flow

This document expands on the concise state machine reference at the top of
[`pokerapp/game_engine.py`](../pokerapp/game_engine.py). It links the public
coroutines that drive the table lifecycle with the collaborating services that
persist chat state, talk to Telegram, and record statistics.

## State progression at a glance

The poker bot advances through well-defined `GameState` values. Hands start in
`WAITING` (players join) and finish when `finalize_game` runs. The diagram below
shows the happy-path transition sequence alongside the coroutines that move the
state machine forward. Early exits (for example when everyone folds) are also
handled by `finalize_game`.

```mermaid
sequenceDiagram
    autonumber
    participant TG as Telegram
    participant PM as PlayerManager
    participant GE as GameEngine
    participant MS as MatchmakingService
    participant TM as TableManager
    participant SS as StatsService/Reporter

    Note over TG,GE: WAITING — lobby is open
    TG->>PM: /start & Join button callbacks
    PM->>TM: save_game(chat_id, game)
    PM->>GE: request start when seats ready

    Note over GE,MS: ROUND_PRE_FLOP setup
    GE->>MS: start_game(context, game, chat)
    MS->>TM: persist dealer/blind rotations
    MS->>SS: hand_started(...)
    MS->>TG: deal hole cards via PokerBotViewer

    loop Betting cycle per stage
        Note over GE,MS: progress_stage() called
        GE->>MS: progress_stage(context, game, chat)
        MS->>MS: collect bets & reset has_acted
        alt ROUND_PRE_FLOP → ROUND_FLOP
            MS->>GE: add_cards_to_table(3)
            GE->>TG: broadcast flop snapshot
        else ROUND_FLOP → ROUND_TURN
            MS->>GE: add_cards_to_table(1)
            GE->>TG: broadcast turn snapshot
        else ROUND_TURN → ROUND_RIVER
            MS->>GE: add_cards_to_table(1)
            GE->>TG: broadcast river snapshot
        else ROUND_RIVER → FINISHED
            MS->>GE: finalize_game(...)
        end
        GE->>TM: save_game(chat_id, game)
    end

    Note over GE,SS: finalize_game()
    GE->>GE: finalize_game(context, game, chat)
    GE->>TG: send_showdown_results()
    GE->>SS: hand_finished(payouts, hand_labels)
    GE->>TM: reset+save_game(chat_id, game)
    GE->>PM: send_join_prompt() → back to WAITING
```

### Stage specific responsibilities

| Stage | Trigger | Key responsibilities |
| ----- | ------- | ------------------- |
| `WAITING` | Player reactions (`/start`, inline joins) | `PlayerManager.send_join_prompt` displays the CTA. `TableManager` keeps the pending game persisted so reconnections resume correctly. |
| `ROUND_PRE_FLOP` | `MatchmakingService.start_game` | Dealer button rotation, blind posting, hole-card dealing, statistics start hooks, `RequestMetrics.start_cycle`. |
| `ROUND_FLOP` | `GameEngine.progress_stage` → `MatchmakingService.progress_stage` | Burn + deal three community cards, reset `has_acted` flags, notify viewers, persist table snapshot. |
| `ROUND_TURN` | Subsequent `progress_stage` call | Deal the fourth card, refresh betting order, persist state. |
| `ROUND_RIVER` | Subsequent `progress_stage` call | Deal the final card, determine if betting continues or hand can be settled immediately. |
| `FINISHED` | `GameEngine.finalize_game` | Evaluate hands, distribute pot, emit statistics, clear anchors, prompt new hand. |

`MatchmakingService.progress_stage` enforces stage order while holding the
`LockManager` stage lock so that Telegram callbacks, background jobs, and rate
limited retries cannot interleave inconsistent mutations.

## Swimlane — collaborating components

The following swimlane diagram captures how the main services collaborate during
one full hand. Each lane highlights the responsibilities that the class owns or
delegates.

```plantuml
@startuml GameFlowSwimlane
|Telegram|
start
:Players tap /start and join buttons;
|PlayerManager|
:Record join intent;
:Persist ready prompt via TableManager;
|GameEngine|
:detect required players;
:acquire stage lock;
|MatchmakingService|
:start_game();
:assign dealer & blinds;
:deal_hole_cards();
|PokerBotViewer|
:send private hole cards;
|GameEngine|
:progress_stage();
|MatchmakingService|
:collect_bets_for_pot();
:transition ROUND_PRE_FLOP→ROUND_FLOP;
|PokerBotViewer|
:announce flop;
|GameEngine|
:progress_stage();
|MatchmakingService|
:transition to turn & river;
|PokerBotViewer|
:update community cards;
|GameEngine|
:finalize_game();
|StatsService|
:hand_finished(payouts);
|TableManager|
:reset & save game;
|PlayerManager|
:send_join_prompt();
|Telegram|
:Players ready for next hand;
stop
@enduml
```

## Supporting services referenced

- **TableManager** keeps one `Game` object per chat persisted to Redis so that
  reconnections and bot restarts recover ongoing hands.
- **PlayerManager** renders the join prompt, manages seat assignments, and keeps
  localized role labels (dealer, small blind, big blind) up to date.
- **PokerBotViewer** is the façade around the messaging layer. It batches edits
  and renders translated templates for table messages and anchors.
- **StatsService** (via `StatsReporter`) records per-hand statistics and drives
  bonus eligibility caches using `AdaptivePlayerReportCache`.
- **RequestMetrics** records timing and outcome metadata for each major player
  interaction so incidents can be diagnosed.

Together these components allow the state machine to remain small and
cohesively focused on poker rules while infra concerns (persistence, retries,
metrics, localization) remain testable in isolation.
