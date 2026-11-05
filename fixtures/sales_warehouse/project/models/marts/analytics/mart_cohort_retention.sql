-- Gold: monthly acquisition-cohort retention (long format: cohort x months-since-first).
-- retention_rate = customers of the cohort who bought in month N / cohort size.
--
-- Caveat surfaced as a column: the first cohort (Dec 2009) is left-censored - it holds
-- every customer whose history began before the data did, not just new customers.

with activity as (

    select distinct
        customer_id,
        cast({{ dbt.date_trunc('month', 'invoice_ts') }} as date) as activity_month
    from {{ ref('fct_order_lines') }}
    where customer_id is not null
        and line_type = 'sale'

),

cohorts as (

    select customer_id, min(activity_month) as cohort_month
    from activity
    group by customer_id

),

cohort_sizes as (

    select cohort_month, count(*) as cohort_size
    from cohorts
    group by cohort_month

),

retention as (

    select
        cohorts.cohort_month,
        cast({{ dbt.datediff('cohorts.cohort_month', 'activity.activity_month', 'month') }}
            as integer)                                 as months_since_first_purchase,
        count(distinct activity.customer_id)            as active_customers
    from activity
    inner join cohorts
        on activity.customer_id = cohorts.customer_id
    group by
        cohorts.cohort_month,
        {{ dbt.datediff('cohorts.cohort_month', 'activity.activity_month', 'month') }}

)

select
    retention.cohort_month,
    retention.months_since_first_purchase,
    cohort_sizes.cohort_size,
    retention.active_customers,
    round(cast(retention.active_customers as double) / cohort_sizes.cohort_size, 4)
                                                        as retention_rate,
    retention.cohort_month = (select min(cohorts.cohort_month) from cohorts)
                                                        as is_left_censored_cohort
from retention
inner join cohort_sizes
    on retention.cohort_month = cohort_sizes.cohort_month
