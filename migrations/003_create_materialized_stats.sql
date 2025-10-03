-- =============================================================================
-- Migration 003: Create Materialized Player Statistics Table
-- =============================================================================
-- DESIGN DECISION: Global Statistics
--
-- This table stores GLOBAL player statistics across ALL chats.
-- Rationale:
--   - Simpler schema (no chat_id column needed)
--   - Players have one unified reputation/track record
--   - Easier cross-chat leaderboards
--   - Matches original bot design philosophy
--
-- Note: Code uses chat-scoped cache invalidation for performance optimization,
--       but the underlying stats remain global. This is intentional.
-- =============================================================================
-- Purpose: Create pre-aggregated stats table for 10-20x faster queries
-- Dependencies: Requires migrations 001 and 002
-- Rollback: See scripts/rollback_003.sql
-- =============================================================================

BEGIN TRANSACTION;

-- ============================================================================
-- PART 1: Create materialized stats table
-- ============================================================================

CREATE TABLE IF NOT EXISTS player_stats (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    
    -- Hand counts
    total_hands INTEGER NOT NULL DEFAULT 0,
    hands_won INTEGER NOT NULL DEFAULT 0,
    hands_lost INTEGER NOT NULL DEFAULT 0,
    
    -- Financial stats
    total_winnings INTEGER NOT NULL DEFAULT 0,
    total_losses INTEGER NOT NULL DEFAULT 0,
    net_profit INTEGER NOT NULL DEFAULT 0,
    biggest_pot INTEGER NOT NULL DEFAULT 0,
    
    -- Performance metrics
    win_rate REAL NOT NULL DEFAULT 0.0,
    avg_pot_size REAL NOT NULL DEFAULT 0.0,
    
    -- Temporal tracking
    first_played_at TEXT,
    last_played_at TEXT,
    
    -- Cache metadata
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Index for leaderboard queries (replaces expensive JOINs)
CREATE INDEX IF NOT EXISTS idx_player_stats_winnings 
    ON player_stats(total_winnings DESC, hands_won DESC);

-- Index for recent activity queries
CREATE INDEX IF NOT EXISTS idx_player_stats_last_played 
    ON player_stats(last_played_at DESC);

-- Index for win rate calculations
CREATE INDEX IF NOT EXISTS idx_player_stats_win_rate 
    ON player_stats(win_rate DESC, total_hands DESC);

-- ============================================================================
-- PART 2: Initial population from existing data
-- ============================================================================

INSERT OR REPLACE INTO player_stats (
    user_id,
    username,
    total_hands,
    hands_won,
    hands_lost,
    total_winnings,
    total_losses,
    net_profit,
    biggest_pot,
    win_rate,
    avg_pot_size,
    first_played_at,
    last_played_at,
    updated_at
)
SELECT 
    hp.user_id,
    u.username,
    COUNT(DISTINCT hp.hand_id) AS total_hands,
    SUM(CASE WHEN hp.amount_won > 0 THEN 1 ELSE 0 END) AS hands_won,
    SUM(CASE WHEN hp.amount_won = 0 THEN 1 ELSE 0 END) AS hands_lost,
    COALESCE(SUM(CASE WHEN hp.amount_won > 0 THEN hp.amount_won ELSE 0 END), 0) AS total_winnings,
    COALESCE(SUM(CASE WHEN hp.amount_won < 0 THEN ABS(hp.amount_won) ELSE 0 END), 0) AS total_losses,
    COALESCE(SUM(hp.amount_won), 0) AS net_profit,
    COALESCE(MAX(hp.amount_won), 0) AS biggest_pot,
    COALESCE(
        ROUND(
            CAST(SUM(CASE WHEN hp.amount_won > 0 THEN 1 ELSE 0 END) AS REAL) /
            NULLIF(COUNT(DISTINCT hp.hand_id), 0) * 100,
            2
        ),
        0.0
    ) AS win_rate,
    ROUND(
        CAST(SUM(hp.amount_won) AS REAL) / 
        NULLIF(COUNT(DISTINCT hp.hand_id), 0), 
        2
    ) AS avg_pot_size,
    MIN(h.completed_at) AS first_played_at,
    MAX(h.completed_at) AS last_played_at,
    CURRENT_TIMESTAMP AS updated_at
FROM 
    hands_players hp
    INNER JOIN users u ON hp.user_id = u.id
    INNER JOIN hands h ON hp.hand_id = h.id
WHERE 
    h.completed_at IS NOT NULL
GROUP BY 
    hp.user_id, u.username;

-- ============================================================================
-- PART 3: Triggers for automatic updates
-- ============================================================================

-- Trigger 1: Update stats when hand completes
CREATE TRIGGER IF NOT EXISTS trg_update_stats_on_hand_complete
AFTER UPDATE OF completed_at ON hands
FOR EACH ROW
WHEN NEW.completed_at IS NOT NULL AND OLD.completed_at IS NULL
BEGIN
    -- Update stats for all players in this hand
    INSERT OR REPLACE INTO player_stats (
        user_id,
        username,
        total_hands,
        hands_won,
        hands_lost,
        total_winnings,
        total_losses,
        net_profit,
        biggest_pot,
        win_rate,
        avg_pot_size,
        first_played_at,
        last_played_at,
        updated_at
    )
    SELECT 
        hp.user_id,
        u.username,
        COALESCE(ps.total_hands, 0) + 1,
        COALESCE(ps.hands_won, 0) + CASE WHEN hp.amount_won > 0 THEN 1 ELSE 0 END,
        COALESCE(ps.hands_lost, 0) + CASE WHEN hp.amount_won = 0 THEN 1 ELSE 0 END,
        COALESCE(ps.total_winnings, 0) + CASE WHEN hp.amount_won > 0 THEN hp.amount_won ELSE 0 END,
        COALESCE(ps.total_losses, 0) + CASE WHEN hp.amount_won < 0 THEN ABS(hp.amount_won) ELSE 0 END,
        COALESCE(ps.net_profit, 0) + hp.amount_won,
        MAX(COALESCE(ps.biggest_pot, 0), hp.amount_won),
        COALESCE(
            ROUND(
                CAST(COALESCE(ps.hands_won, 0) + CASE WHEN hp.amount_won > 0 THEN 1 ELSE 0 END AS REAL) /
                NULLIF(COALESCE(ps.total_hands, 0) + 1, 0) * 100,
                2
            ),
            0.0
        ),
        ROUND(
            CAST(COALESCE(ps.net_profit, 0) + hp.amount_won AS REAL) / 
            NULLIF(COALESCE(ps.total_hands, 0) + 1, 0),
            2
        ),
        COALESCE(ps.first_played_at, NEW.completed_at),
        NEW.completed_at,
        CURRENT_TIMESTAMP
    FROM 
        hands_players hp
        INNER JOIN users u ON hp.user_id = u.id
        LEFT JOIN player_stats ps ON hp.user_id = ps.user_id
    WHERE 
        hp.hand_id = NEW.id;
END;

-- Trigger 2: Update stats when player result is recorded
CREATE TRIGGER IF NOT EXISTS trg_update_stats_on_player_result
AFTER INSERT ON hands_players
FOR EACH ROW
WHEN (SELECT completed_at FROM hands WHERE id = NEW.hand_id) IS NOT NULL
BEGIN
    INSERT OR REPLACE INTO player_stats (
        user_id,
        username,
        total_hands,
        hands_won,
        hands_lost,
        total_winnings,
        total_losses,
        net_profit,
        biggest_pot,
        win_rate,
        avg_pot_size,
        first_played_at,
        last_played_at,
        updated_at
    )
    SELECT 
        NEW.user_id,
        u.username,
        COALESCE(ps.total_hands, 0) + 1,
        COALESCE(ps.hands_won, 0) + CASE WHEN NEW.amount_won > 0 THEN 1 ELSE 0 END,
        COALESCE(ps.hands_lost, 0) + CASE WHEN NEW.amount_won = 0 THEN 1 ELSE 0 END,
        COALESCE(ps.total_winnings, 0) + CASE WHEN NEW.amount_won > 0 THEN NEW.amount_won ELSE 0 END,
        COALESCE(ps.total_losses, 0) + CASE WHEN NEW.amount_won < 0 THEN ABS(NEW.amount_won) ELSE 0 END,
        COALESCE(ps.net_profit, 0) + NEW.amount_won,
        MAX(COALESCE(ps.biggest_pot, 0), NEW.amount_won),
        COALESCE(
            ROUND(
                CAST(COALESCE(ps.hands_won, 0) + CASE WHEN NEW.amount_won > 0 THEN 1 ELSE 0 END AS REAL) /
                NULLIF(COALESCE(ps.total_hands, 0) + 1, 0) * 100,
                2
            ),
            0.0
        ),
        ROUND(
            CAST(COALESCE(ps.net_profit, 0) + NEW.amount_won AS REAL) / 
            NULLIF(COALESCE(ps.total_hands, 0) + 1, 0),
            2
        ),
        COALESCE(ps.first_played_at, (SELECT completed_at FROM hands WHERE id = NEW.hand_id)),
        (SELECT completed_at FROM hands WHERE id = NEW.hand_id),
        CURRENT_TIMESTAMP
    FROM 
        users u
        LEFT JOIN player_stats ps ON u.id = NEW.user_id
    WHERE 
        u.id = NEW.user_id;
END;

-- Trigger 3: Sync username changes into stats table
CREATE TRIGGER IF NOT EXISTS trg_sync_username_to_stats
AFTER UPDATE OF username ON users
FOR EACH ROW
WHEN NEW.username IS NOT NULL AND NEW.username != OLD.username
BEGIN
    UPDATE player_stats
    SET
        username = NEW.username,
        updated_at = CURRENT_TIMESTAMP
    WHERE user_id = NEW.id;
END;

-- ============================================================================
-- PART 4: Verification queries
-- ============================================================================

-- Test 1: Verify stats table is populated
-- Expected: Row count matches distinct user count in hands_players
SELECT 'Stats table populated: ' || COUNT(*) || ' players' AS test_1
FROM player_stats;

-- Test 2: Verify indexes exist
SELECT 'Indexes created: ' || COUNT(*) || ' indexes' AS test_2
FROM sqlite_master 
WHERE type = 'index' 
  AND tbl_name = 'player_stats';

-- Test 3: Verify triggers exist
SELECT 'Triggers created: ' || COUNT(*) || ' triggers' AS test_3
FROM sqlite_master 
WHERE type = 'trigger' 
  AND (name LIKE '%update_stats%' OR name = 'trg_sync_username_to_stats');

-- Test 4: Sample query performance (leaderboard top 10)
EXPLAIN QUERY PLAN
SELECT 
    username,
    total_winnings,
    hands_won,
    win_rate
FROM 
    player_stats
ORDER BY 
    total_winnings DESC, 
    hands_won DESC
LIMIT 10;

-- Test 5: Sample query performance (player profile)
EXPLAIN QUERY PLAN
SELECT 
    username,
    total_hands,
    hands_won,
    hands_lost,
    total_winnings,
    net_profit,
    biggest_pot,
    win_rate,
    avg_pot_size,
    last_played_at
FROM 
    player_stats
WHERE 
    user_id = 12345;

COMMIT;

-- ============================================================================
-- Expected Performance Improvements
-- ============================================================================
-- Leaderboard query (before): 300-500ms with JOINs
-- Leaderboard query (after):  5-10ms with direct SELECT
-- Improvement: 30-100x faster
--
-- Player profile (before): 100-200ms with aggregation
-- Player profile (after):  1-2ms with direct lookup
-- Improvement: 50-200x faster
-- ============================================================================
