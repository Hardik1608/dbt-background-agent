-- Silver: monthly "as of" dates for point-in-time features and churn labels.
--
--   * every cutoff gets features (computed only from data strictly before the cutoff)
--   * only cutoffs whose full label window [cutoff, cutoff + horizon) has been observed
--     get labels (has_complete_label_window) - otherwise recent customers would look
--     "churned" simply because the future has not happened yet.

{{ config(materialized='view') }}

with bounds as (

    select max(invoice_ts) as data_end_ts
    from {{ ref('int_order_lines__deduplicated') }}

),

months as (

    {{ dbt_utils.date_spine(
        datepart="month",
        start_date="cast('" ~ var('feature_cutoff_start') ~ "' as date)",
        end_date="cast('2014-01-01' as date)"
    ) }}

),

cutoffs as (

    select
        cast(months.date_month as date)     as cutoff_date,
        cast(
            {{ dbt.dateadd('day', var('churn_horizon_days'), 'months.date_month') }}
            as date
        )                                   as label_window_end,
        bounds.data_end_ts
    from months
    cross join bounds

)

select
    cutoff_date,
    label_window_end,
    cast(label_window_end as timestamp) <= data_end_ts as has_complete_label_window
from cutoffs
where cast(cutoff_date as timestamp) <= data_end_ts
