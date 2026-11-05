-- Every labelled (customer, cutoff) must have a feature row with the same cutoff,
-- otherwise the point-in-time join in Feast would silently return NULL features.

select
    labels.customer_id,
    labels.cutoff_date
from {{ ref('customer_churn_labels') }} as labels
left join {{ ref('customer_features') }} as features
    on labels.customer_id = features.customer_id
    and labels.cutoff_date = features.cutoff_date
where features.customer_id is null
