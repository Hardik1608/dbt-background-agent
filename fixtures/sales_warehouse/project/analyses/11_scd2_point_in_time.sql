/*
Q:  "Report revenue by customer loyalty tier. Should a sale be credited to the tier the
     customer had WHEN THEY BOUGHT, or the tier they have TODAY?"

Concepts: SCD Type 2 lookups.
  * join fact.customer_sk -> dim_customer.customer_sk : tier at time of sale (history
    preserved, because the fact stored the version key when it was loaded)
  * join fact.customer_id -> dim_customer where is_current : today's tier (Type 1 view)
Both are legitimate questions; SCD2 is what makes the first one answerable at all.

Run this against the replay warehouse (snapshots taken monthly) to see the columns
diverge; on a one-shot load every customer has a single back-dated version, so both
columns are identical - which is itself the interview point about snapshot cadence.
*/

with lines as (

    select
        fct.customer_sk,
        fct.customer_id,
        fct.line_revenue
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    where fct.customer_id is not null
        and dim_product.is_merchandise
        and fct.line_type in ('sale', 'cancellation')

),

tier_at_time_of_sale as (

    select dim_customer.loyalty_tier, sum(lines.line_revenue) as revenue
    from lines
    inner join {{ ref('dim_customer') }} as dim_customer
        on lines.customer_sk = dim_customer.customer_sk
    group by dim_customer.loyalty_tier

),

tier_today as (

    select dim_customer.loyalty_tier, sum(lines.line_revenue) as revenue
    from lines
    inner join {{ ref('dim_customer') }} as dim_customer
        on lines.customer_id = dim_customer.customer_id
        and dim_customer.is_current
    group by dim_customer.loyalty_tier

)

select
    coalesce(tier_at_time_of_sale.loyalty_tier, tier_today.loyalty_tier) as loyalty_tier,
    round(tier_at_time_of_sale.revenue, 0)                               as revenue_by_tier_at_sale,
    round(tier_today.revenue, 0)                                         as revenue_by_tier_today
from tier_at_time_of_sale
full outer join tier_today
    on tier_at_time_of_sale.loyalty_tier = tier_today.loyalty_tier
order by
    case coalesce(tier_at_time_of_sale.loyalty_tier, tier_today.loyalty_tier)
        when 'bronze' then 1 when 'silver' then 2 when 'gold' then 3 else 4
    end
