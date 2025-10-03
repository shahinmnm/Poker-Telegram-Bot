-- ============================================================================
-- Migration: 002_add_performance_indexes.sql
-- Priority: 2B - Step 1 (Incremental Deployment)
-- Purpose: Add composite indexes for leaderboard and player stats queries
-- Risk: LOW (read-only, no data modification)
-- Expected Impact: 3-6x query performance improvement
-- Estimated Duration: 10-30 seconds (depends on table size)
-- ============================================================================

-- Start transaction for atomic application
BEGIN TRANSACTION;

-- ============================================================================
-- INDEX 1: Chat-scoped hand lookups
-- Supports: WHERE chat_id = ? ORDER BY completed_at DESC LIMIT N
-- Use case: Recent hands, date-filtered queries, leaderboard joins
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_hands_chat_completed 
ON hands(chat_id, completed_at DESC);

-- ============================================================================
-- INDEX 2: Hands-players join optimization
-- Supports: JOIN hands_players ON hand_id = ? AND user_id = ?
-- Use case: Player stats queries, win/loss aggregations
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_hands_players_hand_user 
ON hands_players(hand_id, user_id);

-- ============================================================================
-- INDEX 3: User profile lookups
-- Supports: LEFT JOIN users ON id = ? to fetch username
-- Use case: Leaderboard display with usernames
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_users_id_username 
ON users(id, username);

-- ============================================================================
-- INDEX 4: Covering index for recent hands (includes frequently accessed cols)
-- Supports: SELECT id, pot_size, winning_hand_type WHERE chat_id = ?
-- Use case: Recent hands display without full table scan
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_hands_chat_completed_covering 
ON hands(chat_id, completed_at DESC, id, pot_size, winning_hand_type);

-- ============================================================================
-- INDEX 5: Hands-players amount aggregation
-- Supports: WHERE hand_id IN (chat-filtered hands) for SUM/AVG
-- Use case: Leaderboard profit calculations
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_hands_players_hand_amount 
ON hands_players(hand_id, amount_won);

-- ============================================================================
-- INDEX 6: User history across chats
-- Supports: WHERE user_id = ? for cross-chat player lookups
-- Use case: Global player statistics (future feature)
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_hands_players_user_hand 
ON hands_players(user_id, hand_id);

-- Commit all indexes atomically
COMMIT;

-- ============================================================================
-- POST-MIGRATION VERIFICATION
-- These queries should show index usage in EXPLAIN QUERY PLAN
-- ============================================================================

-- Test 1: Leaderboard query should use idx_hands_chat_completed
-- EXPLAIN QUERY PLAN
-- SELECT user_id, SUM(amount_won) FROM hands_players hp
-- JOIN hands h ON h.id = hp.hand_id
-- WHERE h.chat_id = 123 GROUP BY user_id ORDER BY SUM(amount_won) DESC LIMIT 10;

-- Test 2: Player stats query should use idx_hands_players_hand_user
-- EXPLAIN QUERY PLAN
-- SELECT COUNT(*), SUM(hp.amount_won) FROM hands h
-- INNER JOIN hands_players hp ON hp.hand_id = h.id AND hp.user_id = 456
-- WHERE h.chat_id = 123;

-- Test 3: Recent hands should use idx_hands_chat_completed_covering
-- EXPLAIN QUERY PLAN
-- SELECT id, pot_size, winning_hand_type FROM hands
-- WHERE chat_id = 123 ORDER BY completed_at DESC LIMIT 20;

-- ============================================================================
-- EXPECTED RESULTS
-- ============================================================================
-- Before indexes:
--   - Leaderboard query: 300-500ms (SCAN table hands)
--   - Player stats query: 150-300ms (SCAN table hands_players)
--   - Recent hands query: 50-100ms (SCAN table hands)
--
-- After indexes:
--   - Leaderboard query: 50-100ms (SEARCH hands USING INDEX idx_hands_chat_completed)
--   - Player stats query: 20-50ms (SEARCH hands_players USING INDEX idx_hands_players_hand_user)
--   - Recent hands query: 5-10ms (SEARCH hands USING INDEX idx_hands_chat_completed_covering)
--
-- Total improvement: 3-6x faster queries
-- ============================================================================
