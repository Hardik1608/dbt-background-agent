/*
Q:  "Which pairs of products are most often bought together?"

Concepts: self-join on the order id; "a.product_key < b.product_key" counts each
unordered pair once and drops self-pairs; association metrics:
  support    = P(A and B)            = invoices with both / all invoices
  confidence = P(B | A)              = invoices with both / invoices with A
  lift       = P(A and B)/(P(A)P(B)) > 1 means bought together more than by chance

Gotcha: the join is O(lines per invoice ^ 2). Deduplicate to (invoice, product) first
and prune rare pairs with HAVING before joining back to dimensions.
*/

with baskets as (

    select distinct fct.invoice_id, fct.product_key
    from {{ ref('fct_order_lines') }} as fct
    inner join {{ ref('dim_product') }} as dim_product
        on fct.product_key = dim_product.product_key
    where fct.line_type = 'sale'
        and dim_product.is_merchandise

),

total as (

    select count(distinct invoice_id) as n_invoices from baskets

),

product_counts as (

    select product_key, count(*) as n_invoices
    from baskets
    group by product_key

),

pairs as (

    select
        a.product_key   as product_a,
        b.product_key   as product_b,
        count(*)        as n_together
    from baskets as a
    inner join baskets as b
        on a.invoice_id = b.invoice_id
        and a.product_key < b.product_key
    group by a.product_key, b.product_key
    having count(*) >= 20

)

select
    product_a_dim.product_description                                       as product_a,
    product_b_dim.product_description                                       as product_b,
    pairs.n_together,
    round(pairs.n_together * 1.0 / total.n_invoices, 4)                     as support,
    round(pairs.n_together * 1.0 / count_a.n_invoices, 3)                   as confidence_a_to_b,
    round(pairs.n_together * 1.0 * total.n_invoices
          / (count_a.n_invoices * count_b.n_invoices), 1)                   as lift
from pairs
cross join total
inner join product_counts as count_a
    on pairs.product_a = count_a.product_key
inner join product_counts as count_b
    on pairs.product_b = count_b.product_key
inner join {{ ref('dim_product') }} as product_a_dim
    on pairs.product_a = product_a_dim.product_key
inner join {{ ref('dim_product') }} as product_b_dim
    on pairs.product_b = product_b_dim.product_key
order by pairs.n_together desc, product_a asc, product_b asc
limit 15
