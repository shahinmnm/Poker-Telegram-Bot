-- Rollback for migration 002_add_performance_indexes.sql
BEGIN TRANSACTION;

DROP INDEX IF EXISTS idx_hands_chat_completed;
DROP INDEX IF EXISTS idx_hands_players_hand_user;
DROP INDEX IF EXISTS idx_users_id_username;
DROP INDEX IF EXISTS idx_hands_chat_completed_covering;
DROP INDEX IF EXISTS idx_hands_players_hand_amount;
DROP INDEX IF EXISTS idx_hands_players_user_hand;

COMMIT;

