#!/usr/bin/env bash
#
# Verify Grafana dashboard provisioning is working correctly
#

set -euo pipefail

echo "╔════════════════════════════════════════════════════════════╗"
echo "║       GRAFANA DASHBOARD PROVISIONING VERIFICATION          ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

ERRORS=0

# Check 1: Verify local JSON files have titles
echo "📋 Step 1: Checking local dashboard files..."
DASHBOARDS=(
  "config/grafana/alerting_health_dashboard.json:Alerting System Health:alerting-health"
  "config/grafana/smart_retry_dashboard.json:Smart Retry System Health:smart-retry-health"
  "config/grafana/fine_grained_locks_dashboard.json:Fine-Grained Lock Performance:fine-grained-locks"
  "config/grafana/pruning_health_dashboard.json:Database Pruning Health:pruning-health"
)

for dashboard_info in "${DASHBOARDS[@]}"; do
  IFS=':' read -r file expected_title expected_uid <<< "$dashboard_info"

  if [[ ! -f "$file" ]]; then
    echo -e "   ${RED}✗${NC} File not found: $file"
    ((ERRORS++))
    continue
  fi

  # Check if jq is installed
  if ! command -v jq &> /dev/null; then
    echo -e "   ${YELLOW}⚠${NC}  jq not installed, skipping JSON validation"
    echo "      Install with: apt-get install -y jq"
    break
  fi

  # Extract title and UID
  actual_title=$(jq -r '.title // "MISSING"' "$file")
  actual_uid=$(jq -r '.uid // "MISSING"' "$file")

  if [[ "$actual_title" == "MISSING" || "$actual_title" == "" ]]; then
    echo -e "   ${RED}✗${NC} $file: Title is empty or missing"
    ((ERRORS++))
  elif [[ "$actual_title" != "$expected_title" ]]; then
    echo -e "   ${YELLOW}⚠${NC}  $file: Title mismatch"
    echo "      Expected: $expected_title"
    echo "      Got: $actual_title"
  else
    echo -e "   ${GREEN}✓${NC} $file: Title correct"
  fi

  if [[ "$actual_uid" == "MISSING" || "$actual_uid" == "" ]]; then
    echo -e "   ${RED}✗${NC} $file: UID is empty or missing"
    ((ERRORS++))
  elif [[ "$actual_uid" != "$expected_uid" ]]; then
    echo -e "   ${YELLOW}⚠${NC}  $file: UID mismatch"
    echo "      Expected: $expected_uid"
    echo "      Got: $actual_uid"
  else
    echo -e "   ${GREEN}✓${NC} $file: UID correct"
  fi
done

echo ""

# Check 2: Verify Grafana container is running
echo "🐳 Step 2: Checking Grafana container status..."
if docker-compose ps grafana | grep -q "Up"; then
  echo -e "   ${GREEN}✓${NC} Grafana container is running"
else
  echo -e "   ${RED}✗${NC} Grafana container is not running"
  echo "      Start with: docker-compose up -d grafana"
  ((ERRORS++))
fi

echo ""

# Check 3: Check Grafana logs for provisioning errors
echo "📜 Step 3: Checking Grafana provisioning logs..."
if docker-compose logs grafana 2>&1 | grep -q "Dashboard title cannot be empty"; then
  echo -e "   ${RED}✗${NC} Found 'empty title' errors in logs:"
  docker-compose logs grafana 2>&1 | grep "Dashboard title cannot be empty" | tail -5
  ((ERRORS++))
else
  echo -e "   ${GREEN}✓${NC} No 'empty title' errors in logs"
fi

# Check for successful provisioning
if docker-compose logs grafana 2>&1 | grep -q "provisioned dashboard"; then
  echo -e "   ${GREEN}✓${NC} Found successful provisioning messages:"
  docker-compose logs grafana 2>&1 | grep "provisioned dashboard" | tail -8 | sed 's/^/      /'
else
  echo -e "   ${YELLOW}⚠${NC}  No successful provisioning messages found"
  echo "      This might indicate dashboards haven't been loaded yet"
fi

echo ""

# Check 4: Verify dashboards are accessible (if Grafana is up)
echo "🌐 Step 4: Testing dashboard API access..."
if docker-compose ps grafana | grep -q "Up"; then
  # Try to access each dashboard by UID
  for dashboard_info in "${DASHBOARDS[@]}"; do
    IFS=':' read -r file expected_title expected_uid <<< "$dashboard_info"

    # Simple health check (no auth required)
    if curl -sf "http://localhost:3001/api/health" > /dev/null 2>&1; then
      echo -e "   ${GREEN}✓${NC} Grafana API is responsive"
      break
    else
      echo -e "   ${YELLOW}⚠${NC}  Grafana API not accessible yet (might still be starting)"
      break
    fi
  done
else
  echo -e "   ${YELLOW}⚠${NC}  Skipping API check (Grafana not running)"
fi

echo ""
echo "════════════════════════════════════════════════════════════"

if [[ $ERRORS -eq 0 ]]; then
  echo -e "${GREEN}✓ All checks passed!${NC}"
  exit 0
else
  echo -e "${RED}✗ Found $ERRORS error(s)${NC}"
  echo ""
  echo "To fix:"
  echo "  1. Ensure all JSON files have 'title' and 'uid' fields"
  echo "  2. Remove Grafana volume: docker volume rm poker-telegram-bot_grafana_data"
  echo "  3. Rebuild Grafana: docker-compose build --no-cache grafana"
  echo "  4. Restart stack: docker-compose up -d"
  exit 1
fi
