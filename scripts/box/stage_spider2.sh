#!/usr/bin/env bash
# Spider 2.0-DBT, all 68 tasks with the local model, then the official evaluation.
# Resumable: finished tasks have runs/spider2_local_agent/<id>__t0/bench_result.json.
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/box/env.sh
echo "== spider2 run $(date -Is)"
$PY -m dbt_agent bench spider2 run --config $MODEL_CONFIGS configs/spider2.yaml --heavy-lock \
  --run-name spider2_local_agent || exit 1
echo "== spider2 evaluate $(date -Is)"
$PY -m dbt_agent bench spider2 evaluate --run-name spider2_local_agent \
  --report reports/spider2_dbt
