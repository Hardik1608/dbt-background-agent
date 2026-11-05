{{ config(materialized='incremental', unique_key='id') }}

-- watermark on load time: id 1 arrives late (loaded last, smallest id)
select
    id,
    customer,
    amount,
    loaded_at
from {{ ref('raw_events') }}
{% if is_incremental() %}
where loaded_at > (select coalesce(max(loaded_at), '1900-01-01') from {{ this }})
{% endif %}
