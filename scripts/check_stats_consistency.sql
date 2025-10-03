-- ============================================================================
-- Data Consistency Check for player_stats Table
-- ============================================================================
-- Purpose: Compare materialized stats with raw aggregation
-- Usage: sqlite3 data/poker.db < scripts/check_stats_consistency.sql
-- ============================================================================

.mode column
.headers on

-- Full comparison of materialized vs raw stats
WITH raw_stats AS (
    SELECT 
        hp.user_id,
        COUNT(DISTINCT hp.hand_id) AS total_hands,
        SUM(CASE WHEN hp.amount_won > 0 THEN 1 ELSE 0 END) AS hands_won,
        COALESCE(SUM(CASE WHEN hp.amount_won > 0 THEN hp.amount_won ELSE 0 END), 0) AS total_winnings,
        COALESCE(SUM(hp.amount_won), 0) AS net_profit,
        COALESCE(MAX(hp.amount_won), 0) AS biggest_pot
    FROM 
        hands_players hp
        INNER JOIN hands h ON hp.hand_id = h.id
    WHERE 
        h.completed_at IS NOT NULL
    GROUP BY 
        hp.user_id
)
SELECT 
    'Consistency Report' AS report_title,
    COUNT(*) AS total_players,
    SUM(CASE 
        WHEN rs.total_hands != ps.total_hands 
          OR rs.hands_won != ps.hands_won
          OR rs.total_winnings != ps.total_winnings
          OR rs.net_profit != ps.net_profit
          OR rs.biggest_pot != ps.biggest_pot
        THEN 1 ELSE 0 
    END) AS inconsistent_players,
    ROUND(
        CAST(SUM(CASE 
            WHEN rs.total_hands = ps.total_hands 
              AND rs.hands_won = ps.hands_won
              AND rs.total_winnings = ps.total_winnings
              AND rs.net_profit = ps.net_profit
              AND rs.biggest_pot = ps.biggest_pot
            THEN 1 ELSE 0 
        END) AS REAL) / COUNT(*) * 100,
        2
    ) AS consistency_rate_percent
FROM 
    raw_stats rs
    INNER JOIN player_stats ps ON rs.user_id = ps.user_id;

-- Show sample of inconsistent records
SELECT 
    rs.user_id,
    u.username,
    rs.total_hands AS raw_hands,
    ps.total_hands AS mat_hands,
    rs.total_winnings AS raw_winnings,
    ps.total_winnings AS mat_winnings
FROM (
    SELECT 
        hp.user_id,
        COUNT(DISTINCT hp.hand_id) AS total_hands,
        COALESCE(SUM(CASE WHEN hp.amount_won > 0 THEN hp.amount_won ELSE 0 END), 0) AS total_winnings
    FROM 
        hands_players hp
        INNER JOIN hands h ON hp.hand_id = h.id
    WHERE 
        h.completed_at IS NOT NULL
    GROUP BY 
        hp.user_id
) rs
INNER JOIN player_stats ps ON rs.user_id = ps.user_id
INNER JOIN users u ON rs.user_id = u.id
WHERE 
    rs.total_hands != ps.total_hands 
    OR rs.total_winnings != ps.total_winnings
LIMIT 10;
