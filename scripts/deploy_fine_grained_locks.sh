#!/usr/bin/env bash
set -euo pipefail

# Fine-Grained Locks Gradual Rollout Script
# Usage: ./scripts/deploy_fine_grained_locks.sh [percentage]

PERCENTAGE=${1:-10}
CONFIG_FILE="config/system_constants.json"
BACKUP_FILE="config/system_constants.json.backup.$(date +%s)"

echo "🚀 Starting Fine-Grained Locks Rollout"
echo "   Target Percentage: ${PERCENTAGE}%"
echo ""

# Backup current config
echo "📦 Backing up config to ${BACKUP_FILE}"
cp "$CONFIG_FILE" "$BACKUP_FILE"

# Update rollout percentage
echo "⚙️  Updating rollout percentage to ${PERCENTAGE}%"
jq --arg pct "$PERCENTAGE" \
  '.lock_manager.rollout_percentage = ($pct | tonumber)' \
  "$CONFIG_FILE" > "$CONFIG_FILE.tmp"
mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

# Reload bot config (sends SIGHUP to main process)
echo "🔄 Reloading bot configuration"
pkill -HUP -f "python.*main.py" || echo "   (Bot not running, config will apply on next start)"

# Wait for metrics
echo ""
echo "⏳ Waiting 60 seconds for metrics..."
sleep 60

# Check health
echo "🏥 Checking rollout health..."
HEALTH_ENDPOINT="http://localhost:8000/health/fine_grained_locks"
HEALTH=$(curl -s "$HEALTH_ENDPOINT" || echo '{"healthy": false}')

if echo "$HEALTH" | jq -e '.healthy' > /dev/null; then
  echo "✅ Rollout is healthy!"
  echo ""
  echo "📊 Metrics:"
  echo "$HEALTH" | jq .
  exit 0
else
  echo "❌ Rollout is UNHEALTHY - triggering rollback"
  echo ""
  echo "📊 Failure Metrics:"
  echo "$HEALTH" | jq .
  
  # Restore backup
  echo "🔙 Restoring config from backup"
  cp "$BACKUP_FILE" "$CONFIG_FILE"
  pkill -HUP -f "python.*main.py"
  
  exit 1
fi
