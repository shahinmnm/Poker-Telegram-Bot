-- File: migrations/002_add_performance_indexes.sql
-- Purpose: Add compound and partial indexes for high-traffic read paths
-- Notes:
--   * Uses CONCURRENTLY to avoid locking writes on large tables.
--   * Safe to re-run thanks to IF NOT EXISTS guards.
--   * Focuses on wallet, streak, and history lookups powering the bot dashboard.

-- Compound index for player stats scoped to chat and ordered by recency.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_game_history_user_chat_time
    ON game_history(user_id, chat_id, timestamp DESC);

-- Partial index targeting active wallets for quick balance reads.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_wallets_user_active
    ON wallets(user_id, is_active) WHERE is_active = true;

-- Recent history queries filter by timestamp—optimize the 7 day hot path.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_game_history_recent
    ON game_history(timestamp DESC) WHERE timestamp > NOW() - INTERVAL '7 days';

-- Active streak leaderboard lookups.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_streaks_active
    ON player_streaks(user_id, streak_type) WHERE is_active = true;
