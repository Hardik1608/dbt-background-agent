-- Point-in-time dimension lookup check. Each fact line of a known customer must
-- reference *that* customer's SCD2 version that was valid when the invoice happened;
-- guest checkouts must reference the unknown member ('-1').

select
    fct.order_line_id,
    fct.customer_id,
    fct.customer_sk,
    fct.invoice_ts,
    dim.valid_from,
    dim.valid_to
from {{ ref('fct_order_lines') }} as fct
left join {{ ref('dim_customer') }} as dim
    on fct.customer_sk = dim.customer_sk
where
    (fct.customer_id is null and fct.customer_sk <> '-1')
    or (
        fct.customer_id is not null
        and (
            dim.customer_sk is null
            or dim.customer_id <> fct.customer_id
            or fct.invoice_ts < dim.valid_from
            or fct.invoice_ts >= dim.valid_to
        )
    )
