-- The monthly KPI mart must account for every penny in the fact table.

with mart as (

    select sum(total_net_amount) as mart_total from {{ ref('mart_revenue_monthly') }}

),

fact as (

    select sum(line_revenue) as fact_total from {{ ref('fct_order_lines') }}

)

select
    mart.*,
    fact.*
from mart
cross join fact
where mart.mart_total <> fact.fact_total
