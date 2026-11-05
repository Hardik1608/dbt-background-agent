-- Silver: one row per distinct invoice line.
--
-- Deduplication keeps the *earliest ingested* copy of each content hash. In the real data
-- this removes 34,335 exact duplicates, including the 22,523 lines delivered twice because
-- the two source sheets overlap on 2010-12-01..09. Keeping the earliest copy makes the
-- result stable when an old batch is re-delivered later (the incremental fact ignores it).

with lines as (

    select * from {{ ref('stg_bronze__online_retail_lines') }}

),

non_product_codes as (

    select stock_code, product_category from {{ ref('non_product_stock_codes') }}

),

ranked as (

    select
        lines.*,
        row_number() over (
            partition by line_hash
            order by _ingested_at, _source_sheet, _source_row_number
        )                                       as copy_number,
        count(*) over (partition by line_hash)  as copies_in_bronze
    from lines

),

deduplicated as (

    select * from ranked where copy_number = 1

),

classified as (

    select
        deduplicated.line_hash                  as order_line_id,
        deduplicated.invoice_id,
        -- one timestamp per invoice: 83 real invoices straddle a minute boundary, and
        -- every line of an invoice must resolve to the same SCD2 customer version
        min(deduplicated.line_ts) over (
            partition by deduplicated.invoice_id
        )                                       as invoice_ts,
        deduplicated.line_ts,
        deduplicated.stock_code,
        deduplicated.description,
        deduplicated.quantity,
        deduplicated.unit_price,
        deduplicated.line_revenue,
        deduplicated.customer_id,
        deduplicated.country_name,
        deduplicated.line_type,
        coalesce(
            non_product_codes.product_category,
            case
                when deduplicated.stock_code like 'GIFT%' then 'gift_voucher'
                when deduplicated.stock_code like 'TEST%' then 'test'
                else 'merchandise'
            end
        )                                       as product_category,
        deduplicated.is_cancellation,
        deduplicated.has_customer,
        deduplicated.is_zero_price,
        deduplicated.is_quantity_outlier,
        deduplicated.copies_in_bronze,
        deduplicated._batch_id,
        deduplicated._ingested_at
    from deduplicated
    left join non_product_codes
        on deduplicated.stock_code = non_product_codes.stock_code

)

select
    *,
    product_category = 'merchandise' as is_merchandise
from classified
