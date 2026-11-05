-- A label may only exist if its whole 90-day window has been observed; otherwise
-- "churned" would really mean "the future has not been loaded yet".

with bounds as (

    select max(invoice_ts) as data_end_ts from {{ ref('fct_order_lines') }}

)

select labels.*
from {{ ref('customer_churn_labels') }} as labels
cross join bounds
where cast(labels.label_window_end as timestamp) > bounds.data_end_ts
