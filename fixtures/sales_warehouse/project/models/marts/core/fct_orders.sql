-- Gold: order (invoice) fact - an aggregate of fct_order_lines at invoice grain, for
-- order-level questions (AOV, basket size, median order value) without a GROUP BY in
-- every dashboard query. Rebuilt in full: ~54k rows, cheaper than incremental logic.

with lines as (

    select
        fct_order_lines.*,
        dim_product.is_merchandise
    from {{ ref('fct_order_lines') }} as fct_order_lines
    inner join {{ ref('dim_product') }} as dim_product
        on fct_order_lines.product_key = dim_product.product_key

)

select
    invoice_id,
    -- all lines of an invoice share invoice_ts and customer -> one customer version
    min(customer_sk)                                                as customer_sk,
    min(customer_id)                                                as customer_id,
    min(country_key)                                                as country_key,
    min(date_key)                                                   as date_key,
    min(invoice_ts)                                                 as invoice_ts,
    case
        when invoice_id like 'C%' then 'cancellation'
        when invoice_id like 'A%' then 'bad_debt_adjustment'
        when max(quantity) < 0 then 'stock_write_off'
        else 'sale'
    end                                                             as invoice_type,
    cast(count(*) as integer)                                       as n_lines,
    cast(count(distinct product_key) as integer)                    as n_distinct_products,
    cast(sum(quantity) as bigint)                                   as total_units,
    cast(sum(line_revenue) as decimal(18, 3))                       as invoice_revenue,
    cast(sum(
        case when is_merchandise then line_revenue else 0 end
    ) as decimal(18, 3))                                            as merchandise_revenue,
    max(case when is_quantity_outlier then 1 else 0 end) = 1        as has_quantity_outlier
from lines
group by invoice_id
