{#
    Use the configured layer schema verbatim (silver / gold / reference / snapshots)
    instead of dbt's default "<target_schema>_<custom_schema>".

    Each dataset gets its own DuckDB file, so dev and CI never collide. On a shared
    Snowflake/BigQuery account you would prefix non-prod targets, e.g.
    "{{ target.schema }}_{{ custom_schema_name }}" unless target.name == 'prod'.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
