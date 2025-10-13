# Telegram Alerting Bridge Deployment Guide

This guide documents the operational steps for provisioning the Phase 1.5 alerting
stack. The bridge is entirely self-hosted and uses only Telegram as the outbound
notification channel.

## 1. Provision Telegram Channels

Run the helper script to print the high-level checklist:

```bash
python scripts/setup_alert_channels.py
```

Follow the steps:

1. Create three **Telegram supergroups**:
   - `poker-alerts-critical` for critical security incidents.
   - `poker-alerts-ops` for operational warnings and high alerts.
   - `poker-alerts-digest` for the six-hour digest stream.
2. Add `@PokerHardeningBot` to each group and promote it to administrator.
3. Disable the bot's privacy mode in each group so it can read `/start` messages.
4. Post a message containing `/start` in each chat to generate an update.

## 2. Discover Chat Identifiers

The discovery script calls the Telegram `getUpdates` API and extracts the chat IDs
from the stored updates.

```bash
python scripts/get_chat_ids.py --mark-read
```

The command prints the numeric `chat_id` for every group where `/start` was sent
and acknowledges the updates when `--mark-read` is specified. Copy the values into
`.env`:

```dotenv
CRITICAL_CHAT_ID=<critical chat id>
OPERATIONAL_CHAT_ID=<operational chat id>
DIGEST_CHAT_ID=<digest chat id>
```

## 3. Configure Environment Variables

Ensure the following entries exist in `.env` or your secret manager:

- `POKERBOT_TOKEN=<YOUR_BOT_TOKEN_FROM_BOTFATHER>`
- `CRITICAL_CHAT_ID=<critical group id>`
- `OPERATIONAL_CHAT_ID=<ops group id>`
- `DIGEST_CHAT_ID=<digest group id>`

Additional Redis defaults are pre-populated by `make .env` when required.

## 4. Deploy the Stack

```bash
docker-compose --env-file .env up -d alert-bridge alertmanager prometheus grafana bot
```

The compose file adds:

- `alert-bridge` (Python `aiohttp` service listening on `9099`).
- `alertmanager` (Prometheus Alertmanager on `9093`).

Both services include health checks. The bridge exposes `/health` and `/metrics`.

## 5. Validate the Pipeline

1. **Health check** – `curl http://localhost:9099/health` should return `{"status":"ok"}`.
2. **Prometheus rules** – visit `http://localhost:9090/rules` and confirm the
   `poker-security` group loads without errors.
3. **Alertmanager target** – open `http://localhost:9093/#/status` to verify the
   webhook points at `http://alert-bridge:9099/webhook/alertmanager`.
4. **Test alert** – simulate a forgery spike from the bot container:

   ```bash
   curl -X POST http://localhost:8000/metrics/test/forgery
   ```

   Within one minute you should receive a 🔴 critical alert in the critical channel.
5. **Digest batching** – trigger multiple info-level events (for example, by
   posting `/start` to several chats) and confirm the digest channel aggregates up to
   20 alerts per message with a 🔵 header.
6. **Resolution flow** – after metrics return to normal, Alertmanager sends a
   resolved notification that renders with a ✅ emoji.

## 6. Monitoring Commands

- `docker-compose logs alert-bridge` – JSON structured logs of webhook receipts and
  Telegram delivery attempts.
- `docker-compose logs alertmanager` – view Alertmanager routing decisions.
- `curl http://localhost:9099/metrics` – simple counters for processed and failed
  alert deliveries.
- `docker-compose exec prometheus promtool check rules /etc/prometheus/poker_security_alerts.yml`
  – on-demand rule validation.

## 7. Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No alerts arriving | Ensure chat IDs are correct and the bot has permission to post. Check the bridge logs for `Skipping alert delivery` warnings. |
| Telegram errors about markdown | Examine the log payload for the failing alert. If annotations contain Markdown control characters, sanitise them or adjust the runbook content. |
| Digest channel too noisy | Tune `repeat_interval` and `group_interval` in `config/alertmanager/alertmanager.yml` and redeploy Alertmanager. |
| Alertmanager retries endlessly | Confirm the bridge returns HTTP 200 (even when Telegram fails). The bridge logs `Telegram delivery failed` with the HTTP status code from Telegram. |

## 8. Post-Deployment Checklist

- [ ] All three groups created and populated with operators.
- [ ] `.env` updated with chat IDs and committed to the secret store.
- [ ] `docker-compose ps` shows `alert-bridge` and `alertmanager` healthy.
- [ ] Test alerts observed in the correct Telegram channels.
- [ ] Digest batching verified.

This completes Phase 1.5 of the security hardening project.
