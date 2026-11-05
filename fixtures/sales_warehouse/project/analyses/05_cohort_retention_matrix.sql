/*
Q:  "Build a cohort retention matrix: % of each acquisition-month cohort still buying
     1, 2, 3, 6 and 12 months later."

Concepts: cohort assignment with MIN() per customer, DATEDIFF in months, and a portable
PIVOT via conditional aggregation (MAX(CASE ...)) - works on every warehouse, unlike the
vendor-specific PIVOT syntax.

Gotchas:
  * the first cohort (Dec 2009) is left-censored: it contains every customer whose
    history started before the data did, so its "retention" is not comparable;
  * late cohorts have NULLs for months that have not happened yet - NULL is not 0%.
*/

with retention as (

    select * from {{ ref('mart_cohort_retention') }}

)

select
    cohort_month,
    cohort_size,
    is_left_censored_cohort,
    max(case when months_since_first_purchase = 1 then retention_rate end)  as m1,
    max(case when months_since_first_purchase = 2 then retention_rate end)  as m2,
    max(case when months_since_first_purchase = 3 then retention_rate end)  as m3,
    max(case when months_since_first_purchase = 6 then retention_rate end)  as m6,
    max(case when months_since_first_purchase = 12 then retention_rate end) as m12
from retention
group by cohort_month, cohort_size, is_left_censored_cohort
order by cohort_month
