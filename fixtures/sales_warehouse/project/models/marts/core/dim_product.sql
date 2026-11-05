-- Gold: product dimension, SCD Type 1 (attributes are overwritten on every build).
--
-- A stock code carries several descriptions in the source (typos, and free-text write-off
-- notes like "damaged" or "?"), so the canonical description is the one used most often
-- on priced sale lines (newest wins ties). Type 1 is a deliberate choice: nobody needs
-- revenue "as the product was described at the time" - contrast with dim_customer.

with lines as (

    select * from {{ ref('int_order_lines__deduplicated') }}

),

description_votes as (

    select
        stock_code,
        description,
        count(*)            as n_lines,
        max(line_ts)        as last_used_at
    from lines
    where description is not null
        and line_type = 'sale'
        and unit_price > 0
    group by stock_code, description

),

canonical_description as (

    select stock_code, description
    from (
        select
            stock_code,
            description,
            row_number() over (
                partition by stock_code
                order by n_lines desc, last_used_at desc, description asc
            ) as rn
        from description_votes
    ) as ranked
    where rn = 1

),

fallback_description as (

    -- codes never sold at a positive price (e.g. only written off) still need a label
    select stock_code, max(description) as description
    from lines
    group by stock_code

),

product_stats as (

    select
        stock_code,
        max(product_category)                                       as product_category,
        min(invoice_ts)                                             as first_seen_at,
        max(invoice_ts)                                             as last_seen_at,
        cast(avg(
            case when line_type = 'sale' and unit_price > 0 then unit_price end
        ) as decimal(12, 3))                                        as avg_sale_price
    from lines
    group by stock_code

)

select
    {{ dbt_utils.generate_surrogate_key(['product_stats.stock_code']) }} as product_key,
    product_stats.stock_code,
    coalesce(
        canonical_description.description,
        fallback_description.description,
        'UNKNOWN'
    )                                                               as product_description,
    product_stats.product_category,
    product_stats.product_category = 'merchandise'                  as is_merchandise,
    product_stats.avg_sale_price,
    product_stats.first_seen_at,
    product_stats.last_seen_at
from product_stats
left join canonical_description
    on product_stats.stock_code = canonical_description.stock_code
left join fallback_description
    on product_stats.stock_code = fallback_description.stock_code
