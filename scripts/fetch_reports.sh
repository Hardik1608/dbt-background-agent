#!/usr/bin/env bash
# Copy the benchmark reports (JSON + Markdown) from the GPU box into reports/.
# Usage: bash scripts/fetch_reports.sh [user@host]
set -euo pipefail
HOST="${1:-hardik@192.168.1.66}"
KEY="${BOX_SSH_KEY:-$HOME/.ssh/id_ed25519_gpu}"
REMOTE="Hardik/Extras/dbt-background-agent/reports"
cd "$(dirname "$0")/.."
mkdir -p reports
scp -q -i "$KEY" -o IdentitiesOnly=yes "$HOST:$REMOTE/*.json" "$HOST:$REMOTE/*.md" reports/
ls -la reports
