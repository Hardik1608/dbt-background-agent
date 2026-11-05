#!/usr/bin/env bash
# Rebuild the sales reports from the per-task results with the current metrics code.
#   stage_reports.sh        k=1 report (trial 0 only) + oracle report
#   stage_reports.sh 3      also the pass^3 report (trials 0-2)
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/box/env.sh
$PY -m dbt_agent bench report --run-name oracle --report reports/sales_oracle
$PY -m dbt_agent bench report --run-name local_agent --trials 1 \
  --report reports/sales_local_agent_k1
if [ "${1:-1}" -ge 3 ]; then
  $PY -m dbt_agent bench report --run-name local_agent --trials 3 --k 3 \
    --report reports/sales_local_agent_k3
fi
