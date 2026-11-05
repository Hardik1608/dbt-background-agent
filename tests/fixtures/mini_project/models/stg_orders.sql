select
    id,
    amount,
    customer
from {{ ref('raw_orders') }}
