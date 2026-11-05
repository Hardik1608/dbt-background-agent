#!/usr/bin/env bash
# Runs the stages listed in bench_data/queue.txt, one after another (one shell command per
# line; blank lines and lines starting with '#' are skipped). The file is re-read before
# every stage, so stages can be added or reordered while the queue runs. A stage is recorded
# in bench_data/queue.done only when it exits 0. Every stage is resumable on its own, so after
# a power cut, starting this script again continues where it stopped.
# Start: (setsid nohup systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=0 \
#          bash scripts/box/queue.sh >> bench_data/queue.log 2>&1 < /dev/null &)
set -uo pipefail
cd "$(dirname "$0")/../.."
QUEUE=bench_data/queue.txt
DONE=bench_data/queue.done
touch "$QUEUE" "$DONE"
while pgrep -f "[c]hain_sales.sh" > /dev/null; do sleep 120; done  # one batch at a time
failed=()
while true; do
  next=""
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -z "$line" ] && continue
    [ "${line:0:1}" = "#" ] && continue
    grep -qxF -- "$line" "$DONE" && continue
    skip=0
    for f in "${failed[@]+"${failed[@]}"}"; do [ "$f" = "$line" ] && skip=1; done
    [ "$skip" = 1 ] && continue
    next="$line"
    break
  done < "$QUEUE"
  [ -z "$next" ] && break
  echo "== stage $(date -Is): $next"
  if bash -c "$next"; then
    echo "$next" >> "$DONE"
    echo "== stage done $(date -Is)"
  else
    code=$?
    failed+=("$next")
    echo "== stage FAILED (exit $code) $(date -Is): $next"
  fi
done
echo "== queue empty $(date -Is) (failed this run: ${#failed[@]})"
