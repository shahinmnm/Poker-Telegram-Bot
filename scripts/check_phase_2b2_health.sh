#!/bin/bash

echo "=== Phase 2B-2 Health Check ==="

# Check retry metrics
RETRY_SUCCESS=$(grep -c "action_lock_retry_success" logs/bot.log 2>/dev/null || echo 0)
RETRY_FAILURES=$(grep -c "action_lock_retry_failures" logs/bot.log 2>/dev/null || echo 0)
RETRY_TIMEOUTS=$(grep -c "action_lock_retry_timeouts" logs/bot.log 2>/dev/null || echo 0)

# Normalize possible command substitution outputs
RETRY_SUCCESS=${RETRY_SUCCESS:-0}
RETRY_FAILURES=${RETRY_FAILURES:-0}
RETRY_TIMEOUTS=${RETRY_TIMEOUTS:-0}

TOTAL_RETRIES=$((RETRY_SUCCESS + RETRY_FAILURES + RETRY_TIMEOUTS))

# Output retry metrics
cat <<METRICS
Retry Success: $RETRY_SUCCESS
Retry Failures: $RETRY_FAILURES
Retry Timeouts: $RETRY_TIMEOUTS
METRICS

# Check queue estimation
QUEUE_ESTIMATES=$(grep -c "queue_position" logs/bot.log 2>/dev/null || echo 0)
QUEUE_FAILURES=$(grep -c "action_lock_queue_estimation_failed" logs/bot.log 2>/dev/null || echo 0)

QUEUE_ESTIMATES=${QUEUE_ESTIMATES:-0}
QUEUE_FAILURES=${QUEUE_FAILURES:-0}

cat <<QUEUE
Queue Estimates: $QUEUE_ESTIMATES
Queue Failures: $QUEUE_FAILURES
QUEUE

# Check translation usage
PERSIAN_FEEDBACK=$(grep -c "در صف" logs/bot.log 2>/dev/null || echo 0)
ENGLISH_FEEDBACK=$(grep -c "Queue position" logs/bot.log 2>/dev/null || echo 0)

PERSIAN_FEEDBACK=${PERSIAN_FEEDBACK:-0}
ENGLISH_FEEDBACK=${ENGLISH_FEEDBACK:-0}

cat <<TRANSLATIONS
Persian Feedback: $PERSIAN_FEEDBACK
English Feedback: $ENGLISH_FEEDBACK
TRANSLATIONS

if [ $TOTAL_RETRIES -gt 0 ]; then
    SUCCESS_RATE=$((RETRY_SUCCESS * 100 / TOTAL_RETRIES))
    echo "Overall Success Rate: ${SUCCESS_RATE}%"

    if [ $SUCCESS_RATE -ge 95 ]; then
        echo "✅ HEALTH: EXCELLENT"
    elif [ $SUCCESS_RATE -ge 80 ]; then
        echo "⚠️  HEALTH: GOOD"
    else
        echo "❌ HEALTH: NEEDS ATTENTION"
    fi
else
    echo "No retry attempts recorded."
fi
