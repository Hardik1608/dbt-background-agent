#!/usr/bin/env bash
# ADE-bench: every ready DuckDB + dbt task through ADE's own harness with our agent, one at
# a time (RAM admission + heavy lock per task; the first one builds ADE's trial image).
# Resumable: graded tasks have bench_data/adebench/runs/<run>/<task>.result.json.
# Usage: stage_ade.sh [run name] [task ...]   (no tasks: all ready tasks, then the trial
# images ADE built for us are removed)
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/box/env.sh
RUN="${1:-ade_local_agent}"
shift || true
echo "== adebench $RUN $(date -Is)"
if [ "$#" -gt 0 ]; then
  $PY -m dbt_agent bench adebench run --config $MODEL_CONFIGS configs/adebench.yaml \
    --heavy-lock --run-name "$RUN" --tasks "$@" --report "runs/${RUN}_report"
else
  $PY -m dbt_agent bench adebench run --config $MODEL_CONFIGS configs/adebench.yaml \
    --heavy-lock --run-name "$RUN" --report reports/adebench || exit 1
  $PY -m dbt_agent bench adebench cleanup
fi
