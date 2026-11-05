-- Gold: country dimension. Reference seed full-outer-joined with the countries actually
-- observed, so a new country in the feed still gets a key (region 'Unmapped') instead of
-- silently dropping fact rows; the staging relationships test warns about it.

with reference as (

    select * from {{ ref('country_reference') }}

),

observed as (

    select distinct country_name
    from {{ ref('int_order_lines__deduplicated') }}

),

countries as (

    select
        coalesce(reference.country_name, observed.country_name)     as country_name,
        reference.iso_alpha2,
        coalesce(reference.region, 'Unmapped')                      as region,
        coalesce(reference.is_domestic_market, false)               as is_domestic_market,
        observed.country_name is not null                           as has_sales
    from reference
    full outer join observed
        on reference.country_name = observed.country_name

)

select
    {{ dbt_utils.generate_surrogate_key(['country_name']) }}        as country_key,
    country_name,
    iso_alpha2,
    region,
    is_domestic_market,
    has_sales
from countries
