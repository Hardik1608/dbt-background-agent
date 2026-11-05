#!/usr/bin/env bash
# Run a command under the box-wide heavy-job lock, after a RAM check.
#   bash scripts/box/heavy.sh [--need-mb N] -- <command ...>
# Rules (PROGRESS.md): wait for RAM first (never hold the lock while idle); `mkdir` of the
# lock is atomic; owner.json inside; poll every 2 min while held by someone else; a lock
# older than 3 h whose PID is not running is stale; released by a trap on exit.
set -euo pipefail
LOCK="${HEAVY_LOCK:-$HOME/Hardik/Extras/.heavy_job.lock}"
NEED_MB=3072
if [[ "${1:-}" == "--need-mb" ]]; then NEED_MB="$2"; shift 2; fi
[[ "${1:-}" == "--" ]] && shift

avail_mb() { awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo; }

while true; do
  while (( $(avail_mb) < NEED_MB )); do
    echo "$(date '+%H:%M:%S') RAM available $(avail_mb) MB < ${NEED_MB} MB - waiting 60 s"
    sleep 60
  done
  if mkdir "$LOCK" 2>/dev/null; then
    break
  fi
  pid="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['pid'])" \
         "$LOCK/owner.json" 2>/dev/null || true)"
  started="$(python3 -c "import json,sys;print(int(json.load(open(sys.argv[1]))['started_epoch']))" \
             "$LOCK/owner.json" 2>/dev/null || stat -c %Y "$LOCK")"
  age=$(( $(date +%s) - started ))
  if (( age > 10800 )) && [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
    echo "breaking stale lock (pid $pid, ${age}s old)"
    rm -rf "$LOCK"
    continue
  fi
  echo "$(date '+%H:%M:%S') heavy-job lock held (pid ${pid:-?}) - retry in 120 s"
  sleep 120
done
trap 'rm -rf "$LOCK"' EXIT INT TERM
printf '{"project": "dbt-background-agent", "pid": %s, "host": "%s", "started": "%s", "started_epoch": %s, "label": "%s"}\n' \
  "$$" "$(hostname)" "$(date -Is)" "$(date +%s)" "${1:-job}" > "$LOCK/owner.json"
"$@"
