# Step 1 Deployment: Performance Indexes

## 📋 Pre-Deployment Checklist

### 1. Backup Database
```bash
# Create timestamped backup
BACKUP_FILE="data/poker_backup_$(date +%Y%m%d_%H%M%S).db"
sqlite3 data/poker.db ".backup ${BACKUP_FILE}"

# Verify backup integrity
sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;"
# Expected: ok

# Check backup size
ls -lh "${BACKUP_FILE}"
```

### 2. Check Current Schema
```bash
# List existing indexes
sqlite3 data/poker.db ".indexes"

# Verify no conflicts with new index names
sqlite3 data/poker.db "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%';"

# Expected: Empty or existing indexes not named idx_hands_chat_completed, etc.
```

### 3. Estimate Table Sizes
```bash
# Check row counts (affects index creation time)
sqlite3 data/poker.db <<'SQL'
SELECT 'hands', COUNT(*) FROM hands
UNION ALL
SELECT 'hands_players', COUNT(*) FROM hands_players
UNION ALL
SELECT 'users', COUNT(*) FROM users;
SQL

# Estimated index creation time:
# < 1,000 rows:    ~1 second
# 1,000-10,000:    ~5 seconds
# 10,000-100,000:  ~20 seconds
# 100,000+:        ~60 seconds
```

### 4. Check Disk Space
```bash
# Current database size
du -h data/poker.db

# Indexes typically add 20-30% to database size
# Ensure you have at least 2x current size available

df -h data/
```

---

## 🚀 Deployment Steps

### Step 1: Stop Bot (Recommended)

**Note**: Bot can remain running, but stopping prevents lock contention during index creation.
```bash
# Graceful stop
docker-compose stop bot

# Verify no connections
lsof -i :8080  # Should be empty
```

### Step 2: Apply Migration
```bash
# Apply indexes
sqlite3 data/poker.db < migrations/002_add_performance_indexes.sql

# Check for errors
echo $?
# Expected: 0 (success)
```

**If using Docker**:
```bash
# Copy migration into container
docker cp migrations/002_add_performance_indexes.sql poker_bot:/app/migrations/

# Apply migration
docker-compose exec -T bot sqlite3 /app/data/poker.db < migrations/002_add_performance_indexes.sql
```

### Step 3: Verify Indexes Created
```bash
# List all indexes
sqlite3 data/poker.db ".indexes hands"

# Expected output:
# idx_hands_chat_completed
# idx_hands_chat_completed_covering

sqlite3 data/poker.db ".indexes hands_players"

# Expected output:
# idx_hands_players_hand_user
# idx_hands_players_hand_amount
# idx_hands_players_user_hand

sqlite3 data/poker.db ".indexes users"

# Expected output:
# idx_users_id_username
```

### Step 4: Run Verification Script
```bash
# Execute verification queries
bash scripts/verify_indexes.sh

# Expected: All queries show "SEARCH ... USING INDEX idx_..."
```

### Step 5: Restart Bot
```bash
# Start bot
docker-compose up -d bot

# Monitor logs for errors
docker-compose logs -f bot
```

---

## ✅ Verification Checklist

### 1. Index Existence
```bash
# Count created indexes
INDEX_COUNT=$(sqlite3 data/poker.db "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%';")
echo "Created indexes: ${INDEX_COUNT}"
# Expected: 6
```

### 2. Query Performance

**Before indexes** (baseline - if you have old measurements):
```bash
# Example timing for leaderboard query
sqlite3 data/poker.db <<'SQL'
.timer ON
SELECT user_id, SUM(amount_won) 
FROM hands_players hp
JOIN hands h ON h.id = hp.hand_id
WHERE h.chat_id = (SELECT chat_id FROM hands LIMIT 1)
GROUP BY user_id
ORDER BY SUM(amount_won) DESC
LIMIT 10;
SQL

# Baseline: ~300-500ms (without indexes)
```

**After indexes**:
```bash
# Same query should now use indexes
sqlite3 data/poker.db <<'SQL'
.timer ON
SELECT user_id, SUM(amount_won) 
FROM hands_players hp
JOIN hands h ON h.id = hp.hand_id
WHERE h.chat_id = (SELECT chat_id FROM hands LIMIT 1)
GROUP BY user_id
ORDER BY SUM(amount_won) DESC
LIMIT 10;
SQL

# Target: ~50-100ms (3-6x faster)
```

### 3. Index Usage Verification
```bash
# Run verification script
bash scripts/verify_indexes.sh

# All queries should show:
# SEARCH ... USING INDEX idx_...
# NOT: SCAN TABLE ...
```

### 4. Bot Health Check
```bash
# Check bot is running
docker-compose ps bot
# State should be: Up

# Check logs for errors
docker-compose logs --tail=50 bot | grep -i error
# Should be empty or unrelated to indexes

# Test /stats command in Telegram
# Should work normally (no user-visible changes yet)
```

---
## 📊 Success Metrics

**✅ Deployment successful if:**

1. **All 6 indexes created** without errors
2. **Query plans show index usage** (SEARCH ... USING INDEX)
3. **Leaderboard queries 3-6x faster** (50-100ms vs 300-500ms)
4. **No bot errors** in logs
5. **Bot responds normally** to /stats commands
6. **Database size increased** by ~20-30%

