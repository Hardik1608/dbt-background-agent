-- Silver: the *current* tracked state of each customer, as of the latest loaded data,
-- plus the business timestamp at which that state began. Source for the SCD2 snapshot.
--
-- Tracked (Type 2) attributes:
--   * country       - the country of the customer's most recent invoice
--   * loyalty_tier  - from cumulative net merchandise spend (see macros/business_rules.sql)
--
-- state_effective_at is computed with a gaps-and-islands pattern: every invoice where
-- (country, tier) differs from the previous invoice starts a new "island"; the current
-- state began at the first invoice of the last island. The snapshot uses it as
-- `updated_at`, so SCD2 validity is expressed in *business* time, not load time.

with invoices as (

    select * from {{ ref('int_customer_invoices') }}

),

running_spend as (

    select
        customer_id,
        invoice_id,
        invoice_ts,
        country_name,
        sum(net_merchandise_amount) over (
            partition by customer_id
            order by invoice_ts, invoice_id
            rows between unbounded preceding and current row
        )                                   as cumulative_net_spend
    from invoices

),

tiered as (

    select
        *,
        {{ loyalty_tier('cumulative_net_spend') }} as loyalty_tier
    from running_spend

),

state_changes as (

    select
        *,
        case
            when lag(country_name) over (
                    partition by customer_id order by invoice_ts, invoice_id
                ) = country_name
                and lag(loyalty_tier) over (
                    partition by customer_id order by invoice_ts, invoice_id
                ) = loyalty_tier
                then 0
            else 1  -- first invoice (lag is null) or a change starts a new island
        end                                 as starts_new_state
    from tiered

),

islands as (

    select
        *,
        sum(starts_new_state) over (
            partition by customer_id
            order by invoice_ts, invoice_id
            rows between unbounded preceding and current row
        )                                   as state_number,
        row_number() over (
            partition by customer_id
            order by invoice_ts desc, invoice_id desc
        )                                   as recency_rank
    from state_changes

),

latest_island as (

    select customer_id, max(state_number) as state_number
    from islands
    group by customer_id

),

current_state as (

    select
        islands.customer_id,
        min(islands.invoice_ts)             as state_effective_at
    from islands
    inner join latest_island
        on islands.customer_id = latest_island.customer_id
        and islands.state_number = latest_island.state_number
    group by islands.customer_id

),

customer_summary as (

    select
        customer_id,
        min(invoice_ts)                     as first_invoice_at
    from invoices
    group by customer_id

)

-- Only tracked attributes + immutable ones: with the timestamp strategy, a column that
-- changes on every invoice (e.g. lifetime spend) would be frozen at version creation.
select
    islands.customer_id,
    islands.country_name,
    islands.loyalty_tier,
    current_state.state_effective_at,
    customer_summary.first_invoice_at
from islands
inner join current_state
    on islands.customer_id = current_state.customer_id
inner join customer_summary
    on islands.customer_id = customer_summary.customer_id
where islands.recency_rank = 1
