#!/usr/bin/env bash
# Copy the project (code, fixtures, tasks; no venv, data, runs or reports) to the GPU box
# workspace. Reports only flow the other way (scripts/fetch_reports.sh), so a sync can never
# overwrite a newer report on the box. tar replaces each file with a new inode, so a script
# that bash is executing on the box keeps reading its old copy.
# Usage (Git Bash on the laptop): bash scripts/sync_to_box.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BOX="${BOX:-hardik@192.168.1.66}"
KEY="${KEY:-$HOME/.ssh/id_ed25519_gpu}"
REMOTE="${REMOTE:-Hardik/Extras/dbt-background-agent}"
cd "$HERE"
tar czf - \
  --exclude=.venv --exclude=runs --exclude=bench_data --exclude=./reports \
  --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache \
  --exclude='*.duckdb' --exclude='*.duckdb.wal' --exclude='fixtures/build' \
  --exclude='target' --exclude='logs' --exclude='dbt_packages' \
  . | ssh -i "$KEY" -o IdentitiesOnly=yes "$BOX" "mkdir -p ~/$REMOTE && cd ~/$REMOTE && tar xzf -"
echo "synced to $BOX:~/$REMOTE"
