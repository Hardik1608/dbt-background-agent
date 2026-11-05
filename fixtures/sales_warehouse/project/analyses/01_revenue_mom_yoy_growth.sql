/*
Q:  "Show monthly revenue with month-over-month and year-over-year growth."

Concepts: LAG(), a calendar spine, NULLIF() to avoid division by zero.

Gotcha: LAG(x, 12) means "12 ROWS back", not "12 months back". If any month had no
sales, the offset silently compares against the wrong month. Left-joining revenue onto
a complete month spine turns a missing month into an explicit 0 row first.
*/

with bounds as (

    select
        cast({{ dbt.date_trunc('month', 'min(invoice_ts)') }} as date) as first_month,
        cast({{ dbt.date_trunc('month', 'max(invoice_ts)') }} as date) as last_month
    from {{ ref('fct_order_lines') }}

),

month_spine as (

    select distinct dim_date.first_day_of_month as revenue_month
    from {{ ref('dim_date') }} as dim_date
    cross join bounds
    where dim_date.first_day_of_month between bounds.first_month and bounds.last_month

),

monthly_revenue as (

    select
        dim_date.first_day_of_month     as revenue_month,
        sum(fct.line_revenue)           as net_revenue
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_date') }} as dim_date
        on fct.date_key = dim_date.date_key
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    where dim_product.is_merchandise
        and fct.line_type in ('sale', 'cancellation')
    group by dim_date.first_day_of_month

),

spine_revenue as (

    select
        month_spine.revenue_month,
        coalesce(monthly_revenue.net_revenue, 0) as net_revenue
    from month_spine
    left join monthly_revenue
        on month_spine.revenue_month = monthly_revenue.revenue_month

)

select
    revenue_month,
    round(net_revenue, 2)                                                   as net_revenue,
    round(lag(net_revenue) over (order by revenue_month), 2)                as prev_month_revenue,
    round(100.0 * (net_revenue - lag(net_revenue) over (order by revenue_month))
          / nullif(lag(net_revenue) over (order by revenue_month), 0), 1)   as mom_growth_pct,
    round(lag(net_revenue, 12) over (order by revenue_month), 2)            as same_month_last_year,
    round(100.0 * (net_revenue - lag(net_revenue, 12) over (order by revenue_month))
          / nullif(lag(net_revenue, 12) over (order by revenue_month), 0), 1) as yoy_growth_pct
from spine_revenue
order by revenue_month
