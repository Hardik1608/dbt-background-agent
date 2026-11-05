-- Gold: monthly revenue KPIs with month-over-month and year-over-year growth.
--
-- Revenue definitions (see docs/DATA_MODEL.md):
--   gross_merchandise_sales  = merchandise sale lines
--   cancelled_merchandise    = merchandise cancellation lines (negative)
--   net_merchandise_revenue  = gross + cancelled
--   non_merchandise_amount   = postage, fees, manual adjustments, bad debt, ...
--   total_net_amount         = everything; reconciles to fct_order_lines (singular test)
--
-- LAG(x, 12) is only a valid "same month last year" because every month has sales; the
-- analyses/ version shows the calendar-spine join that makes it safe in general.

with lines as (

    select
        fct.invoice_id,
        fct.customer_id,
        fct.line_type,
        fct.line_revenue,
        dim_product.is_merchandise,
        dim_date.first_day_of_month
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    inner join {{ ref('dim_date') }} as dim_date
        on fct.date_key = dim_date.date_key

),

bounds as (

    select max(invoice_ts) as data_end_ts from {{ ref('fct_order_lines') }}

),

monthly as (

    select
        first_day_of_month                                                  as revenue_month,
        count(distinct case when line_type = 'sale' then invoice_id end)    as n_orders,
        count(distinct case when line_type = 'sale' then customer_id end)   as n_active_customers,
        sum(case when line_type = 'sale' and is_merchandise
                 then line_revenue else 0 end)                              as gross_merchandise_sales,
        sum(case when line_type = 'cancellation' and is_merchandise
                 then line_revenue else 0 end)                              as cancelled_merchandise,
        sum(case when line_type in ('sale', 'cancellation') and is_merchandise
                 then line_revenue else 0 end)                              as net_merchandise_revenue,
        sum(case when not is_merchandise
                 then line_revenue else 0 end)                              as non_merchandise_amount,
        sum(line_revenue)                                                   as total_net_amount
    from lines
    group by first_day_of_month

),

with_growth as (

    select
        monthly.*,
        lag(net_merchandise_revenue) over (order by revenue_month)      as prev_month_net_revenue,
        lag(net_merchandise_revenue, 12) over (order by revenue_month)  as same_month_last_year_net_revenue,
        sum(net_merchandise_revenue) over (
            partition by extract(year from revenue_month)
            order by revenue_month
            rows between unbounded preceding and current row
        )                                                               as ytd_net_revenue
    from monthly

)

select
    revenue_month,
    n_orders,
    n_active_customers,
    cast(gross_merchandise_sales as decimal(18, 2))                     as gross_merchandise_sales,
    cast(cancelled_merchandise as decimal(18, 2))                       as cancelled_merchandise,
    cast(net_merchandise_revenue as decimal(18, 2))                     as net_merchandise_revenue,
    cast(non_merchandise_amount as decimal(18, 2))                      as non_merchandise_amount,
    cast(total_net_amount as decimal(18, 3))                            as total_net_amount,
    cast(net_merchandise_revenue / nullif(n_orders, 0) as decimal(18, 2)) as avg_order_value,
    round(cast(
        (net_merchandise_revenue - prev_month_net_revenue)
        / nullif(prev_month_net_revenue, 0) as double), 4)              as mom_growth,
    round(cast(
        (net_merchandise_revenue - same_month_last_year_net_revenue)
        / nullif(same_month_last_year_net_revenue, 0) as double), 4)    as yoy_growth,
    cast(ytd_net_revenue as decimal(18, 2))                             as ytd_net_revenue,
    -- the data ends mid-month (2011-12-09): flag it so nobody reads it as a collapse
    cast({{ dbt.dateadd('month', 1, 'revenue_month') }} as timestamp)
        > (select bounds.data_end_ts from bounds)                       as is_partial_month
from with_growth
