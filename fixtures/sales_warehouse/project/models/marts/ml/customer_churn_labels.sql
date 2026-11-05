-- Gold / ML: churn labels. Grain = (customer_id, cutoff_date).
--
-- Population: customers "active" at the cutoff = at least one merchandise purchase in
--             the {{ var('active_lookback_days') }} days before it (long-gone customers
--             would be trivially "churned" and inflate metrics).
-- Label:      is_churned = 1 if the customer makes NO purchase in
--             [cutoff, cutoff + {{ var('churn_horizon_days') }} days).
-- Only cutoffs whose whole label window is observed are labelled.
--
-- This table is the Feast "entity dataframe": (customer_id, event_timestamp, label).

with cutoffs as (

    select
        cutoff_date,
        label_window_end,
        cast(cutoff_date as timestamp)          as cutoff_ts,
        cast(label_window_end as timestamp)     as window_end_ts
    from {{ ref('int_feature_cutoffs') }}
    where has_complete_label_window

),

purchases as (

    select distinct
        fct.customer_id,
        fct.invoice_id,
        fct.invoice_ts
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    where fct.customer_id is not null
        and fct.line_type = 'sale'
        and dim_product.is_merchandise
        and not fct.is_quantity_outlier

),

active_population as (

    select distinct
        cutoffs.cutoff_date,
        purchases.customer_id
    from cutoffs
    inner join purchases
        on purchases.invoice_ts < cutoffs.cutoff_ts
        and purchases.invoice_ts >= {{ dbt.dateadd('day', -1 * var('active_lookback_days'), 'cutoffs.cutoff_ts') }}

),

future_purchases as (

    select
        cutoffs.cutoff_date,
        purchases.customer_id,
        count(distinct purchases.invoice_id)    as n_orders
    from cutoffs
    inner join purchases
        on purchases.invoice_ts >= cutoffs.cutoff_ts
        and purchases.invoice_ts < cutoffs.window_end_ts
    group by cutoffs.cutoff_date, purchases.customer_id

)

select
    active_population.customer_id,
    active_population.cutoff_date,
    cast(active_population.cutoff_date as timestamp)            as event_timestamp,
    cutoffs.label_window_end,
    cast(case when future_purchases.customer_id is null then 1 else 0 end as integer)
                                                                as is_churned,
    -- future information: for analysis only, never a feature (not exported)
    cast(coalesce(future_purchases.n_orders, 0) as integer)     as orders_in_label_window
from active_population
inner join cutoffs
    on active_population.cutoff_date = cutoffs.cutoff_date
left join future_purchases
    on active_population.customer_id = future_purchases.customer_id
    and active_population.cutoff_date = future_purchases.cutoff_date
