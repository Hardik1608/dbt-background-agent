-- Point-in-time correctness: no feature row may contain information from on/after its
-- cutoff. If this fails, the churn model would be trained on the future (label leakage).

select
    customer_id,
    cutoff_date,
    first_order_at,
    last_order_at,
    feature_timestamp
from {{ ref('customer_features') }}
where last_order_at >= feature_timestamp
    or first_order_at >= feature_timestamp
    or first_order_at > last_order_at
