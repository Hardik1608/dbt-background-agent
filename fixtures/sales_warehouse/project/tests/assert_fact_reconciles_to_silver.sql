-- Revenue + row-count reconciliation, silver -> gold.
-- The fact keeps every line type, so its row count and total revenue must equal the
-- deduplicated silver lines exactly (DECIMAL arithmetic, no tolerance needed). Catches
-- join fan-out, dropped rows from inner joins, and broken incremental watermarks.

with silver as (

    select
        count(*)            as silver_rows,
        sum(line_revenue)   as silver_revenue
    from {{ ref('int_order_lines__deduplicated') }}

),

gold as (

    select
        count(*)            as gold_rows,
        sum(line_revenue)   as gold_revenue
    from {{ ref('fct_order_lines') }}

)

select
    silver.*,
    gold.*
from silver
cross join gold
where silver.silver_rows <> gold.gold_rows
    or silver.silver_revenue <> gold.gold_revenue
