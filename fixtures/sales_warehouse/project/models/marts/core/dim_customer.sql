-- Gold: customer dimension, SCD Type 2, built from the snap_customer_state snapshot.
--
-- Grain: one row per customer *version*. customer_sk (the snapshot's dbt_scd_id) is the
-- surrogate key the fact table stores, so a sale made while a customer was "silver" stays
-- attributed to "silver" after they become "gold".
--
-- Conventions:
--   * half-open validity [valid_from, valid_to); the current version ends at 9999-12-31
--   * the first version is back-dated to 1900-01-01: a snapshot only observes history
--     from the moment it starts running, and facts that pre-date the first observation
--     must still resolve to a version (documented limitation - see docs/DATA_MODEL.md)
--   * an "unknown member" row (customer_sk = '-1') absorbs guest checkouts, so the fact's
--     foreign key is never NULL

{{ config(materialized='table') }}

with snapshot as (

    select * from {{ ref('snap_customer_state') }}

),

versions as (

    select
        dbt_scd_id                                                  as customer_sk,
        customer_id,
        country_name,
        loyalty_tier,
        first_invoice_at,
        dbt_valid_from                                              as observed_valid_from,
        dbt_valid_to                                                as valid_to,
        row_number() over (
            partition by customer_id order by dbt_valid_from
        )                                                           as version_number
    from snapshot

),

known_customers as (

    select
        customer_sk,
        customer_id,
        country_name,
        loyalty_tier,
        cast(version_number as integer)                             as version_number,
        case
            when version_number = 1 then {{ scd_start_of_time() }}
            else observed_valid_from
        end                                                         as valid_from,
        valid_to,
        valid_to = {{ scd_end_of_time() }}                          as is_current,
        first_invoice_at
    from versions

),

unknown_member as (

    select
        '-1'                                                        as customer_sk,
        '-1'                                                        as customer_id,
        'Unknown'                                                   as country_name,
        'unknown'                                                   as loyalty_tier,
        cast(1 as integer)                                          as version_number,
        {{ scd_start_of_time() }}                                   as valid_from,
        {{ scd_end_of_time() }}                                     as valid_to,
        true                                                        as is_current,
        cast(null as timestamp)                                     as first_invoice_at

)

select * from known_customers
union all
select * from unknown_member
