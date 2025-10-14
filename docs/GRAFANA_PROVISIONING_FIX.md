# Grafana Dashboard Provisioning Fix

## Issue
Grafana dashboards were not loading due to:
1. Conflicting volume mounts in `docker-compose.yml`
2. Incorrect JSON structure (wrapped in `.dashboard` objects)
3. Missing required `uid` fields
4. Duplicate provisioning providers re-registering the same dashboards
5. Prometheus compactions failing when the Docker host runs out of disk space

## Solution
1. Consolidated to single volume mount: `./config/grafana/provisioning:/etc/grafana/provisioning:ro`
2. Moved dashboards to `config/grafana/provisioning/dashboards/`
3. Created `dashboards.yaml` provisioning config
4. Renamed the provider to `Poker Bot Dashboards v2` to avoid collisions with stale configs
5. Unwrapped dashboard JSONs and added UIDs
6. Documented the disk clean-up procedure for the metrics stack

## Deployment
After pulling this PR:
```bash
# Ensure no duplicate provisioning files linger
find config/grafana/provisioning -type f \( -name "*.yml" -o -name "*.yaml" \)

# Remove cached Grafana volume so the renamed provider is applied
docker-compose down
docker volume rm poker-telegram-bot_grafana_data
docker-compose up -d

# Optional: reclaim disk space if Prometheus reports compaction errors
df -h /
docker system prune -a --volumes -f

# Verify dashboards loaded
docker-compose logs grafana | grep "provisioned dashboard"
```
