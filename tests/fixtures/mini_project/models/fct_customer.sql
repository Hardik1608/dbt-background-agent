select
    customer,
    count(*) as n_orders,
    sum(amount) as total_amount
from {{ ref('stg_orders') }}
group by customer
