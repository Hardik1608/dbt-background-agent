# Example pull requests

Unedited `PR.md` and `patch.diff` files from the reported sales-suite run with the local model
(Qwen3.5-9B int4, Docker sandbox, GPU box, 2026-10-15; run directory
`runs/local_agent/<task>__t0/pr/` on the box). Their grades are in
`reports/sales_local_agent_k1.md`.

| Task | What happened | Grade |
|---|---|---|
| [B02_source_price_renamed](B02_source_price_renamed.PR.md) | CI failure after an upstream rename: the agent fixed the source YAML and the staging model; all checks pass; HIGH risk only because 13 downstream models are affected | resolved |
| [F12_customer_features_is_repeat](F12_customer_features_is_repeat.PR.md) | Feature ticket: a new column in the ML feature table, with its contract type, documentation and the unit test's expected row updated; LOW risk | resolved |
| [F15_dim_product_n_customers](F15_dim_product_n_customers.PR.md) | Feature ticket where every deterministic check passed, but the agent added a filter the ticket did not ask for; the reviewer requested changes and named exactly that filter | not resolved (hidden test failed) |
