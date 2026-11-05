/*
Q:  "Year-to-date running revenue and a 3-month moving average."

Concepts: SUM()/AVG() OVER with explicit frames; PARTITION BY to reset the running total
each year.

Gotcha (ROWS vs RANGE): with ORDER BY and no frame, the default frame is
"RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW", which treats rows with equal ORDER BY
values as one peer group - a running total over invoice lines would jump by a whole
invoice at once, because every line of an invoice shares invoice_ts. Always write the
frame. ROWS counts physical rows; "3-month moving average" with ROWS is only correct
because the input has exactly one row per month (see 01 for the spine).
*/

with monthly as (

    select
        dim_date.first_day_of_month     as revenue_month,
        dim_date.calendar_year,
        sum(fct.line_revenue)           as net_revenue
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_date') }} as dim_date
        on fct.date_key = dim_date.date_key
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    where dim_product.is_merchandise
        and fct.line_type in ('sale', 'cancellation')
    group by dim_date.first_day_of_month, dim_date.calendar_year

)

select
    revenue_month,
    round(net_revenue, 2) as net_revenue,
    round(sum(net_revenue) over (
        partition by calendar_year
        order by revenue_month
        rows between unbounded preceding and current row
    ), 2)                                                   as ytd_revenue,
    round(avg(net_revenue) over (
        order by revenue_month
        rows between 2 preceding and current row
    ), 2)                                                   as moving_avg_3m,
    -- how many months the moving average actually used (the first two are partial)
    count(*) over (
        order by revenue_month
        rows between 2 preceding and current row
    )                                                       as months_in_window
from monthly
order by revenue_month
