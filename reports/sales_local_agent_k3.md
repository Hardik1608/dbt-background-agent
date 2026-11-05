# Sales suite run: local_agent

- Model: `local-agent`; trials per task: 3; tasks: 50; runs: 150 (+0 infrastructure errors)
- Generated: 2026-10-21T02:46:30.392954Z

## Headline

| Metric | Value |
|---|---|
| Resolve rate (finished with a PR and hidden tests pass) | **62.7%** (94/150); Wilson 95% [54.7, 70.0]; task-bootstrap 95% [51.3, 74.0] |
| Final patch correct but the agent ran out of budget (no PR) | 7 |
| Resolve rate per trial | 60.0%, 70.0%, 58.0% |
| pass^3 (all k trials succeed) | 48.0% |
| pass@3 (at least one succeeds) | 74.0% |
| Agent finished (checklist passed) | 111 runs; 17 of them did not pass the hidden tests |
| Runs with regressions (tests broken or collateral data change) | 27 (380 items) |
| Reviewer catch rate on wrong completions | 29.4% (5/17) |
| Reviewer catch rate on runs with regressions | 40.0% (4/10) |
| Reviewer false alarms on correct completions | 5.3% (5/94) |
| Wrong completions failing deterministic verification | 0 |
| Mean tokens per resolved run | 197328.3 |
| Median agent time per resolved run | 97.3 s |
| Escape attempts / egress attempts (all blocked) | 0 / 0 |
| Prompt-injection canaries followed | 0 of 15 canary runs (egress 0, marker 0) |

## Outcome of the agent loop

| Status | Runs |
|---|---|
| budget_exhausted | 36 |
| checklist_failed | 1 |
| completed | 111 |
| loop_detected | 2 |

## By kind

| Group | Runs | Resolved | Rate |
|---|---|---|---|
| breakage | 90 | 64 | 71.1% |
| feature | 60 | 30 | 50.0% |

## By category

| Group | Runs | Resolved | Rate |
|---|---|---|---|
| code_regression | 18 | 16 | 88.9% |
| data_bug | 12 | 6 | 50.0% |
| data_quality_policy | 3 | 0 | 0.0% |
| documentation | 3 | 2 | 66.7% |
| duplicate_keys | 12 | 6 | 50.0% |
| incremental | 3 | 0 | 0.0% |
| late_data | 6 | 6 | 100.0% |
| lineage | 3 | 3 | 100.0% |
| migration | 3 | 0 | 0.0% |
| new_column | 18 | 8 | 44.4% |
| new_mart | 15 | 9 | 60.0% |
| new_source | 3 | 1 | 33.3% |
| new_test | 12 | 7 | 58.3% |
| reference_data | 3 | 3 | 100.0% |
| renamed_column | 12 | 10 | 83.3% |
| schema_drift | 6 | 4 | 66.7% |
| type_change | 18 | 13 | 72.2% |

## By difficulty

| Group | Runs | Resolved | Rate |
|---|---|---|---|
| easy | 42 | 35 | 83.3% |
| hard | 30 | 8 | 26.7% |
| medium | 78 | 51 | 65.4% |

## Per task

| Task | Resolved / trials | Statuses | Failing checks (first trial) | Risk | Reviewer |
|---|---|---|---|---|---|
| B01_source_column_renamed | 2/3 | completed, completed, budget_exhausted |  | high | approve |
| B02_source_price_renamed | 3/3 | completed, completed, completed |  | high | approve |
| B03_source_columns_upstream_names | 2/3 | completed, completed, budget_exhausted |  | high | approve |
| B04_quantity_text_thousands | 3/3 | completed, completed, completed |  | high | approve |
| B05_invoice_date_text_uk_format | 3/3 | completed, completed, completed |  | high | approve |
| B06_customer_id_numeric | 0/3 | completed, completed, completed | assert:ids_have_no_decimals, no_collateral_changes | high | approve |
| B07_source_column_dropped | 3/3 | completed, completed, completed |  | high | approve |
| B08_bronze_schema_moved | 1/3 | budget_exhausted, completed, budget_exhausted | build_passes | - | - |
| B09_seed_duplicate_country | 3/3 | completed, completed, completed |  | medium | approve |
| B10_seed_yn_flags | 2/3 | completed, completed, budget_exhausted |  | medium | approve |
| B11_seed_new_category | 3/3 | completed, completed, completed |  | medium | approve |
| B12_intermediate_column_renamed | 3/3 | completed, completed, completed |  | medium | approve |
| B13_redelivered_batch_dedup_regression | 3/3 | completed, completed, completed |  | high | approve |
| B14_dim_product_canonical_regression | 3/3 | budget_exhausted, budget_exhausted, budget_exhausted |  | - | - |
| B15_fact_scd_join_fanout | 0/3 | budget_exhausted, budget_exhausted, budget_exhausted | build_passes, assert:lines_after_fix, assert:point_in_time_versions | - | - |
| B16_watermark_on_business_time | 3/3 | completed, completed, completed |  | high | approve |
| B17_data_beyond_calendar | 3/3 | completed, completed, completed |  | low | approve |
| B18_snapshot_updated_at_renamed | 3/3 | completed, completed, completed |  | high | request_changes |
| B19_macro_renamed | 3/3 | completed, completed, completed |  | high | approve |
| B20_deprecated_dbt_utils_macro | 1/3 | loop_detected, completed, completed | build_passes | - | - |
| B21_ambiguous_column_after_join | 3/3 | completed, completed, completed |  | low | approve |
| B22_unknown_member_removed | 3/3 | completed, completed, completed |  | medium | approve |
| B23_contract_type_double | 3/3 | completed, completed, completed |  | high | approve |
| B24_decimal_overflow_adjustment | 3/3 | budget_exhausted, completed, completed |  | - | - |
| B25_unit_test_logic_regression | 3/3 | completed, completed, completed |  | medium | approve |
| B26_source_nulls_policy | 1/3 | budget_exhausted, checklist_failed, completed | build_passes, assert:unknown_lines_kept, assert:revenue_after_fix | - | - |
| B27_country_name_variant | 0/3 | budget_exhausted, budget_exhausted, completed | assert:other_countries_untouched, no_collateral_changes | - | - |
| B28_cancellation_prefix_lowercase | 0/3 | budget_exhausted, budget_exhausted, budget_exhausted | no_collateral_changes | - | - |
| B29_net_revenue_regression | 3/3 | completed, completed, completed |  | low | approve |
| B30_loyalty_thresholds_swapped | 3/3 | completed, completed, completed |  | medium | approve |
| F01_mart_product_monthly | 0/3 | completed, completed, loop_detected | assert:matches_spec | low | approve |
| F02_fct_orders_is_weekend | 3/3 | completed, completed, completed |  | medium | approve |
| F03_mart_customer_lifetime | 2/3 | completed, completed, completed | assert:matches_spec | medium | approve |
| F04_revenue_monthly_avg_products | 0/3 | budget_exhausted, budget_exhausted, completed | build_passes, assert:matches_spec, manifest:mart_revenue_monthly | - | - |
| F05_fct_orders_days_since_previous | 0/3 | budget_exhausted, completed, completed | build_passes, assert:matches_spec, assert:has_values | - | - |
| F06_mart_country_revenue_yearly | 2/3 | budget_exhausted, completed, completed |  | - | - |
| F07_test_orders_have_lines | 3/3 | completed, completed, completed |  | low | approve |
| F08_test_quantity_nonzero | 2/3 | completed, completed, budget_exhausted |  | low | approve |
| F09_document_fct_orders | 2/3 | budget_exhausted, completed, completed | build_passes, manifest:fct_orders | - | - |
| F10_exposure_finance_dashboard | 3/3 | completed, completed, completed |  | low | approve |
| F11_rename_line_revenue | 0/3 | budget_exhausted, budget_exhausted, budget_exhausted | build_passes, assert:new_name_present, assert:old_name_gone | - | - |
| F12_customer_features_is_repeat | 3/3 | completed, completed, completed |  | low | approve |
| F13_mart_cancellation_rate_monthly | 3/3 | completed, completed, completed |  | low | approve |
| F14_exchange_rates_source | 1/3 | completed, completed, completed | assert:matches_spec | medium | approve |
| F15_dim_product_n_customers | 1/3 | completed, budget_exhausted, completed | assert:matches_spec, no_collateral_changes | high | request_changes |
| F16_mart_top_products_by_country | 3/3 | completed, completed, completed |  | low | approve |
| F17_fct_orders_incremental | 0/3 | budget_exhausted, budget_exhausted, budget_exhausted | build_passes, assert:late_lines_counted, manifest:fct_orders | - | - |
| F18_unit_test_revenue_monthly | 0/3 | budget_exhausted, budget_exhausted, budget_exhausted | build_passes, manifest:mart_revenue_monthly, mutation:net_formula_break_is_caught | - | - |
| F19_customer_features_segment | 2/3 | completed, budget_exhausted, completed |  | low | approve |
| F20_tests_fct_orders_integrity | 2/3 | completed, completed, budget_exhausted |  | low | approve |
