-- ============================================================================
-- Migration 004: Phase 2 Player Statistics Materialization
-- ============================================================================
-- Purpose: Ensure the player_stats table, indexes, data, and triggers exist
--          for the Phase 2 statistics rollout.
-- Execution: sqlite3 data/poker.db < migrations/004_phase2_player_stats.sql
-- ============================================================================

PRAGMA foreign_keys = ON;

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS player_stats (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    total_hands INTEGER NOT NULL DEFAULT 0,
    hands_won INTEGER NOT NULL DEFAULT 0,
    hands_lost INTEGER NOT NULL DEFAULT 0,
    total_winnings INTEGER NOT NULL DEFAULT 0,
    total_losses INTEGER NOT NULL DEFAULT 0,
    net_profit INTEGER NOT NULL DEFAULT 0,
    biggest_pot INTEGER NOT NULL DEFAULT 0,
    win_rate REAL NOT NULL DEFAULT 0.0,
    avg_pot_size REAL NOT NULL DEFAULT 0.0,
    first_played_at TEXT,
    last_played_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_player_stats_winnings
    ON player_stats(total_winnings DESC, hands_won DESC);

CREATE INDEX IF NOT EXISTS idx_player_stats_last_played
    ON player_stats(last_played_at DESC);

CREATE INDEX IF NOT EXISTS idx_player_stats_win_rate
    ON player_stats(win_rate DESC, total_hands DESC);

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
    hp.user_id,
    u.username;

CREATE TRIGGER IF NOT EXISTS trg_update_stats_on_hand_complete
AFTER UPDATE OF completed_at ON hands
FOR EACH ROW
WHEN NEW.completed_at IS NOT NULL AND OLD.completed_at IS NULL
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

COMMIT;
