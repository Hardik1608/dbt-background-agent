-- Gold / ML: point-in-time customer features. Grain = (customer_id, cutoff_date).
--
-- Every feature for a cutoff is computed ONLY from facts with invoice_ts < cutoff (strict),
-- so a row describes what was knowable at 00:00 on the cutoff date. This is what makes
-- the training set leakage-free and is enforced by a singular test and a unit test.
-- feature_timestamp (= cutoff) is the event timestamp Feast uses for point-in-time joins.
--
-- Scope: merchandise sales and cancellations of known customers; postage/fees and bulk
-- quantity outliers are excluded (they would dominate monetary features).

with data_start as (

    select cast(min(invoice_ts) as date) as first_data_date
    from {{ ref('fct_order_lines') }}

),

cutoffs as (

    select
        int_feature_cutoffs.cutoff_date,
        cast(int_feature_cutoffs.cutoff_date as timestamp) as cutoff_ts,
        -- last year's equivalent of the label window [cutoff - 365d, cutoff - 275d) is
        -- only usable if it lies entirely inside the data (else: unknown, not zero)
        cast({{ dbt.dateadd('day', -365, 'int_feature_cutoffs.cutoff_date') }} as date)
            >= data_start.first_data_date                   as has_last_year_window
    from {{ ref('int_feature_cutoffs') }} as int_feature_cutoffs
    cross join data_start

),

merch_lines as (

    select
        fct.customer_id,
        fct.invoice_id,
        fct.invoice_ts,
        fct.line_type,
        fct.quantity,
        fct.line_revenue,
        fct.product_key,
        dim_country.country_name
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    inner join {{ ref('dim_country') }} as dim_country
        on fct.country_key = dim_country.country_key
    where fct.customer_id is not null
        and dim_product.is_merchandise
        and not fct.is_quantity_outlier
        and fct.line_type in ('sale', 'cancellation')

),

orders as (

    select
        customer_id,
        invoice_id,
        invoice_ts,
        max(country_name)       as country_name,
        sum(line_revenue)       as order_revenue,
        sum(quantity)           as order_units
    from merch_lines
    where line_type = 'sale'
    group by customer_id, invoice_id, invoice_ts

),

cancellations as (

    select
        customer_id,
        invoice_id,
        invoice_ts,
        sum(line_revenue)       as cancelled_revenue
    from merch_lines
    where line_type = 'cancellation'
    group by customer_id, invoice_id, invoice_ts

),

-- point-in-time join: each cutoff only "sees" orders strictly before it
orders_as_of as (

    select
        cutoffs.cutoff_date,
        cutoffs.cutoff_ts,
        orders.*,
        {{ dbt.datediff('orders.invoice_ts', 'cutoffs.cutoff_ts', 'day') }} as days_before_cutoff,
        row_number() over (
            partition by cutoffs.cutoff_date, orders.customer_id
            order by orders.invoice_ts desc, orders.invoice_id desc
        )                                                               as order_recency_rank
    from cutoffs
    inner join orders
        on orders.invoice_ts < cutoffs.cutoff_ts

),

order_features as (

    select
        cutoff_date,
        customer_id,
        min(invoice_ts)                                                         as first_order_at,
        max(invoice_ts)                                                         as last_order_at,
        count(*)                                                                as frequency_orders,
        sum(case when days_before_cutoff <= 30 then 1 else 0 end)               as orders_last_30d,
        sum(case when days_before_cutoff <= 90 then 1 else 0 end)               as orders_last_90d,
        sum(case when days_before_cutoff > 90 and days_before_cutoff <= 180
                 then 1 else 0 end)                                             as orders_prev_90d,
        sum(case when days_before_cutoff <= 365 then 1 else 0 end)              as orders_last_365d,
        -- seasonality: orders in last year's equivalent of the label window
        sum(case when days_before_cutoff > 275 and days_before_cutoff <= 365
                 then 1 else 0 end)                                             as orders_same_window_ly,
        sum(order_revenue)                                                      as monetary_gross,
        sum(case when days_before_cutoff <= 90 then order_revenue else 0 end)   as revenue_last_90d,
        sum(case when days_before_cutoff <= 365 then order_revenue else 0 end)  as revenue_last_365d,
        avg(order_revenue)                                                      as avg_order_value,
        avg(order_units)                                                        as avg_units_per_order,
        count(distinct case when days_before_cutoff <= 365
                            then {{ dbt.date_trunc('month', 'invoice_ts') }} end) as active_months_last_365d,
        max(case when order_recency_rank = 1 then country_name end)             as latest_country
    from orders_as_of
    group by cutoff_date, customer_id

),

cancellation_features as (

    select
        cutoffs.cutoff_date,
        cancellations.customer_id,
        count(*)                                    as n_cancellations,
        sum(cancellations.cancelled_revenue)        as cancelled_revenue
    from cutoffs
    inner join cancellations
        on cancellations.invoice_ts < cutoffs.cutoff_ts
    group by cutoffs.cutoff_date, cancellations.customer_id

),

product_features as (

    select
        cutoffs.cutoff_date,
        merch_lines.customer_id,
        count(distinct merch_lines.product_key)     as n_distinct_products
    from cutoffs
    inner join merch_lines
        on merch_lines.invoice_ts < cutoffs.cutoff_ts
        and merch_lines.line_type = 'sale'
    group by cutoffs.cutoff_date, merch_lines.customer_id

),

combined as (

    select
        order_features.customer_id,
        order_features.cutoff_date,
        order_features.first_order_at,
        order_features.last_order_at,
        {{ dbt.datediff('order_features.last_order_at',
                        'cast(order_features.cutoff_date as timestamp)', 'day') }}  as recency_days,
        {{ dbt.datediff('order_features.first_order_at',
                        'cast(order_features.cutoff_date as timestamp)', 'day') }}  as tenure_days,
        order_features.frequency_orders,
        order_features.orders_last_30d,
        order_features.orders_last_90d,
        order_features.orders_prev_90d,
        order_features.orders_last_365d,
        order_features.monetary_gross,
        order_features.monetary_gross
            + coalesce(cancellation_features.cancelled_revenue, 0)                as monetary_net,
        order_features.revenue_last_90d,
        order_features.revenue_last_365d,
        order_features.avg_order_value,
        order_features.avg_units_per_order,
        case
            when order_features.frequency_orders > 1
                then {{ dbt.datediff('order_features.first_order_at',
                                     'order_features.last_order_at', 'day') }}
                     * 1.0 / (order_features.frequency_orders - 1)
        end                                                                     as avg_days_between_orders,
        coalesce(cancellation_features.n_cancellations, 0)                      as n_cancellations,
        coalesce(cancellation_features.n_cancellations, 0) * 1.0
            / (order_features.frequency_orders
               + coalesce(cancellation_features.n_cancellations, 0))            as cancellation_rate,
        product_features.n_distinct_products,
        order_features.active_months_last_365d,
        case when order_features.latest_country = 'United Kingdom' then 1 else 0 end
                                                                                as is_domestic,
        case
            when cutoffs.has_last_year_window then order_features.orders_same_window_ly
        end                                                                     as orders_same_window_last_year,
        extract(month from order_features.cutoff_date)                          as cutoff_month_of_year
    from order_features
    inner join cutoffs
        on order_features.cutoff_date = cutoffs.cutoff_date
    left join cancellation_features
        on order_features.customer_id = cancellation_features.customer_id
        and order_features.cutoff_date = cancellation_features.cutoff_date
    left join product_features
        on order_features.customer_id = product_features.customer_id
        and order_features.cutoff_date = product_features.cutoff_date

),

scored as (

    -- RFM quintiles within each cutoff. CUME_DIST (not NTILE): NTILE splits tied values
    -- across buckets arbitrarily, which makes features non-deterministic between builds.
    select
        *,
        ceil(cume_dist() over (partition by cutoff_date order by recency_days desc) * 5)
                                                            as rfm_recency_score,
        ceil(cume_dist() over (partition by cutoff_date order by frequency_orders) * 5)
                                                            as rfm_frequency_score,
        ceil(cume_dist() over (partition by cutoff_date order by monetary_net) * 5)
                                                            as rfm_monetary_score
    from combined

)

select
    customer_id,
    cutoff_date,
    cast(cutoff_date as timestamp)                          as feature_timestamp,
    first_order_at,
    last_order_at,
    cast(recency_days as integer)                           as recency_days,
    cast(tenure_days as integer)                            as tenure_days,
    cast(frequency_orders as integer)                       as frequency_orders,
    cast(orders_last_30d as integer)                        as orders_last_30d,
    cast(orders_last_90d as integer)                        as orders_last_90d,
    cast(orders_prev_90d as integer)                        as orders_prev_90d,
    cast(orders_last_365d as integer)                       as orders_last_365d,
    cast(monetary_gross as double)                          as monetary_gross,
    cast(monetary_net as double)                            as monetary_net,
    cast(revenue_last_90d as double)                        as revenue_last_90d,
    cast(revenue_last_365d as double)                       as revenue_last_365d,
    cast(avg_order_value as double)                         as avg_order_value,
    cast(avg_units_per_order as double)                     as avg_units_per_order,
    cast(avg_days_between_orders as double)                 as avg_days_between_orders,
    cast(n_cancellations as integer)                        as n_cancellations,
    cast(cancellation_rate as double)                       as cancellation_rate,
    cast(n_distinct_products as integer)                    as n_distinct_products,
    cast(active_months_last_365d as integer)                as active_months_last_365d,
    cast(is_domestic as integer)                            as is_domestic,
    cast(orders_same_window_last_year as integer)           as orders_same_window_last_year,
    cast(cutoff_month_of_year as integer)                   as cutoff_month_of_year,
    -- "how overdue": days since last order relative to the customer's own rhythm
    cast(recency_days / nullif(avg_days_between_orders, 0) as double)
                                                            as recency_to_cadence_ratio,
    cast(rfm_recency_score as integer)                      as rfm_recency_score,
    cast(rfm_frequency_score as integer)                    as rfm_frequency_score,
    cast(rfm_monetary_score as integer)                     as rfm_monetary_score
from scored
