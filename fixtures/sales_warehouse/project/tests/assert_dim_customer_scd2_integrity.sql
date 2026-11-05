-- SCD Type 2 integrity for dim_customer. Per customer:
--   * versions tile time with no gaps and no overlaps (next.valid_from = this.valid_to)
--   * valid_from < valid_to for every version
--   * exactly one current version
-- Returns one row per violation.

with versions as (

    select
        customer_id,
        customer_sk,
        valid_from,
        valid_to,
        is_current,
        lead(valid_from) over (
            partition by customer_id order by valid_from
        ) as next_valid_from
    from {{ ref('dim_customer') }}

),

gaps_or_overlaps as (

    select customer_id, customer_sk, 'gap_or_overlap' as violation
    from versions
    where next_valid_from is not null
        and next_valid_from <> valid_to

),

inverted_ranges as (

    select customer_id, customer_sk, 'valid_from_not_before_valid_to' as violation
    from versions
    where valid_from >= valid_to

),

current_version_count as (

    select customer_id, max(customer_sk) as customer_sk, 'not_exactly_one_current' as violation
    from versions
    group by customer_id
    having sum(case when is_current then 1 else 0 end) <> 1

)

select * from gaps_or_overlaps
union all
select * from inverted_ranges
union all
select * from current_version_count
