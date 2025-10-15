
-- Migration 003: Create materialized stats
-- Transaction management is handled by SQLAlchemy migration runner
-- Do NOT include BEGIN/COMMIT statements in this file

-- Add missing columns with IF NOT EXISTS syntax
-- Ensure legacy schemas have the columns required by this migration.
ALTER TABLE hands_players
    ADD COLUMN IF NOT EXISTS amount_won INTEGER NOT NULL DEFAULT 0;

ALTER TABLE hands_players
    ADD COLUMN IF NOT EXISTS buyin_amount INTEGER NOT NULL DEFAULT 0;

ALTER TABLE hands
    ADD COLUMN IF NOT EXISTS completed_at TEXT;

-- PostgreSQL equivalents (execute manually when running against Postgres):
-- ALTER TABLE hands_players ADD COLUMN IF NOT EXISTS amount_won INTEGER DEFAULT 0;
-- ALTER TABLE hands_players ADD COLUMN IF NOT EXISTS buyin_amount INTEGER DEFAULT 0;
-- ALTER TABLE hands ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;

-- ============================================================================
-- PART 1: MATERIALIZED STATISTICS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS player_stats (
    user_id INTEGER PRIMARY KEY,
    username TEXT,

    -- Hand counters
    total_hands INTEGER NOT NULL DEFAULT 0,
    hands_won INTEGER NOT NULL DEFAULT 0,
    hands_lost INTEGER NOT NULL DEFAULT 0,

    -- Financial metrics
    total_winnings INTEGER NOT NULL DEFAULT 0,
    total_buyins INTEGER NOT NULL DEFAULT 0,
    biggest_win INTEGER NOT NULL DEFAULT 0,
    biggest_loss INTEGER NOT NULL DEFAULT 0,

    -- Streak tracking
    current_streak INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    worst_streak INTEGER NOT NULL DEFAULT 0,

    -- Temporal data
    last_played_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================================
-- PART 2: PERFORMANCE INDEXES
-- ============================================================================

-- Leaderboard queries (ORDER BY total_winnings DESC)
CREATE INDEX IF NOT EXISTS idx_player_stats_winnings 
    ON player_stats(total_winnings DESC, hands_won DESC);

-- Recent activity queries (last_played_at DESC)
CREATE INDEX IF NOT EXISTS idx_player_stats_last_played 
    ON player_stats(last_played_at DESC) 
    WHERE last_played_at IS NOT NULL;

-- Win rate queries (computed expression index)
CREATE INDEX IF NOT EXISTS idx_player_stats_win_rate 
    ON player_stats(
        CAST(hands_won AS REAL) / NULLIF(total_hands, 0) DESC,
        total_hands DESC
    ) 
    WHERE total_hands > 0;

-- ============================================================================
-- PART 3: INITIAL DATA POPULATION
-- ============================================================================

INSERT OR REPLACE INTO player_stats (
    user_id,
    username,
    total_hands,
    hands_won,
    hands_lost,
    total_winnings,
    total_buyins,
    biggest_win,
    biggest_loss,
    last_played_at,
    updated_at
)
SELECT 
    hp.user_id,
    MAX(u.username) AS username,
    COUNT(*) AS total_hands,
    SUM(CASE WHEN hp.amount_won > 0 THEN 1 ELSE 0 END) AS hands_won,
    SUM(CASE WHEN hp.amount_won <= 0 THEN 1 ELSE 0 END) AS hands_lost,
    SUM(hp.amount_won) AS total_winnings,
    SUM(hp.buyin_amount) AS total_buyins,
    MAX(hp.amount_won) AS biggest_win,
    MIN(hp.amount_won) AS biggest_loss,
    MAX(h.completed_at) AS last_played_at,
    datetime('now')
FROM hands_players hp
JOIN hands h ON hp.hand_id = h.id
LEFT JOIN users u ON hp.user_id = u.id
WHERE h.completed_at IS NOT NULL
GROUP BY hp.user_id;

-- ============================================================================
-- PART 4: AUTOMATIC MAINTENANCE TRIGGERS
-- ============================================================================

-- Helper to coalesce username when present
CREATE TRIGGER IF NOT EXISTS trg_player_stats_cleanup_username
AFTER INSERT ON users
BEGIN
    UPDATE player_stats
    SET username = COALESCE(username, NEW.username)
    WHERE user_id = NEW.id;
END;

-- Trigger 1: Update stats when hand completes
CREATE TRIGGER IF NOT EXISTS trg_update_stats_on_hand_complete
AFTER UPDATE OF completed_at ON hands
WHEN NEW.completed_at IS NOT NULL AND OLD.completed_at IS NULL
BEGIN
    INSERT OR REPLACE INTO player_stats (
        user_id,
        username,
        total_hands,
        hands_won,
        hands_lost,
        total_winnings,
        total_buyins,
        biggest_win,
        biggest_loss,
        current_streak,
        best_streak,
        worst_streak,
        last_played_at,
        created_at,
        updated_at
    )
    SELECT 
        hp.user_id,
        COALESCE((SELECT username FROM users WHERE id = hp.user_id), ps.username),
        COALESCE(ps.total_hands, 0) + 1,
        COALESCE(ps.hands_won, 0) + CASE WHEN hp.amount_won > 0 THEN 1 ELSE 0 END,
        COALESCE(ps.hands_lost, 0) + CASE WHEN hp.amount_won <= 0 THEN 1 ELSE 0 END,
        COALESCE(ps.total_winnings, 0) + hp.amount_won,
        COALESCE(ps.total_buyins, 0) + hp.buyin_amount,
        MAX(COALESCE(ps.biggest_win, 0), hp.amount_won),
        MIN(COALESCE(ps.biggest_loss, 0), hp.amount_won),
        CASE
            WHEN hp.amount_won > 0 THEN
                CASE WHEN COALESCE(ps.current_streak, 0) >= 0
                    THEN COALESCE(ps.current_streak, 0) + 1
                    ELSE 1
                END
            ELSE
                CASE WHEN COALESCE(ps.current_streak, 0) <= 0
                    THEN COALESCE(ps.current_streak, 0) - 1
                    ELSE -1
                END
        END,
        CASE
            WHEN hp.amount_won > 0 THEN MAX(COALESCE(ps.best_streak, 0),
                CASE WHEN COALESCE(ps.current_streak, 0) >= 0
                    THEN COALESCE(ps.current_streak, 0) + 1
                    ELSE 1
                END)
            ELSE COALESCE(ps.best_streak, 0)
        END,
        CASE
            WHEN hp.amount_won <= 0 THEN MIN(COALESCE(ps.worst_streak, 0),
                CASE WHEN COALESCE(ps.current_streak, 0) <= 0
                    THEN COALESCE(ps.current_streak, 0) - 1
                    ELSE -1
                END)
            ELSE COALESCE(ps.worst_streak, 0)
        END,
        NEW.completed_at,
        COALESCE(ps.created_at, datetime('now')),
        datetime('now')
    FROM hands_players hp
    LEFT JOIN player_stats ps ON hp.user_id = ps.user_id
    WHERE hp.hand_id = NEW.id;
END;

-- Trigger 2: Update stats when player result changes (manual corrections)
CREATE TRIGGER IF NOT EXISTS trg_update_stats_on_player_result
AFTER UPDATE OF amount_won, buyin_amount ON hands_players
BEGIN
    INSERT OR REPLACE INTO player_stats (
        user_id,
        username,
        total_hands,
        hands_won,
        hands_lost,
        total_winnings,
        total_buyins,
        biggest_win,
        biggest_loss,
        current_streak,
        best_streak,
        worst_streak,
        last_played_at,
        created_at,
        updated_at
    )
    SELECT 
        NEW.user_id,
        COALESCE((SELECT username FROM users WHERE id = NEW.user_id), ps.username),
        COALESCE(ps.total_hands, 0),
        COALESCE(ps.hands_won, 0) + CASE 
            WHEN NEW.amount_won > 0 AND OLD.amount_won <= 0 THEN 1
            WHEN NEW.amount_won <= 0 AND OLD.amount_won > 0 THEN -1
            ELSE 0
        END,
        COALESCE(ps.hands_lost, 0) + CASE 
            WHEN NEW.amount_won <= 0 AND OLD.amount_won > 0 THEN 1
            WHEN NEW.amount_won > 0 AND OLD.amount_won <= 0 THEN -1
            ELSE 0
        END,
        COALESCE(ps.total_winnings, 0) - OLD.amount_won + NEW.amount_won,
        COALESCE(ps.total_buyins, 0) - OLD.buyin_amount + NEW.buyin_amount,
        MAX(COALESCE(ps.biggest_win, 0), NEW.amount_won),
        MIN(COALESCE(ps.biggest_loss, 0), NEW.amount_won),
        CASE
            WHEN NEW.amount_won > 0 THEN
                CASE WHEN COALESCE(ps.current_streak, 0) >= 0
                    THEN COALESCE(ps.current_streak, 0) + 1
                    ELSE 1
                END
            WHEN NEW.amount_won <= 0 THEN
                CASE WHEN COALESCE(ps.current_streak, 0) <= 0
                    THEN COALESCE(ps.current_streak, 0) - 1
                    ELSE -1
                END
            ELSE COALESCE(ps.current_streak, 0)
        END,
        CASE
            WHEN NEW.amount_won > 0 THEN MAX(COALESCE(ps.best_streak, 0),
                CASE WHEN COALESCE(ps.current_streak, 0) >= 0
                    THEN COALESCE(ps.current_streak, 0) + 1
                    ELSE 1
                END)
            ELSE COALESCE(ps.best_streak, 0)
        END,
        CASE
            WHEN NEW.amount_won <= 0 THEN MIN(COALESCE(ps.worst_streak, 0),
                CASE WHEN COALESCE(ps.current_streak, 0) <= 0
                    THEN COALESCE(ps.current_streak, 0) - 1
                    ELSE -1
                END)
            ELSE COALESCE(ps.worst_streak, 0)
        END,
        ps.last_played_at,
        COALESCE(ps.created_at, datetime('now')),
        datetime('now')
    FROM player_stats ps
    WHERE ps.user_id = NEW.user_id;
END;

-- Trigger 3: Sync username changes from users table
CREATE TRIGGER IF NOT EXISTS trg_sync_username_to_stats
AFTER UPDATE OF username ON users
BEGIN
    UPDATE player_stats
    SET username = NEW.username,
        updated_at = datetime('now')
    WHERE user_id = NEW.id;
END;

-- ============================================================================
-- VERIFICATION QUERY (for deployment logs)
-- ============================================================================

SELECT
    'Phase 2 Migration Complete' AS status,
    (SELECT COUNT(*) FROM player_stats) AS player_count,
    (SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%stats%') AS trigger_count,
    (SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name='player_stats') AS index_count;
