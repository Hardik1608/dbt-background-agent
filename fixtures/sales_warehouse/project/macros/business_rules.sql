{#
    Business rules shared by several models. Keeping them in one macro means the SCD2
    dimension, its unit tests and any analysis classify customers identically.
#}

{% macro loyalty_tier(amount_expr) -%}
    {%- set t = var('loyalty_tier_thresholds') -%}
    case
        when {{ amount_expr }} >= {{ t['platinum'] }} then 'platinum'
        when {{ amount_expr }} >= {{ t['gold'] }} then 'gold'
        when {{ amount_expr }} >= {{ t['silver'] }} then 'silver'
        else 'bronze'
    end
{%- endmacro %}


{#- Portable integer date key (yyyymmdd) - works on DuckDB, Snowflake and BigQuery. -#}
{% macro date_key(date_expr) -%}
    cast(
        extract(year from {{ date_expr }}) * 10000
        + extract(month from {{ date_expr }}) * 100
        + extract(day from {{ date_expr }})
        as {{ dbt.type_int() }}
    )
{%- endmacro %}


{% macro scd_end_of_time() -%}
    cast('{{ var("scd_end_of_time") }}' as {{ dbt.type_timestamp() }})
{%- endmacro %}


{% macro scd_start_of_time() -%}
    cast('{{ var("scd_start_of_time") }}' as {{ dbt.type_timestamp() }})
{%- endmacro %}
