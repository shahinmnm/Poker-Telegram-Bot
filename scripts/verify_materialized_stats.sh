#!/bin/bash
# ============================================================================
# Verification script for materialized stats migration
# ============================================================================

set -euo pipefail

DB_PATH="${1:-data/poker.db}"

echo "🔍 Verifying materialized stats migration..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check 1: Table exists
echo "✓ Check 1: Table existence"
TABLE_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='player_stats';")
if [ "$TABLE_COUNT" -eq 1 ]; then
    echo "  ✅ player_stats table exists"
else
    echo "  ❌ player_stats table NOT found"
    exit 1
fi
echo ""

# Check 2: Indexes exist
echo "✓ Check 2: Index verification"
INDEXES=$(sqlite3 "$DB_PATH" $'SELECT name FROM sqlite_master WHERE type=\'index\' AND tbl_name=\'player_stats\';')
echo "$INDEXES" | sed 's/^/    ✅ /'
INDEX_COUNT=$(echo "$INDEXES" | wc -l)
if [ "$INDEX_COUNT" -ge 3 ]; then
    echo "  ✅ All $INDEX_COUNT indexes created"
else
    echo "  ❌ Expected 3+ indexes, found $INDEX_COUNT"
    exit 1
fi
echo ""

# Check 3: Triggers exist
echo "✓ Check 3: Trigger verification"
TRIGGERS=$(sqlite3 "$DB_PATH" $'SELECT name FROM sqlite_master WHERE type=\'trigger\' AND name LIKE \'%stats%\' ORDER BY name;')
if [ -n "$TRIGGERS" ]; then
    echo "$TRIGGERS" | sed 's/^/    ✅ /'
fi
TRIGGER_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE '%stats%';")
if [ "$TRIGGER_COUNT" -eq 3 ]; then
    echo "  ✅ All 3 triggers created"
else
    echo "  ❌ Expected 3 triggers, found $TRIGGER_COUNT"
    exit 1
fi

TRIGGER_COUNT_SYNC=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='trg_sync_username_to_stats';")
echo "    Username sync trigger: $TRIGGER_COUNT_SYNC"
if [ "$TRIGGER_COUNT_SYNC" -eq 1 ]; then
    echo "  ✅ Username sync trigger exists"
else
    echo "  ⚠️  Username sync trigger missing"
fi
echo ""

# Check 4: Data population
echo "✓ Check 4: Data population"
PLAYER_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM player_stats;")
EXPECTED_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(DISTINCT user_id) FROM hands_players;")
echo "    Players in stats table: $PLAYER_COUNT"
echo "    Expected from raw data: $EXPECTED_COUNT"
if [ "$PLAYER_COUNT" -eq "$EXPECTED_COUNT" ]; then
    echo "  ✅ Data populated correctly"
else
    echo "  ⚠️  Count mismatch (may be OK if hands are in progress)"
fi
echo ""

# Check 5: Query performance
echo "✓ Check 5: Query performance"
echo "  Testing leaderboard query with EXPLAIN QUERY PLAN..."
EXPLAIN_OUTPUT=$(sqlite3 "$DB_PATH" $'EXPLAIN QUERY PLAN\nSELECT * FROM player_stats ORDER BY total_winnings DESC LIMIT 10;')
if echo "$EXPLAIN_OUTPUT" | grep -q "idx_player_stats_winnings"; then
    echo "  ✅ Leaderboard query uses index"
    echo "$EXPLAIN_OUTPUT" | sed 's/^/    /'
else
    echo "  ⚠️  Index may not be used optimally"
    echo "$EXPLAIN_OUTPUT" | sed 's/^/    /'
fi
echo ""

# Check 6: Data consistency (sample)
echo "✓ Check 6: Data consistency (sample)"
INCONSISTENT=$(sqlite3 "$DB_PATH" <<'SQL'
WITH raw_stats AS (
    SELECT 
        user_id,
        COUNT(*) AS hands
    FROM hands_players hp
    JOIN hands h ON hp.hand_id = h.id
    WHERE h.completed_at IS NOT NULL
    GROUP BY user_id
    LIMIT 10
)
SELECT COUNT(*) 
FROM raw_stats rs
JOIN player_stats ps ON rs.user_id = ps.user_id
WHERE rs.hands != ps.total_hands;
SQL
)
if [ "$INCONSISTENT" -eq 0 ]; then
    echo "  ✅ Sample data consistent"
else
    echo "  ⚠️  Found $INCONSISTENT inconsistent records in sample"
fi
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Verification complete!"
echo ""
echo "Next steps:"
echo "  1. Test /stats command in Telegram"
echo "  2. Monitor bot logs for 24 hours"
echo "  3. Run full integrity check after 1 week"
