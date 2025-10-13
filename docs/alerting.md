# Telegram Alerting Bridge Deployment Guide

This guide documents how to configure the Poker alerting stack so that every
notification is delivered straight to the administrator's personal Telegram
chat. The bridge remains self-hosted and continues to use Telegram as the only
outbound channel.

## 1. Quick Start Checklist

Run the helper script to see the streamlined setup steps:

```bash
python scripts/setup_alert_channels.py
```

Follow the prompts to discover your chat ID, populate `.env`, redeploy the
services, and send a verification alert.

## 2. Prerequisites

- **Admin's Personal Telegram Account**: You must have a personal Telegram
  account that can receive messages from the poker bot.
- **Bot Token**: The poker bot must have a valid Telegram API token.
- **Docker Compose**: Services will run as containerized components.

**No Telegram groups are required** for this simplified alerting model.

## 3. Architecture

The alerting system consists of three components:

1. **Prometheus**: Evaluates security rules and detects violations.
2. **Alertmanager**: Receives alerts from Prometheus and forwards to the bridge.
3. **Alert Bridge**: Sends formatted alerts to the admin's private Telegram chat.

### Alert Flow

```
┌─────────────┐   Alert   ┌──────────────┐   Webhook   ┌─────────────┐
│  Prometheus │──────────►│ Alertmanager │────────────►│ Alert Bridge│
│   (Rules)   │           │  (Routing)   │             │  (Telegram) │
└─────────────┘           └──────────────┘             └──────┬──────┘
                                                             │
                                                             ▼
                                                     ┌──────────────────┐
                                                     │ Admin Private DM │
                                                     └──────────────────┘
```

All alerts are delivered to a single destination: the admin’s personal Telegram
chat. Alertmanager batches similar alerts (within 2 minutes) to prevent
notification spam.

## 4. Configuration

### 4.1 Discover Your Chat ID

Send `/start` to your poker bot from your personal Telegram account, then run:

```bash
python scripts/get_chat_ids.py
```

Expected output:

```
Discovered chat identifiers:

  • [Private] (unknown title): 123456789

✅ Suggested admin chat ID: 123456789

Update your .env file with:
  ADMIN_CHAT_ID=123456789
```

### 4.2 Update Environment Variables

Add to `.env` (or your secret manager):

```dotenv
POKERBOT_TOKEN=<YOUR_BOT_TOKEN_FROM_BOTFATHER>
ADMIN_CHAT_ID=123456789
```

**Security Note**: Never commit `.env` to version control. Use `.env.example`
as a template and populate secrets at deployment time.

### 4.3 Deploy Services

```bash
docker-compose up -d alert-bridge alertmanager
```

Verify services are running:

```bash
docker-compose ps | grep -E "(alert-bridge|alertmanager)"
```

Expected output:

```
poker-alert-bridge    running    0.0.0.0:9099->9099/tcp
poker-alertmanager    running    0.0.0.0:9093->9093/tcp
```

## 5. Testing

### 5.1 Send Test Alert

```bash
curl -X POST http://localhost:9093/api/v1/alerts \
  -H 'Content-Type: application/json' \
  -d '[
{
  "labels": {
    "alertname": "AdminSetupTest",
    "severity": "info",
    "component": "alerting"
  },
  "annotations": {
    "summary": "Alert system configured successfully",
    "description": "This is a test message to verify admin alerting works."
  }
}
  ]'
```

### 5.2 Verify Delivery

Check your personal Telegram chat with the poker bot. You should receive:

```
🔵 AdminSetupTest
Severity: INFO
Status: FIRING
Component: alerting
Summary: Alert system configured successfully
Description: This is a test message to verify admin alerting works.
Runbook: (not provided)
Timestamp: 2025-10-13 12:34:56Z
```

### 5.3 Test Alert Resolution

Fire and resolve the same alert to verify cleanup:

```bash
# Fire alert
curl -X POST http://localhost:9093/api/v1/alerts -d '[
  {
    "labels": {"alertname":"ResolutionTest","severity":"warning"},
    "annotations": {"summary":"Testing resolution"}
  }
]'

# Wait 10 seconds, then resolve
sleep 10

curl -X POST http://localhost:9093/api/v1/alerts -d '[
  {
    "labels": {"alertname":"ResolutionTest","severity":"warning"},
    "annotations": {"summary":"Testing resolution"},
    "endsAt": "'"$(date -u +"%Y-%m-%dT%H:%M:%SZ")"'"
  }
]'
```

You should receive two messages:

1. 🟡 **ResolutionTest** (FIRING)
2. ✅ **ResolutionTest** (RESOLVED)

## 6. Docker Compose Updates

Ensure the alert bridge service references the new environment variable:

```yaml
services:
  alert-bridge:
    build:
      context: .
      dockerfile: Dockerfile.alert-bridge
    container_name: poker-alert-bridge
    environment:
      - POKERBOT_TOKEN=${POKERBOT_TOKEN}
      - ADMIN_CHAT_ID=${ADMIN_CHAT_ID}
      - TELEGRAM_API_BASE=${TELEGRAM_API_BASE:-https://api.telegram.org}
      - ALERT_BRIDGE_PORT=9099
    ports:
      - "9099:9099"
    networks:
      - poker-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9099/health"]
      interval: 30s
      timeout: 5s
      retries: 3
    restart: unless-stopped
```

## 7. Monitoring Commands

- `docker-compose logs alert-bridge` – JSON structured logs of webhook receipts
  and Telegram delivery attempts.
- `docker-compose logs alertmanager` – view Alertmanager routing decisions.
- `curl http://localhost:9099/metrics` – simple counters for processed and failed
  alert deliveries.
- `docker-compose exec prometheus promtool check rules /etc/prometheus/poker_security_alerts.yml`
  – on-demand rule validation.

## 8. Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No alerts arriving | Confirm `ADMIN_CHAT_ID` is set correctly and the bot can message your personal chat. Check the bridge logs for delivery failures. |
| Telegram errors about markdown | Examine the log payload for the failing alert. If annotations contain Markdown control characters, sanitise them or adjust the runbook content. |
| Alerts seem delayed | Review `group_wait`, `group_interval`, and `repeat_interval` in `config/alertmanager/alertmanager.yml`. Adjust to suit your tolerance for batching and retries. |
| Alertmanager retries endlessly | Confirm the bridge returns HTTP 200 (even when Telegram fails). The bridge logs `Telegram delivery failed` with the HTTP status code from Telegram. |

## 9. Post-Deployment Checklist

- [ ] `.env` updated with `POKERBOT_TOKEN` and `ADMIN_CHAT_ID`.
- [ ] `docker-compose ps` shows `alert-bridge` and `alertmanager` healthy.
- [ ] Test alerts observed in the admin's personal Telegram chat.
- [ ] Resolution notifications verified.

This completes the admin-direct alerting deployment.
