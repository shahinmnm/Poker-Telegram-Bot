# Grafana Dashboard Provisioning Fix

## Issue
Grafana dashboards were not loading due to:
1. Conflicting volume mounts in `docker-compose.yml`
2. Incorrect JSON structure (wrapped in `.dashboard` objects)
3. Missing required `uid` fields

## Solution
1. Consolidated to single volume mount: `./config/grafana/provisioning:/etc/grafana/provisioning:ro`
2. Moved dashboards to `config/grafana/provisioning/dashboards/`
3. Created `dashboards.yaml` provisioning config
4. Unwrapped dashboard JSONs and added UIDs

## Deployment
After pulling this PR:
```bash
# Remove cached Grafana volume
docker volume rm poker-telegram-bot_grafana_data

# Restart services
docker-compose down
docker-compose up -d

# Verify dashboards loaded
docker-compose logs grafana | grep "provisioned dashboard"
```
