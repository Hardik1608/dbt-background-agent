/*
Q:  "What share of customers generates 80% of revenue? How much do the top 10% bring?"

Concepts: running share = SUM() OVER (ORDER BY revenue DESC ROWS UNBOUNDED PRECEDING)
divided by SUM() OVER () (the grand total, no ORDER BY = whole partition). Find the first
rank where the running share crosses 80% with a conditional MIN().
*/

with customer_revenue as (

    select
        fct.customer_id,
        sum(fct.line_revenue) as revenue
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    where fct.customer_id is not null
        and dim_product.is_merchandise
        and fct.line_type in ('sale', 'cancellation')
    group by fct.customer_id
    having sum(fct.line_revenue) > 0

),

ranked as (

    select
        customer_id,
        revenue,
        row_number() over (order by revenue desc, customer_id asc)     as revenue_rank,
        count(*) over ()                                                as n_customers,
        sum(revenue) over (
            order by revenue desc, customer_id asc
            rows between unbounded preceding and current row
        ) / sum(revenue) over ()                                        as cumulative_share
    from customer_revenue

)

select
    max(n_customers)                                                    as customers,
    min(case when cumulative_share >= 0.8 then revenue_rank end)        as customers_for_80pct_revenue,
    round(100.0 * min(case when cumulative_share >= 0.8 then revenue_rank end)
          / max(n_customers), 1)                                        as pct_customers_for_80pct_revenue,
    round(100.0 * sum(case when revenue_rank <= 0.1 * n_customers then revenue end)
          / sum(revenue), 1)                                            as top_10pct_customers_revenue_share,
    round(100.0 * sum(case when revenue_rank <= 0.01 * n_customers then revenue end)
          / sum(revenue), 1)                                            as top_1pct_customers_revenue_share
from ranked
