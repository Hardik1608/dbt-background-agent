# [agent] Repeat-customer flag in the ML features

> Opened by dbt-background-agent for `F12_customer_features_is_repeat` (ticket). **A human must review and merge.** Risk class: **LOW**.

## Summary

Added `is_repeat_customer` (integer, 1 or 0) to gold.customer_features as requested. The column is computed as 1 when frequency_orders >= 2 at the cutoff, else 0. Changes made:

1. **models/marts/ml/customer_features.sql**: Added `case when frequency_orders >= 2 then 1 else 0 end as is_repeat_customer` in the `scored` CTE, and included it in the final SELECT.

2. **models/marts/ml/_ml__models.yml**: Added contract for `is_repeat_customer` with data_type: integer and documentation ("1 when the customer has placed 2 or more orders by the cutoff, else 0. Indicates repeat-purchase behavior at the point in time."). Also updated the unit test expect row to include `is_repeat_customer: 1` (since the test case has frequency_orders: 2).

All tests pass: `dbt build --select customer_features` passes with PASS 8 | SUCCESS 1, and `dbt test --select customer_features` passes with PASS 8. The contract enforces the integer type and the unit test validates the expected value.

## Changes

| File | Added | Removed |
|---|---:|---:|
| `models/marts/ml/_ml__models.yml` | 6 | 1 |
| `models/marts/ml/customer_features.sql` | 3 | 1 |

## Test evidence

Re-run from the starting commit in a fresh sandbox with only this patch applied.

| Check | Result | Details |
|---|---|---|
| Patch applies to the starting commit | PASS | 2 file(s) |
| Project parses | PASS |  |
| No test or contract removed or weakened | PASS |  |
| Slim CI: `dbt build -s state:modified+ --full-refresh` | PASS | pass 8, success 1 |
| Reconciliation tests | PASS | assert_fact_reconciles_to_silver assert_monthly_mart_reconciles_to_fact |
| Incremental = full refresh (`fct_order_lines`) | skipped | model not affected by the change |
| sqlfluff (new violations only) | PASS | no new violations |

Verification took 15 s.

## Lineage impact

- Modified (dbt `state:modified`, incl. macro changes): `model.sales_analytics.customer_features`; plus 8 test(s) on them
- Downstream, rebuilt and tested: 0 model(s), 0 test(s)
- Exposures: none

## Review

Sceptical LLM review (`local-agent`): **approve**, risk **low**.

> The change correctly implements the ticket by adding `is_repeat_customer` as a derived column based on `frequency_orders >= 2`. The logic is sound, the contract and documentation are properly added, and all CI checks including reconciliation and unit tests pass without introducing data quality risks.

## Risk

**LOW**

- low: reviewer risk low

## Cost

| | Turns | Tool calls | Prompt tokens | Completion tokens | Wall time |
|---|---:|---:|---:|---:|---:|
| Agent (`local-agent`) | 27 | 27 | 242,975 | 3,816 | 2.4 min |
| Reviewer | 1 | 0 | 1,820 | 79 | 3 s |

Local model: no API charge (energy use not measured).

## Reproduce

```bash
git apply patch.diff
dbt build --select state:modified+ --state <production manifest> --full-refresh
```
