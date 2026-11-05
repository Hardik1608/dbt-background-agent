{#
    Calendar helpers. Only day_of_week_iso differs per warehouse, so it is the one macro
    that uses adapter.dispatch; names are derived from numbers to stay portable
    (Snowflake's DAYNAME returns 'Mon', DuckDB's returns 'Monday', BigQuery has neither).
    Only the DuckDB implementation is exercised by CI.
#}

{% macro day_of_week_iso(date_expr) -%}
    {{ return(adapter.dispatch('day_of_week_iso', 'sales_analytics')(date_expr)) }}
{%- endmacro %}

{% macro default__day_of_week_iso(date_expr) -%}
    extract(isodow from {{ date_expr }})
{%- endmacro %}

{% macro duckdb__day_of_week_iso(date_expr) -%}
    isodow({{ date_expr }})
{%- endmacro %}

{% macro snowflake__day_of_week_iso(date_expr) -%}
    dayofweekiso({{ date_expr }})
{%- endmacro %}

{% macro bigquery__day_of_week_iso(date_expr) -%}
    {#- BigQuery DAYOFWEEK: 1 = Sunday ... 7 = Saturday -#}
    mod(extract(dayofweek from {{ date_expr }}) + 5, 7) + 1
{%- endmacro %}


{% macro day_name(date_expr) -%}
    case {{ day_of_week_iso(date_expr) }}
        when 1 then 'Monday'
        when 2 then 'Tuesday'
        when 3 then 'Wednesday'
        when 4 then 'Thursday'
        when 5 then 'Friday'
        when 6 then 'Saturday'
        else 'Sunday'
    end
{%- endmacro %}


{% macro month_name(date_expr) -%}
    case extract(month from {{ date_expr }})
        when 1 then 'January'
        when 2 then 'February'
        when 3 then 'March'
        when 4 then 'April'
        when 5 then 'May'
        when 6 then 'June'
        when 7 then 'July'
        when 8 then 'August'
        when 9 then 'September'
        when 10 then 'October'
        when 11 then 'November'
        else 'December'
    end
{%- endmacro %}
