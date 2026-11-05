/*
Q:  "Segment customers with RFM (recency, frequency, monetary) quintile scores."

Concepts: NTILE vs CUME_DIST/PERCENT_RANK for scoring.

Gotcha: NTILE(5) splits rows into 5 equal-sized buckets, so customers with the SAME
recency can land in different buckets depending on physical row order - the score is not
a function of the value and can change between runs. Frequency is heavily tied (most
customers have 1-2 orders), which makes this very visible. customer_features uses
CEIL(CUME_DIST() * 5): ties always share a score. The last column counts how often the
two methods disagree on the latest cutoff.
*/

with latest as (

    select *
    from {{ ref('customer_features') }}
    where cutoff_date = (
        select max(all_cutoffs.cutoff_date)
        from {{ ref('customer_features') }} as all_cutoffs
    )

),

scored as (

    select
        customer_id,
        recency_days,
        frequency_orders,
        monetary_net,
        rfm_recency_score,
        rfm_frequency_score,
        rfm_monetary_score,
        ntile(5) over (order by frequency_orders) as frequency_ntile
    from latest

),

segmented as (

    select
        *,
        case
            when rfm_recency_score >= 4 and rfm_frequency_score >= 4 then '1 Champions'
            when rfm_recency_score >= 3 and rfm_frequency_score >= 3 then '2 Loyal'
            when rfm_recency_score >= 4 and rfm_frequency_score <= 2 then '3 New / promising'
            when rfm_recency_score <= 2 and rfm_frequency_score >= 3 then '4 At risk'
            when rfm_recency_score <= 2 and rfm_frequency_score <= 2 then '5 Hibernating'
            else '6 Needs attention'
        end as segment
    from scored

)

select
    segment,
    count(*)                                        as customers,
    round(avg(recency_days), 1)                     as avg_recency_days,
    round(avg(frequency_orders), 1)                 as avg_orders,
    round(avg(monetary_net), 0)                     as avg_net_spend,
    sum(case when frequency_ntile <> rfm_frequency_score then 1 else 0 end)
                                                    as ntile_vs_cume_dist_disagreements
from segmented
group by segment
order by segment
