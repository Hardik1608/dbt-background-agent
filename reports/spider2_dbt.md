# Spider 2.0-DBT

- Model: `local-agent (Qwen3.5-9B int4)`; harness: dbt-background-agent; one trial per task.
- Official evaluate.py: **9/68 (13.2%)** (evaluated 68 submissions; the unmodified script was run once per instance with that instance's gold database).
- Context (external, not ours): leaderboard top reported 65.6% (SignalPilot, vendor-reported).

| Status of the agent loop | Runs |
|---|---|
| budget_exhausted | 46 |
| completed | 9 |
| llm_error | 1 |
| loop_detected | 12 |

Passed instances: f1003, lever001, maturity001, playbook001, quickbooks002, quickbooks003, retail001, salesforce001, tickit002

Runs that produced every table the gold spec compares: 25/68.

Instances no agent can pass with the public data (they still count in the denominator, as in the official script):
- no gold database in `dbt_gold.zip`: airbnb002, biketheft001, google_ads001, gitcoin001
- gold file name in `spider2_eval.jsonl` not in the archive: xero_new001, social_media001, xero_new002
