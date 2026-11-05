/*
Q:  "For each customer, show the first product they ever bought and their most recent
     one, on every order row."

Concepts: FIRST_VALUE / LAST_VALUE and window frames.

Gotcha: with ORDER BY, the default frame ends at the CURRENT ROW, so LAST_VALUE returns
the current row's value, not the partition's last value. You need
"ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING" (or FIRST_VALUE with a
descending sort). The query counts how many rows the naive version gets wrong.
*/

with customer_lines as (

    select
        fct.customer_id,
        fct.invoice_ts,
        fct.order_line_id,
        dim_product.stock_code
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    where fct.customer_id is not null
        and fct.line_type = 'sale'
        and dim_product.is_merchandise

),

windowed as (

    select
        customer_id,
        stock_code,
        first_value(stock_code) over (
            partition by customer_id order by invoice_ts, order_line_id
        )                                               as first_product,
        last_value(stock_code) over (
            partition by customer_id order by invoice_ts, order_line_id
        )                                               as last_product_naive,
        last_value(stock_code) over (
            partition by customer_id order by invoice_ts, order_line_id
            rows between unbounded preceding and unbounded following
        )                                               as last_product_correct
    from customer_lines

)

select
    count(*)                                                            as rows_checked,
    sum(case when last_product_naive = stock_code then 1 else 0 end)    as naive_equals_current_row,
    sum(case when last_product_naive <> last_product_correct then 1 else 0 end)
                                                                        as naive_rows_wrong,
    round(100.0 * sum(case when last_product_naive <> last_product_correct then 1 else 0 end)
          / count(*), 1)                                                as pct_wrong
from windowed
