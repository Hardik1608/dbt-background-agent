#!/usr/bin/env bash
# Free disk in finished run directories (box budget: 6 GB). Keeps what the reports and a
# re-run need: events.jsonl, outcome/result JSON, patch.diff, pr/, grades, Spider2's
# _submission. Removes build output, installed packages and databases from work copies.
# Only finished runs (bench_result.json present) are touched; find never follows symlinks.
set -euo pipefail
cd "$(dirname "$0")/../../runs"
before=$(du -sm . | cut -f1)
for result in */*/bench_result.json; do
  run="$(dirname "$result")"
  for sub in workdir/target workdir/logs workdir/dbt_packages verify_work grade_work; do
    [ -d "$run/$sub" ] && [ ! -L "$run/$sub" ] && rm -rf -- "${run:?}/$sub"
  done
  case "$run" in
    spider2_local_agent/*|spider2_smoke/*) [ -d "$run/workdir" ] && rm -rf -- "${run:?}/workdir" ;;
  esac
done
# superseded partial runs are not reported: drop their databases and build output
if [ -d _superseded ]; then
  find _superseded -type f \( -name '*.duckdb' -o -name '*.duckdb.wal' \) -delete
  find _superseded -type d \( -name target -o -name dbt_packages -o -name logs \) -prune \
    -exec rm -rf -- {} +
fi
after=$(du -sm . | cut -f1)
echo "runs/: ${before} MB -> ${after} MB"
