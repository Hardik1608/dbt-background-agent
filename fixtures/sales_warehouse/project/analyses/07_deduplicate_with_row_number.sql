/*
Q:  "The raw table contains duplicate rows. Return one row per record, keeping the
     earliest loaded copy - and explain why SELECT DISTINCT is not enough."

Concepts: ROW_NUMBER() OVER (PARTITION BY <business key> ORDER BY <tie-breaker>) = 1.

Why not DISTINCT?
  * duplicates in a raw layer usually differ in metadata (load time, file, row number),
    so SELECT DISTINCT * keeps every copy;
  * SELECT DISTINCT <business columns> drops the metadata you need for lineage and
    cannot choose WHICH copy survives (earliest? latest? from the trusted file?).
The ORDER BY in the window is the business rule; make it deterministic (add tie-breakers).

This query measures all three approaches on the bronze/staging data.
*/

with staged as (

    select * from {{ ref('stg_bronze__online_retail_lines') }}

),

ranked as (

    select
        line_hash,
        row_number() over (
            partition by line_hash
            order by _ingested_at, _source_sheet, _source_row_number
        ) as copy_number
    from staged

)

select
    (select count(*) from staged)                                  as raw_rows,
    (select count(*) from (select distinct * from staged) as d)    as distinct_star_rows,
    (select count(distinct line_hash) from staged)                 as distinct_business_rows,
    (select count(*) from ranked where copy_number = 1)            as row_number_rows,
    (select count(*) from ranked where copy_number > 1)            as duplicates_removed
