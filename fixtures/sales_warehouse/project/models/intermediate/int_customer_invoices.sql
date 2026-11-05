-- Silver: one row per (customer, invoice) - the event stream behind the SCD2 customer
-- state. Every invoice of a known customer is kept (so every customer in the fact has a
-- dimension row); only merchandise lines (no postage/fees) and no bulk outliers count
-- towards spend, and cancellations count negatively (net spend).

select
    customer_id,
    invoice_id,
    invoice_ts,
    is_cancellation,
    -- an invoice carries exactly one country in the source; max() is a deterministic pick
    max(country_name)                                           as country_name,
    sum(
        case
            when is_merchandise
                and not is_quantity_outlier
                and line_type in ('sale', 'cancellation')
                then line_revenue
            else 0
        end
    )                                                           as net_merchandise_amount,
    count(*)                                                    as n_lines
from {{ ref('int_order_lines__deduplicated') }}
where customer_id is not null
group by customer_id, invoice_id, invoice_ts, is_cancellation
