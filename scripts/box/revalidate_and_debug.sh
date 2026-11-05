#!/usr/bin/env bash
# Re-validate tasks, then dump a full reference grade for any that are still invalid.
set -uo pipefail
cd "$(dirname "$0")/../.."
export PYTHONUNBUFFERED=1
.venv/bin/python -m dbt_agent suite validate --config configs/box.yaml --heavy-lock \
  --report reports/suite_validation_partial --tasks "$@"
for t in "$@"; do
  f=$(ls bench_data/sales/validation/${t}_*.json 2>/dev/null | head -1)
  if [ -n "$f" ] && ! grep -q '"valid": true' "$f"; then
    echo "== debug $t"
    bash scripts/box/heavy.sh --need-mb 2560 -- .venv/bin/python -m dbt_agent suite debug \
      --config configs/box.yaml --tasks "$t"
  fi
done
echo "== revalidate done $(date -Is)"
