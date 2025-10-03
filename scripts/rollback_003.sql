-- ============================================================================
-- Rollback Script for Migration 003: Materialized Stats Table
-- ============================================================================
-- Purpose: Remove player_stats table, indexes, and triggers
-- Usage: sqlite3 data/poker.db < scripts/rollback_003.sql
-- ============================================================================

BEGIN TRANSACTION;

-- Drop triggers first (dependencies)
DROP TRIGGER IF EXISTS trg_update_stats_on_hand_complete;
DROP TRIGGER IF EXISTS trg_update_stats_on_player_result;

-- Drop indexes
DROP INDEX IF EXISTS idx_player_stats_winnings;
DROP INDEX IF EXISTS idx_player_stats_last_played;
DROP INDEX IF EXISTS idx_player_stats_win_rate;

-- Drop table
DROP TABLE IF EXISTS player_stats;

-- Verify cleanup
SELECT 'Rollback complete. Remaining artifacts: ' || COUNT(*) AS status
FROM sqlite_master 
WHERE name LIKE '%player_stats%' OR name LIKE '%update_stats%';

COMMIT;
