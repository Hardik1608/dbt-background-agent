-- Gold: calendar dimension (one row per day). Integer yyyymmdd surrogate key so fact
-- partitions/clustering and BI filters stay human-readable.

with spine as (

    {{ dbt_utils.date_spine(
        datepart="day",
        start_date="cast('2009-01-01' as date)",
        end_date="cast('2013-01-01' as date)"
    ) }}

),

days as (

    select cast(date_day as date) as date_day from spine

)

select
    {{ date_key('date_day') }}                                      as date_key,
    date_day,
    cast(extract(year from date_day) as integer)                    as calendar_year,
    cast(extract(quarter from date_day) as integer)                 as calendar_quarter,
    cast(extract(month from date_day) as integer)                   as month_of_year,
    {{ month_name('date_day') }}                                    as month_name,
    cast(extract(year from date_day) as varchar)
        || '-' || lpad(cast(extract(month from date_day) as varchar), 2, '0')
                                                                    as year_month,
    cast({{ dbt.date_trunc('month', 'date_day') }} as date)         as first_day_of_month,
    cast(extract(day from date_day) as integer)                     as day_of_month,
    cast({{ day_of_week_iso('date_day') }} as integer)              as day_of_week_iso,
    {{ day_name('date_day') }}                                      as day_name,
    {{ day_of_week_iso('date_day') }} in (6, 7)                     as is_weekend
from days
