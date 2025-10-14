# Phase 2: Materialized Statistics Deployment Guide

## Overview

Phase 2 introduces a materialized statistics layer that pre-computes player metrics
for instant leaderboard generation and stats queries. This eliminates expensive
JOIN operations and aggregations on every request.

## Architecture Changes

### Before Phase 2 (Slow Path)
User requests leaderboard  
  → JOIN hands_players + hands + users  
  → GROUP BY user_id  
  → SUM(amount_won), COUNT(*)  
  → ORDER BY total_winnings DESC  
  → Return top 10 (500-800ms query time)

### After Phase 2 (Fast Path)
User requests leaderboard  
  → SELECT * FROM player_stats  
  → ORDER BY total_winnings DESC (uses index)  
  → LIMIT 10  
  → Return (5-15ms query time)

## Deployment Checklist

1. **Back up the database** (SQLite: copy `/app/data/poker.db`).
2. **Upgrade the bot** to the Phase 2 application build.
3. **Run the migration runner** (`StatsService.ensure_ready()` or `make migrate-stats`).
   - Migration 002 is skipped automatically on SQLite.
   - Migration 003 creates the materialized `player_stats` table and triggers.
4. **Verify migration results**:
   - Confirm rows in `player_stats` match expected active players.
   - Inspect `stats_schema_migrations` for entries `001` and `003`.
   - Check Grafana dashboards (Smart Retry & Fine-Grained Locks) render titles.
5. **Warm caches** by running the leaderboard command in a staging chat.
6. **Monitor logs** for `stats_migration_applied` and `player_stats_*` query timings.

## Rollback Plan

1. Restore the pre-upgrade database backup.
2. Downgrade the bot application to the previous release.
3. Clear cached statistics in Redis/memory to avoid stale data.

## Operational Notes

- The `PlayerStatsQuery` class is optimized for long-lived SQLite connections.
- Trigger-based updates ensure that inserts/updates to `hands` and `hands_players`
  immediately refresh materialized statistics.
- To force a manual rebuild, truncate `player_stats` and rerun migration 003.
- Grafana dashboards now ship with explicit titles to satisfy provisioning checks.

## Verification Queries

```sql
SELECT * FROM stats_schema_migrations ORDER BY applied_at DESC;
SELECT user_id, total_hands, total_winnings FROM player_stats ORDER BY total_winnings DESC LIMIT 5;
```

## Support

If deployment issues arise, capture the migration logs (look for
`Phase 2 Migration Complete`) and reach out to the infrastructure team with the
log excerpt plus the output of `PRAGMA table_info(player_stats);`.
