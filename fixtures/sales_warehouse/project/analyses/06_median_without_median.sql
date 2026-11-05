/*
Q:  "Median order value per country - without MEDIAN() or PERCENTILE_CONT()."

Concepts: ROW_NUMBER() + COUNT() OVER the same partition. With n ordered values the
median is the average of positions floor((n+1)/2) and floor((n+2)/2):
  n = 5 -> positions 3 and 3 (one middle value)
  n = 4 -> positions 2 and 3 (average of two)
Divide by 2.0, not 2: integer division is a classic silent bug.

The built-in is shown alongside to prove the manual version matches. Dialect note:
PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) works on DuckDB/Snowflake/Postgres;
BigQuery only has the analytic form PERCENTILE_CONT(x, 0.5) OVER (PARTITION BY ...).
*/

with orders as (

    select
        dim_country.country_name,
        fct_orders.merchandise_revenue  as order_value
    from {{ ref('fct_orders') }} as fct_orders
    inner join {{ ref('dim_country') }} as dim_country
        on fct_orders.country_key = dim_country.country_key
    where fct_orders.invoice_type = 'sale'
        and fct_orders.merchandise_revenue > 0

),

ranked as (

    select
        country_name,
        order_value,
        row_number() over (partition by country_name order by order_value) as rn,
        count(*) over (partition by country_name)                           as n
    from orders

),

manual_median as (

    select
        country_name,
        max(n)              as n_orders,
        avg(order_value)    as median_manual
    from ranked
    where rn in (floor((n + 1) / 2.0), floor((n + 2) / 2.0))
    group by country_name

),

builtin_median as (

    select
        country_name,
        percentile_cont(0.5) within group (order by order_value) as median_builtin
    from orders
    group by country_name

)

select
    manual_median.country_name,
    manual_median.n_orders,
    round(manual_median.median_manual, 2)                               as median_manual,
    round(builtin_median.median_builtin, 2)                             as median_builtin,
    abs(manual_median.median_manual - builtin_median.median_builtin) < 0.005 as matches
from manual_median
inner join builtin_median
    on manual_median.country_name = builtin_median.country_name
order by manual_median.n_orders desc
limit 10
