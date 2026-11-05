#!/usr/bin/env bash
# What is this project running on the box right now? (bracket trick: pgrep never matches itself)
echo "== processes";  pgrep -af "[p]ython -m dbt_agent" || echo "none"
echo "== containers"; docker ps --filter label=dbt-agent.managed=true --format "{{.Names}} {{.Status}}"
echo "== lock";       cat ~/Hardik/Extras/.heavy_job.lock/owner* 2>/dev/null || echo "free"
echo "== ram/disk";   free -m | sed -n 2p; df -h ~ | tail -1
