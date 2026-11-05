#!/usr/bin/env bash
# Suite validation on the GPU box (resumable: valid tasks are cached per task).
#   (setsid nohup systemd-run --user --scope -q -p MemoryMax=2G -p MemorySwapMax=0 \
#      scripts/box/run_validate.sh [--tasks ...] > bench_data/validate.log 2>&1 < /dev/null &)
set -uo pipefail
cd "$(dirname "$0")/../.."
export PYTHONUNBUFFERED=1
echo "== fixture build $(date -Is)"
.venv/bin/python -m dbt_agent fixture build
echo "== validate $(date -Is)"
.venv/bin/python -m dbt_agent suite validate --config configs/box.yaml --heavy-lock \
  --report reports/suite_validation "$@"
echo "== done $(date -Is) exit $?"
