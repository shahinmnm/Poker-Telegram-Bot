#!/usr/bin/env bash
set -euo pipefail

DB_PATH="data/poker.db"

if [[ ! -f "$DB_PATH" ]]; then
  echo "[WARN] Database file '$DB_PATH' not found. Please run from repo root with populated database."
  exit 1
fi

run_explain() {
  local description="$1"
  local query="$2"

  echo "\n==> $description"
  sqlite3 "$DB_PATH" "EXPLAIN QUERY PLAN $query" | sed 's/^/    /'
}

run_explain "Leaderboard aggregation" $'SELECT user_id, SUM(amount_won) FROM hands_players hp JOIN hands h ON h.id = hp.hand_id WHERE h.chat_id = (SELECT chat_id FROM hands LIMIT 1) GROUP BY user_id ORDER BY SUM(amount_won) DESC LIMIT 10;'

run_explain "Player stats per chat" $'SELECT COUNT(*), SUM(hp.amount_won) FROM hands h INNER JOIN hands_players hp ON hp.hand_id = h.id AND hp.user_id = (SELECT user_id FROM hands_players LIMIT 1) WHERE h.chat_id = (SELECT chat_id FROM hands LIMIT 1);'

run_explain "Recent hands" $'SELECT id, pot_size, winning_hand_type FROM hands WHERE chat_id = (SELECT chat_id FROM hands LIMIT 1) ORDER BY completed_at DESC LIMIT 20;'

run_explain "Hands-player amount aggregation" $'SELECT SUM(amount_won) FROM hands_players WHERE hand_id IN (SELECT id FROM hands WHERE chat_id = (SELECT chat_id FROM hands LIMIT 1));'

run_explain "User history across chats" $'SELECT hand_id FROM hands_players WHERE user_id = (SELECT user_id FROM hands_players LIMIT 1) ORDER BY hand_id DESC LIMIT 5;'

echo "\nVerification complete. Ensure each plan reports SEARCH ... USING INDEX for the idx_* indexes."

