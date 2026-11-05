-- Gold: transaction fact. Grain = one row per distinct invoice line (the lowest grain
-- the source offers), so every aggregate - order, customer, product, day - can be
-- derived without re-reading silver.
--
-- Incremental strategy
--   * watermark on _ingested_at (load time), NOT invoice_ts (business time): a late file
--     containing old invoices is still picked up, because it was *loaded* after the
--     last run;
--   * unique_key = order_line_id (content hash) makes reprocessing idempotent - a
--     re-delivered or force-reloaded batch replaces rows instead of duplicating them;
--   * delete+insert on DuckDB; MERGE on Snowflake/BigQuery (both are idempotent upserts).
--
-- Point-in-time dimension lookup: customer_sk is the SCD2 version valid at invoice_ts.

{{
    config(
        materialized='incremental',
        unique_key='order_line_id',
        incremental_strategy=('delete+insert' if target.type == 'duckdb' else 'merge'),
        on_schema_change='fail'
    )
}}

with lines as (

    select * from {{ ref('int_order_lines__deduplicated') }}
    {% if is_incremental() %}
    where _ingested_at > (
        select coalesce(max(_ingested_at), {{ scd_start_of_time() }}) from {{ this }}
    )
    {% endif %}

),

customers as (

    select customer_sk, customer_id, valid_from, valid_to
    from {{ ref('dim_customer') }}
    where customer_sk <> '-1'

),

products as (

    select product_key, stock_code from {{ ref('dim_product') }}

),

countries as (

    select country_key, country_name from {{ ref('dim_country') }}

)

select
    lines.order_line_id,
    lines.invoice_id,
    {{ date_key('lines.invoice_ts') }}                  as date_key,
    coalesce(customers.customer_sk, '-1')               as customer_sk,
    lines.customer_id,
    products.product_key,
    countries.country_key,
    lines.invoice_ts,
    lines.line_type,
    lines.quantity,
    lines.unit_price,
    lines.line_revenue,
    lines.is_quantity_outlier,
    lines._batch_id,
    lines._ingested_at
from lines
left join customers
    on lines.customer_id = customers.customer_id
    and lines.invoice_ts >= customers.valid_from
    and lines.invoice_ts < customers.valid_to
left join products
    on lines.stock_code = products.stock_code
left join countries
    on lines.country_name = countries.country_name
