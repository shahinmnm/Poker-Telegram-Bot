-- Migration: Add performance indexes for statistics workloads
-- Generated on: 2025-10-03

BEGIN;

-- Improve retrieval of recent hands per chat
CREATE INDEX IF NOT EXISTS idx_player_hand_history_chat_finished
    ON player_hand_history (chat_id, finished_at DESC);

-- Accelerate player-specific history lookups
CREATE INDEX IF NOT EXISTS idx_player_hand_history_user_finished
    ON player_hand_history (user_id, finished_at DESC);

-- Optimise leaderboard queries based on games played
CREATE INDEX IF NOT EXISTS idx_player_stats_total_games
    ON player_stats (total_games DESC);

-- Optimise leaderboard queries for winners
CREATE INDEX IF NOT EXISTS idx_player_stats_total_wins
    ON player_stats (total_wins DESC);

-- Support filtering by chat and player when analysing hands
CREATE INDEX IF NOT EXISTS idx_player_hand_history_chat_user
    ON player_hand_history (chat_id, user_id);

COMMIT;
