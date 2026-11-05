#!/usr/bin/env bash
# The local model over the sales suite: stage_model.sh <trials> <report stem>
# Resumable (per-task bench_result.json in runs/local_agent); RAM admission, then the heavy
# lock per task (paced). Waits for any other sales batch of ours to finish first.
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/box/env.sh
TRIALS="${1:-1}"
REPORT="${2:-reports/sales_local_agent_k${TRIALS}}"
while pgrep -f "[o]racle_rerun.sh" > /dev/null || pgrep -f "[c]hain_sales.sh" > /dev/null; do
  sleep 60
done
echo "== model trials=${TRIALS} $(date -Is)"
$PY -m dbt_agent bench sales --config $MODEL_CONFIGS configs/sales_suite.yaml \
  --heavy-lock --run-name local_agent --trials "$TRIALS" --report "$REPORT"
