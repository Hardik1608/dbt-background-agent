# Sales suite validation

Every task must fail before the fix (empty patch) and pass with the reference fix.

- Tasks: 50; valid: **50**
- Fail before the fix: 50/50
- Pass with the reference fix: 50/50
- Reference re-graded with the collateral check (determinism): 0/0

| Task | Kind | Category | Valid | Checks failing before the fix | Seconds |
|---|---|---|---|---|---|
| B01_source_column_renamed | breakage | renamed_column | yes | build_passes | 8.4 |
| B02_source_price_renamed | breakage | renamed_column | yes | build_passes | 8.0 |
| B03_source_columns_upstream_names | breakage | renamed_column | yes | build_passes | 8.2 |
| B04_quantity_text_thousands | breakage | type_change | yes | build_passes | 7.8 |
| B05_invoice_date_text_uk_format | breakage | type_change | yes | build_passes | 8.1 |
| B06_customer_id_numeric | breakage | type_change | yes | build_passes | 7.8 |
| B07_source_column_dropped | breakage | schema_drift | yes | build_passes | 7.6 |
| B08_bronze_schema_moved | breakage | schema_drift | yes | build_passes | 7.6 |
| B09_seed_duplicate_country | breakage | duplicate_keys | yes | build_passes | 8.8 |
| B10_seed_yn_flags | breakage | type_change | yes | build_passes, assert:file_kept_as_delivered | 13.5 |
| B11_seed_new_category | breakage | reference_data | yes | build_passes | 7.8 |
| B12_intermediate_column_renamed | breakage | renamed_column | yes | build_passes | 15.2 |
| B13_redelivered_batch_dedup_regression | breakage | duplicate_keys | yes | build_passes | 8.0 |
| B14_dim_product_canonical_regression | breakage | duplicate_keys | yes | build_passes, assert:lines_after_fix | 14.6 |
| B15_fact_scd_join_fanout | breakage | duplicate_keys | yes | build_passes, assert:lines_after_fix, assert:point_in_time_versions | 15.3 |
| B16_watermark_on_business_time | breakage | late_data | yes | build_passes, assert:late_lines_loaded, assert:lines_after_fix | 16.2 |
| B17_data_beyond_calendar | breakage | late_data | yes | build_passes, assert:every_fact_day_in_calendar | 9.1 |
| B18_snapshot_updated_at_renamed | breakage | code_regression | yes | build_passes | 8.2 |
| B19_macro_renamed | breakage | code_regression | yes | build_passes | 8.5 |
| B20_deprecated_dbt_utils_macro | breakage | code_regression | yes | build_passes | 8.1 |
| B21_ambiguous_column_after_join | breakage | code_regression | yes | build_passes, assert:tier_column_kept_and_correct | 16.8 |
| B22_unknown_member_removed | breakage | code_regression | yes | build_passes, assert:unknown_member_present | 16.1 |
| B23_contract_type_double | breakage | type_change | yes | build_passes | 15.9 |
| B24_decimal_overflow_adjustment | breakage | type_change | yes | build_passes, assert:adjustment_loaded, assert:revenue_after_fix | 13.4 |
| B25_unit_test_logic_regression | breakage | code_regression | yes | build_passes | 15.5 |
| B26_source_nulls_policy | breakage | data_quality_policy | yes | build_passes, assert:unknown_lines_kept, assert:revenue_after_fix, test_present:not_null(stock_code) | 14.6 |
| B27_country_name_variant | breakage | data_bug | yes | assert:no_variant_country, assert:irish_revenue_after_fix, assert:other_countries_untouched, no_collateral_changes | 15.8 |
| B28_cancellation_prefix_lowercase | breakage | data_bug | yes | assert:cancellations_after_fix, assert:write_offs_unchanged, no_collateral_changes | 15.4 |
| B29_net_revenue_regression | breakage | data_bug | yes | assert:net_is_gross_plus_cancelled, assert:net_total_restored, no_collateral_changes | 17.1 |
| B30_loyalty_thresholds_swapped | breakage | data_bug | yes | assert:tiers_restored, no_collateral_changes | 17.9 |
| F01_mart_product_monthly | feature | new_mart | yes | assert:matches_spec, manifest:mart_product_monthly | 9.3 |
| F02_fct_orders_is_weekend | feature | new_column | yes | assert:flag_matches_calendar, assert:has_weekend_orders, manifest:fct_orders, no_collateral_changes | 17.6 |
| F03_mart_customer_lifetime | feature | new_mart | yes | assert:matches_spec, manifest:mart_customer_lifetime | 17.0 |
| F04_revenue_monthly_avg_products | feature | new_column | yes | assert:matches_spec, manifest:mart_revenue_monthly, no_collateral_changes | 17.4 |
| F05_fct_orders_days_since_previous | feature | new_column | yes | assert:matches_spec, assert:has_values, manifest:fct_orders, no_collateral_changes | 17.6 |
| F06_mart_country_revenue_yearly | feature | new_mart | yes | assert:matches_spec, manifest:mart_country_revenue_yearly | 17.1 |
| F07_test_orders_have_lines | feature | new_test | yes | mutation:orphan_order_is_caught | 24.0 |
| F08_test_quantity_nonzero | feature | new_test | yes | mutation:zero_quantity_is_caught | 24.4 |
| F09_document_fct_orders | feature | documentation | yes | manifest:fct_orders | 16.6 |
| F10_exposure_finance_dashboard | feature | lineage | yes | exposure:finance_monthly_revenue_dashboard | 16.7 |
| F11_rename_line_revenue | feature | migration | yes | assert:new_name_present, assert:old_name_gone, assert:values_kept | 13.7 |
| F12_customer_features_is_repeat | feature | new_column | yes | assert:matches_rule, manifest:customer_features, no_collateral_changes | 17.3 |
| F13_mart_cancellation_rate_monthly | feature | new_mart | yes | assert:matches_spec, manifest:mart_cancellation_rate_monthly | 16.9 |
| F14_exchange_rates_source | feature | new_source | yes | assert:matches_spec, manifest:stg_bronze__exchange_rates, manifest:mart_revenue_monthly_eur, test_present:not_null(gbp_rate) | 17.7 |
| F15_dim_product_n_customers | feature | new_column | yes | assert:matches_spec, manifest:dim_product, no_collateral_changes | 17.2 |
| F16_mart_top_products_by_country | feature | new_mart | yes | assert:matches_spec, manifest:mart_top_products_by_country | 16.9 |
| F17_fct_orders_incremental | feature | incremental | yes | manifest:fct_orders | 32.6 |
| F18_unit_test_revenue_monthly | feature | new_test | yes | manifest:mart_revenue_monthly, mutation:net_formula_break_is_caught, mutation:non_merchandise_break_is_caught | 26.0 |
| F19_customer_features_segment | feature | new_column | yes | assert:matches_rule, assert:all_segments_present, manifest:customer_features, no_collateral_changes (+1) | 17.4 |
| F20_tests_fct_orders_integrity | feature | new_test | yes | mutation:orphan_country_is_caught, mutation:empty_order_is_caught | 31.6 |
