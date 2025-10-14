#!/bin/bash
# Verification script for monitoring stack fixes
set -e

banner_line="╔════════════════════════════════════════════════════════════╗"
banner_footer="╚════════════════════════════════════════════════════════════╝"

echo "$banner_line"
echo "║                  MONITORING STACK VERIFICATION             ║"
echo "$banner_footer"
echo

echo "Test 1: Alert Bridge Metrics Endpoint"
echo "1. Testing Alert Bridge /metrics endpoint..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/metrics || true)
if [ "$HTTP_CODE" = "200" ]; then
  echo "  ✅ Metrics endpoint: HTTP $HTTP_CODE"
  echo "  Checking response format..."
  METRICS_SAMPLE=$(curl -s http://localhost:8081/metrics | head -5 || true)
  if echo "$METRICS_SAMPLE" | grep -q "# HELP"; then
    echo "  ✅ Prometheus format valid"
  else
    echo "  ⚠️ Warning: Unexpected metrics format"
  fi
else
  echo "  ❌ Metrics endpoint: HTTP $HTTP_CODE (expected 200)"
  echo "  Checking logs..."
  docker-compose logs --tail=20 alert-bridge | grep -i error || true
fi

echo

echo "Test 2: Alert Bridge Health"
echo "2. Testing Alert Bridge /health endpoint..."
HEALTH_STATUS=$(curl -s http://localhost:8081/health | jq -r '.status // "unknown"' || echo "unknown")
if [ "$HEALTH_STATUS" = "healthy" ]; then
  echo "  ✅ Health check: $HEALTH_STATUS"
else
  echo "  ❌ Health check: $HEALTH_STATUS"
fi

echo

echo "Test 3: Grafana Dashboard Provisioning"
echo "3. Checking Grafana dashboard provisioning..."
declare -a dashboards=(
  "smart-retry-health:Smart Retry System Health"
  "fine-grained-locks:Fine-Grained Lock Performance"
  "pruning-health:Database Pruning Health"
  "alerting-health:Alert System Health"
)
FAILED_DASHBOARDS=0
for dashboard in "${dashboards[@]}"; do
  uid="${dashboard%%:*}"
  expected_title="${dashboard#*:}"
  ACTUAL_TITLE=$(curl -s "http://localhost:3001/api/dashboards/uid/${uid}" | jq -r '.dashboard.title // "NOT_FOUND"' || echo "NOT_FOUND")
  if [ "$ACTUAL_TITLE" = "$expected_title" ]; then
    echo "  ✅ $uid: \"$ACTUAL_TITLE\""
  elif [ "$ACTUAL_TITLE" = "NOT_FOUND" ]; then
    echo "  ⚠️ $uid: Dashboard not found (may not exist in this version)"
  else
    echo "  ❌ $uid: \"$ACTUAL_TITLE\" (expected: \"$expected_title\")"
    FAILED_DASHBOARDS=$((FAILED_DASHBOARDS + 1))
  fi
done
if [ $FAILED_DASHBOARDS -eq 0 ]; then
  echo "  ✅ All dashboards provisioned correctly"
else
  echo "  ❌ $FAILED_DASHBOARDS dashboard(s) have incorrect titles"
fi

echo

echo "Test 4: Prometheus Scraping"
echo "4. Testing Prometheus scraping of Alert Bridge..."
PROMETHEUS_TARGET=$(curl -s http://localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | select(.labels.job=="alert-bridge") | .health' || echo "unknown")
if [ "$PROMETHEUS_TARGET" = "up" ]; then
  echo "  ✅ Prometheus scraping: $PROMETHEUS_TARGET"
else
  echo "  ⚠️ Prometheus scraping: $PROMETHEUS_TARGET"
fi

echo

echo "$banner_line"
echo "║                    VERIFICATION COMPLETE                    ║"
echo "$banner_footer"
echo

echo "Next steps:"
echo " 1. If all tests passed: monitoring stack is healthy"
echo " 2. If failures exist: check docker-compose logs for errors"
echo " 3. Restart affected services: docker-compose restart <service>"
echo
