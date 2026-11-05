# [agent] Build fails after the price column was renamed upstream

> Opened by dbt-background-agent for `B02_source_price_renamed` (ci_failure). **A human must review and merge.** Risk class: **HIGH**.

## Summary

The CI build failed because the upstream data source renamed the column `price` to `unit_price`, but dbt still referenced `price` in both the YAML source definition and the staging SQL model. I updated: (1) models/staging/_bronze__sources.yml - changed column name from `price` to `unit_price` in the not_null test; (2) models/staging/stg_bronze__online_retail_lines.sql - changed the cast expression from `price` to `unit_price`. The build now passes with 84 PASS and 0 ERROR, and all tests pass.

## Changes

| File | Added | Removed |
|---|---:|---:|
| `models/staging/_bronze__sources.yml` | 1 | 1 |
| `models/staging/stg_bronze__online_retail_lines.sql` | 1 | 1 |

## Test evidence

Re-run from the starting commit in a fresh sandbox with only this patch applied.

| Check | Result | Details |
|---|---|---|
| Patch applies to the starting commit | PASS | 2 file(s) |
| Project parses | PASS |  |
| No test or contract removed or weakened | PASS |  |
| Slim CI: `dbt build -s state:modified+ --full-refresh` | PASS | pass 67, success 15 |
| Reconciliation tests | PASS | assert_fact_reconciles_to_silver assert_monthly_mart_reconciles_to_fact |
| Incremental = full refresh (`fct_order_lines`) | PASS | rows 67943 vs 67943, checksum identical (replayed the last 2 load batches; 4547 rows) |
| sqlfluff (new violations only) | PASS | no new violations |

Verification took 18 s.

## Lineage impact

- Modified (dbt `state:modified`, incl. macro changes): `model.sales_analytics.stg_bronze__online_retail_lines`; plus 9 test(s) on them
- Downstream, rebuilt and tested: 13 model(s), 58 test(s): `customer_churn_labels`, `customer_features`, `dim_country`, `dim_customer`, `dim_product`, `fct_order_lines`, `fct_orders`, `int_customer_invoices`, `int_customer_state`, `int_feature_cutoffs`, `int_order_lines__deduplicated`, `mart_cohort_retention`, `mart_revenue_monthly`
- Exposures: none

## Review

Sceptical LLM review (`local-agent`): **approve**, risk **low**.

> The diff correctly addresses the root cause by renaming the column reference from 'price' to 'unit_price' in both the source YAML definition and the SQL model, matching the upstream schema change. CI evidence confirms no data loss, row count stability, or new linting violations, and the lineage impact is limited to expected downstream models without breaking contracts.

## Risk

**HIGH**

- high: 13 downstream models
- low: reviewer risk low

## Cost

| | Turns | Tool calls | Prompt tokens | Completion tokens | Wall time |
|---|---:|---:|---:|---:|---:|
| Agent (`local-agent`) | 14 | 13 | 64,044 | 1,286 | 78 s |
| Reviewer | 1 | 0 | 1,685 | 93 | 4 s |

Local model: no API charge (energy use not measured).

## Reproduce

```bash
git apply patch.diff
dbt build --select state:modified+ --state <production manifest> --full-refresh
```
