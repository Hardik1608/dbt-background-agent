/*
Q:  "For each customer, find the longest run of consecutive months with a purchase."

Concepts: gaps-and-islands. Number the active months per customer with ROW_NUMBER();
for consecutive months, (month_index - row_number) is constant, so it identifies the
island (streak). A gap in activity bumps the difference and starts a new island.

  month_index : 3 4 5   8 9   12
  row_number  : 1 2 3   4 5    6
  difference  : 2 2 2   4 4    6   -> islands of length 3, 2, 1

The same pattern drives int_customer_state (SCD2 state changes) in production code.
*/

with customer_months as (

    select distinct
        fct.customer_id,
        dim_date.first_day_of_month                                 as active_month,
        {{ dbt.datediff("cast('2000-01-01' as date)", 'dim_date.first_day_of_month', 'month') }}
                                                                    as month_index
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_date') }} as dim_date
        on fct.date_key = dim_date.date_key
    where fct.customer_id is not null
        and fct.line_type = 'sale'

),

islands as (

    select
        customer_id,
        active_month,
        month_index - row_number() over (
            partition by customer_id order by month_index
        )                                                           as island_id
    from customer_months

),

streaks as (

    select
        customer_id,
        island_id,
        count(*)            as streak_months,
        min(active_month)   as streak_start,
        max(active_month)   as streak_end
    from islands
    group by customer_id, island_id

),

longest as (

    select
        customer_id,
        streak_months,
        streak_start,
        streak_end,
        row_number() over (
            partition by customer_id order by streak_months desc, streak_start asc
        ) as rn
    from streaks

)

select
    streak_months                           as longest_streak_months,
    count(*)                                as customers,
    round(100.0 * count(*) / sum(count(*)) over (), 1) as pct_of_customers,
    min(streak_start)                       as earliest_streak_start
from longest
where rn = 1
group by streak_months
order by streak_months desc
