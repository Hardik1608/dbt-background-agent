/*
Q:  "Top 3 products by revenue in each of the 5 largest countries."

Concepts: ROW_NUMBER vs RANK vs DENSE_RANK, top-N per group.

  ROW_NUMBER  1,2,3,4  - always unique; ties broken arbitrarily (add a tie-breaker!)
  RANK        1,2,2,4  - ties share a rank, then a gap
  DENSE_RANK  1,2,2,3  - ties share a rank, no gap -> "top 3 distinct revenue levels"

Snowflake / BigQuery / DuckDB support QUALIFY, which filters on a window function
without a subquery:  ... qualify dense_rank() over (...) <= 3
The CTE form below is the portable equivalent (Postgres, SQL Server, MySQL 8).
*/

with product_country_revenue as (

    select
        dim_country.country_name,
        dim_product.stock_code,
        dim_product.product_description,
        sum(fct.line_revenue)           as revenue
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    inner join {{ ref('dim_country') }} as dim_country
        on fct.country_key = dim_country.country_key
    where dim_product.is_merchandise
        and fct.line_type in ('sale', 'cancellation')
    group by
        dim_country.country_name,
        dim_product.stock_code,
        dim_product.product_description

),

top_countries as (

    select country_name
    from product_country_revenue
    group by country_name
    order by sum(revenue) desc
    limit 5

),

ranked as (

    select
        product_country_revenue.*,
        row_number() over (
            partition by country_name order by revenue desc, stock_code asc
        )                                                   as row_num,
        rank() over (partition by country_name order by revenue desc)       as rnk,
        dense_rank() over (partition by country_name order by revenue desc) as dense_rnk
    from product_country_revenue
    where product_country_revenue.country_name in (
        select top_countries.country_name from top_countries
    )

)

select
    country_name,
    dense_rnk,
    rnk,
    row_num,
    stock_code,
    product_description,
    round(revenue, 2) as revenue
from ranked
where dense_rnk <= 3
order by country_name, dense_rnk, row_num
