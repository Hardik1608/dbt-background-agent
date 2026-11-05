/*
Q:  "How long do customers take to place their next order? Show the distribution."

Concepts: LEAD() for "next row" comparisons (LAG for previous), DATEDIFF, and a
cumulative share with SUM() OVER (ORDER BY ...) on top of an aggregate - window functions
are evaluated after GROUP BY, so they can run over aggregated rows.

Business use: the distribution motivates the 90-day churn horizon - most repeat orders
happen well inside it, so 90 days of silence is a meaningful signal.
*/

with orders as (

    select customer_id, invoice_id, invoice_ts
    from {{ ref('fct_orders') }}
    where invoice_type = 'sale'
        and customer_id is not null

),

next_orders as (

    select
        customer_id,
        invoice_ts,
        lead(invoice_ts) over (
            partition by customer_id order by invoice_ts, invoice_id
        ) as next_invoice_ts
    from orders

),

intervals as (

    select {{ dbt.datediff('invoice_ts', 'next_invoice_ts', 'day') }} as days_to_next
    from next_orders
    where next_invoice_ts is not null

),

bucketed as (

    select
        case
            when days_to_next <= 7 then '1: 0-7 days'
            when days_to_next <= 30 then '2: 8-30 days'
            when days_to_next <= 60 then '3: 31-60 days'
            when days_to_next <= 90 then '4: 61-90 days'
            when days_to_next <= 180 then '5: 91-180 days'
            else '6: 181+ days'
        end as gap_bucket
    from intervals

)

select
    gap_bucket,
    count(*)                                                        as repeat_orders,
    round(100.0 * count(*) / sum(count(*)) over (), 1)              as pct,
    round(100.0 * sum(count(*)) over (
        order by gap_bucket rows between unbounded preceding and current row
    ) / sum(count(*)) over (), 1)                                   as cumulative_pct
from bucketed
group by gap_bucket
order by gap_bucket
