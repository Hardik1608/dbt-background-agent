/*
Source-data profile of the raw (bronze) layer.

Not an interview query: it documents *why* each silver-layer rule exists, and it is the
reproducible source of the raw-data numbers quoted in docs/DATA_MODEL.md. Duplicates are
counted on the raw values (before any trimming), across every column of the source.
*/

with bronze as (

    select * from {{ source('bronze', 'online_retail_lines') }}

),

content_groups as (

    select
        invoice,
        stock_code,
        description,
        quantity,
        invoice_date,
        price,
        customer_id,
        country,
        count(*)                                                        as copies,
        -- copies per source sheet (the real workbook has two overlapping sheets)
        sum(case when _source_sheet = 'Year 2009-2010' then 1 else 0 end) as copies_sheet_1,
        sum(case when _source_sheet = 'Year 2010-2011' then 1 else 0 end) as copies_sheet_2
    from bronze
    group by invoice, stock_code, description, quantity, invoice_date, price, customer_id, country

),

invoice_timestamps as (

    select invoice, count(distinct invoice_date) as n_timestamps
    from bronze
    group by invoice

),

customer_countries as (

    select customer_id, count(distinct country) as n_countries
    from bronze
    where customer_id is not null
    group by customer_id

)

select
    (select count(*) from bronze)                                               as raw_lines,
    (select min(invoice_date) from bronze)                                      as first_invoice_at,
    (select max(invoice_date) from bronze)                                      as last_invoice_at,
    (select sum(copies - 1) from content_groups)                                as exact_duplicate_lines,
    -- lines delivered by both sheets = per content, min(copies in sheet 1, copies in sheet 2)
    (select sum(least(copies_sheet_1, copies_sheet_2)) from content_groups)    as lines_in_both_sheets,
    (select count(*) from bronze where customer_id is null)                     as missing_customer_lines,
    (select count(*) from bronze where invoice like 'C%')                       as cancellation_lines,
    (select count(*) from bronze where invoice like 'A%')                       as adjustment_lines,
    (select count(*) from bronze where quantity < 0 and invoice not like 'C%')  as negative_qty_outside_cancellations,
    (select count(*) from bronze where price = 0)                               as zero_price_lines,
    (select count(*) from bronze where stock_code <> upper(stock_code))         as lowercase_code_lines,
    (select count(*) from bronze where description <> trim(description))        as untrimmed_description_lines,
    (select count(*) from bronze where description is null)                     as missing_description_lines,
    (
        select count(distinct upper(trim(stock_code))) from bronze
        where left(trim(stock_code), 1) not between '0' and '9'
    )                                                                           as codes_not_starting_with_digit,
    (select count(*) from invoice_timestamps where n_timestamps > 1)            as invoices_with_two_timestamps,
    (select count(*) from customer_countries where n_countries > 1)             as customers_in_multiple_countries,
    (select max(abs(quantity)) from bronze)                                     as max_abs_quantity
