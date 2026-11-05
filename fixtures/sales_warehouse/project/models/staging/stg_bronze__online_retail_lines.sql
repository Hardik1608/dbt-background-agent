-- Silver / staging: one row per bronze row. Types, standardises and flags every line.
-- Nothing is filtered or deduplicated here, so every downstream exclusion is explicit,
-- documented and testable.

with source as (

    select * from {{ source('bronze', 'online_retail_lines') }}

),

standardised as (

    select
        trim(invoice)                               as invoice_id,
        -- 3,471 real lines use lowercase suffixes ("85123a") for the same product
        upper(trim(stock_code))                     as stock_code,
        -- 213k real descriptions carry stray whitespace
        nullif(trim(description), '')               as description,
        cast(quantity as integer)                   as quantity,
        cast(invoice_date as timestamp)             as line_ts,
        -- money as DECIMAL, never float: prices have at most 3 decimals (e.g. 0.001)
        cast(price as decimal(12, 3))               as unit_price,
        nullif(trim(customer_id), '')               as customer_id,
        trim(country)                               as country_name,
        _source_sheet,
        _source_row_number,
        _batch_id,
        _source_file,
        _ingested_at
    from source

),

flagged as (

    select
        *,
        cast(quantity * unit_price as decimal(18, 3))           as line_revenue,
        invoice_id like 'C%'                                    as is_cancellation,
        customer_id is not null                                 as has_customer,
        unit_price = 0                                          as is_zero_price,
        abs(quantity) >= {{ var('outlier_quantity_threshold') }} as is_quantity_outlier,
        case
            when invoice_id like 'C%' then 'cancellation'
            when invoice_id like 'A%' then 'bad_debt_adjustment'
            -- negative quantity outside a cancellation = inventory write-off
            -- ("damaged", "lost", ...), always priced at 0 in the real data
            when quantity < 0 then 'stock_write_off'
            else 'sale'
        end                                                     as line_type,
        -- content hash of the business columns: identical lines (re-delivered extracts,
        -- the overlapping Excel sheets) collapse to one key in the intermediate layer
        {{ dbt_utils.generate_surrogate_key([
            'invoice_id', 'stock_code', 'description', 'quantity',
            'line_ts', 'unit_price', 'customer_id', 'country_name'
        ]) }}                                                   as line_hash
    from standardised

)

select * from flagged
