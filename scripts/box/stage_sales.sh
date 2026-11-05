#!/usr/bin/env bash
# The local model over the sales suite, optionally on a subset (e.g. extra trials for pass^k):
#   stage_sales.sh <trials> <report stem> [task ids ...]   (no ids: all 50 tasks)
# Resumable (per-task bench_result.json in runs/local_agent; trial 0 is shared with the k=1
# run, so k=3 only adds trials 1 and 2); RAM admission, then the heavy lock per task (paced).
# Never edit a stage script while the queue runs it: bash reads scripts as it goes.
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/box/env.sh
TRIALS="${1:-1}"
REPORT="${2:-reports/sales_local_agent_k${TRIALS}}"
shift 2 || shift $#
echo "== sales model trials=${TRIALS} tasks=${*:-all} $(date -Is)"
TASK_ARGS=()
if [ "$#" -gt 0 ]; then TASK_ARGS=(--tasks "$@"); fi
$PY -m dbt_agent bench sales --config $MODEL_CONFIGS configs/sales_suite.yaml \
  --heavy-lock --run-name local_agent --trials "$TRIALS" --report "$REPORT" "${TASK_ARGS[@]}"
