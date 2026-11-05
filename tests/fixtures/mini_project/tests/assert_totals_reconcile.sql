-- every penny in staging is in the customer fact
select 1
from (select sum(amount) as s from {{ ref('stg_orders') }}) as a
cross join (select sum(total_amount) as f from {{ ref('fct_customer') }}) as b
where a.s <> b.f
